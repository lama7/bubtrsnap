#!/usr/bin/env python3
"""Unit test for the remote->local snapshot restore branch of _run_rebuild.

When a snapshot is missing locally but present on the remote, rebuild must
recover it by `ssh send | local receive` into snapshot_dir (a top-level
readonly subvolume, matching the normal snapshot convention).

This exercises the new `from_remote` branch: the planning decision
(`in_r and not in_s and not in_b`), the snapshot-space guard, and the
Phase 4 execution that pipes an SSH send into a local receive.

TestRebuildFailureReporting covers what happens when a Phase 4 leg fails:
it must be visible at verbosity 1 and the run must exit non-zero.
"""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout, redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock
import tempfile

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
if not _path.is_file():
    raise FileNotFoundError(f"Cannot find bubtrsnap.py at {_path}")
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()


class TestRebuildFromRemote(unittest.TestCase):
    """The remote->local (from_remote) snapshot restore branch."""

    def _cfg(self, snap_dir: Path, remote: str, remote_dir: str) -> dict:
        return {
            "local_sudo": True,
            "verbose": 2,
            "dry_run": True,
            "snapshot_dir": str(snap_dir),
            "backup_dir": None,  # no backup column -> in_b always False
            "remote_host": remote,
            "remote_path": remote_dir,
            "remote_sudo": True,
            "keep_hourly": 0,
            "keep_daily": 0,
            "keep_weekly": 0,
            "keep_monthly": 0,
            "keep_yearly": 0,
            "week_startday": "sunday",
        }

    @mock.patch.object(bs, "piped_run")
    @mock.patch.object(bs, "_remote_size_of", return_value=52428800)
    @mock.patch.object(bs, "iter_archive_items_ssh")
    @mock.patch.object(bs, "iter_archive_items")
    def test_from_remote_restores_snapshot(self,
                                           m_iter_local,
                                           m_iter_remote,
                                           m_size,
                                           m_piped):
        """A snapshot only on the remote is restored via ssh send | local receive."""
        remote = "gerry@thorin"
        remote_dir = "/run/media/gerry/backup"
        ts = "202609190820"
        remote_subvol = f"{remote_dir}/root.{ts}"

        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            cfg = self._cfg(snap_dir, remote, remote_dir)
            archive = {
                "name": "root",
                "subvolume": str(snap_dir / "root"),
                "remote_host": remote,
                "remote_path": remote_dir,
                "remote_sudo": True,
            }

            # in_s=False, in_b=False (no backup_dir), in_r=True (orphan on remote)
            m_iter_local.return_value = iter([])
            m_iter_remote.return_value = iter([(ts, f"{remote_dir}/root.{ts}")])

            def _fake_pipe(send_cmd, recv_cmd, **kwargs):
                # record the exact commands the branch built
                m_piped.captured = (send_cmd, recv_cmd)
                return 0, ""

            m_piped.side_effect = _fake_pipe

            rc = bs._run_rebuild([archive], cfg, verbosity=2)

            self.assertEqual(rc, 0)
            self.assertTrue(m_piped.captured, "piped_run was not invoked")
            send_cmd, recv_cmd = m_piped.captured

            # --- send side: an SSH command, plain (non-incremental) send ---
            self.assertIn("ssh", send_cmd)
            self.assertIn("btrfs", send_cmd)
            self.assertIn("send", send_cmd)
            self.assertIn(remote_subvol, send_cmd)
            self.assertNotIn("-c", send_cmd)  # not incremental
            self.assertNotIn("-p", send_cmd)  # not incremental
            self.assertNotIn("-r", send_cmd)  # no readonly flag on the send

            # --- receive side: a LOCAL btrfs receive into snapshot_dir ---
            self.assertIn("btrfs", recv_cmd)
            self.assertIn("receive", recv_cmd)
            self.assertEqual(recv_cmd[-1], str(snap_dir))
            self.assertNotIn("ssh", recv_cmd)  # receive runs locally, not remote


class TestRebuildWithoutBackupDir(unittest.TestCase):
    """A snapshot-only archive (no backup_dir configured) has nothing to
    restore locally.

    The planner used to emit `restore local backup` rows with dest=None for
    every snapshot, and the execution phase then raised AttributeError inside
    _piped_send_to_local (glob on None), swallowed by the broad except and
    reported as a restore failure.
    """

    @mock.patch.object(bs, "_piped_send_to_local")
    @mock.patch.object(bs, "iter_archive_items_ssh")
    def test_snapshot_only_plans_no_local_restore(self, m_iter_ssh, m_piped):
        m_iter_ssh.return_value = []
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            for ts in ("202610011200", "202610021200"):
                (snap_dir / f"root.{ts}").mkdir()
            cfg = {
                "verbose": 2, "dry_run": True, "local_sudo": False,
                "snapshot_dir": str(snap_dir), "backup_dir": None,
                "remote_host": None, "remote_path": None, "remote_sudo": False,
            }
            archive = {"name": "root", "subvolume": "/home", "backup_dir": None,
                       "remote_host": None, "remote_path": None,
                       "remote_sudo": False}
            fake = subprocess.CompletedProcess([], 0, "", "")
            out = io.StringIO()
            with mock.patch.object(bs, "run", return_value=fake), redirect_stdout(out):
                rc = bs._run_rebuild([archive], cfg, 2)
            text = out.getvalue()
            self.assertNotIn("restore local backup", text)
            self.assertNotIn("NoneType", text)
            self.assertNotIn("failed", text)
            m_piped.assert_not_called()
            self.assertEqual(rc, 0)

    @mock.patch.object(bs, "_piped_send_to_local")
    @mock.patch.object(bs, "iter_archive_items_ssh")
    def test_snapshot_plus_remote_still_re_sends_to_remote(self, m_iter_ssh, m_piped):
        """No backup_dir, but a remote that is missing the snapshot: the plan
        must re-send from the snapshot, not attempt a local backup restore."""
        m_iter_ssh.return_value = []
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            (snap_dir / f"root.{ts}").mkdir()
            cfg = {
                "verbose": 2, "dry_run": True, "local_sudo": False,
                "snapshot_dir": str(snap_dir), "backup_dir": None,
                "remote_host": "user@host", "remote_path": "/backup",
                "remote_sudo": False,
            }
            archive = {"name": "root", "subvolume": "/home", "backup_dir": None,
                       "remote_host": "user@host", "remote_path": "/backup",
                       "remote_sudo": False}
            fake = subprocess.CompletedProcess([], 0, "", "")
            out = io.StringIO()
            with mock.patch.object(bs, "run", return_value=fake), redirect_stdout(out):
                bs._run_rebuild([archive], cfg, 2)
            text = out.getvalue()
            self.assertIn("re-send to remote (snapshot)", text)
            self.assertNotIn("restore local backup", text)
            m_piped.assert_not_called()


class TestRebuildFailureReporting(unittest.TestCase):
    """A failed rebuild leg must be visible at verbosity 1 and make the run
    exit non-zero.

    Before the fix, remote re-send and remote->local restore failures were
    logged at level 3 (invisible at the default verbosity) and _run_rebuild
    returned 0 no matter what phase 4 did, so `bubtrsnap rebuild` reported
    success while a receive had died half-way leaving a partial subvolume.
    """

    ENOSPC = "receive failed: ERROR: writing to huge.bin failed: " \
             "No space left on device"

    def _cfg(self, snap_dir: Path, backup_dir: Path | None = None,
             remote: str | None = None, remote_dir: str | None = None) -> dict:
        return {
            "verbose": 1, "dry_run": False, "local_sudo": False,
            "snapshot_dir": str(snap_dir),
            "backup_dir": str(backup_dir) if backup_dir else None,
            "remote_host": remote, "remote_path": remote_dir,
            "remote_sudo": False,
            "keep_hourly": 0, "keep_daily": 0, "keep_weekly": 0,
            "keep_monthly": 0, "keep_yearly": 0,
            "week_startday": "sunday",
        }

    def _archive(self, snap_dir: Path, name: str = "root",
                 remote: str | None = None, remote_dir: str | None = None) -> dict:
        return {"name": name, "subvolume": str(snap_dir / name),
                "remote_host": remote, "remote_path": remote_dir,
                "remote_sudo": False}

    def _run(self, archives, cfg, patches):
        """Run _run_rebuild with the given (name, kwargs) patches applied.

        Returns (rc, stdout_text, stderr_text).
        """
        out, err = io.StringIO(), io.StringIO()
        fake = subprocess.CompletedProcess([], 0, "", "")
        stack = [mock.patch.object(bs, name, **kw) for name, kw in patches]
        stack.append(mock.patch.object(bs, "run", return_value=fake))
        for p in stack:
            p.__enter__()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = bs._run_rebuild(archives, cfg, cfg["verbose"])
        finally:
            for p in reversed(stack):
                p.__exit__(None, None, None)
        return rc, out.getvalue(), err.getvalue()

    def test_remote_resend_failure_visible_and_nonzero(self):
        """A remote re-send that dies with ENOSPC must be reported at -v 1 and
        must not report a successful rebuild."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            (snap_dir / f"root.{ts}").mkdir()
            cfg = self._cfg(snap_dir, remote="user@host", remote_dir="/backup")
            archive = self._archive(snap_dir, remote="user@host",
                                    remote_dir="/backup")
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents_ssh", {"return_value": []}),
                 ("piped_run", {"return_value": (1, self.ENOSPC)})],
            )
            self.assertEqual(rc, 1, "rebuild must exit non-zero when a leg failed")
            self.assertIn(f"root.{ts}: remote re-send failed", out)
            self.assertIn("No space left on device", out)
            self.assertIn("1 rebuild action(s) failed", err)
            self.assertIn(f"root.{ts}", err)

    def test_from_remote_failure_visible_and_nonzero(self):
        """Remote->local restore is the rebuild retry path; a dead receive
        there must be visible at -v 1 and exit non-zero."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            cfg = self._cfg(snap_dir, remote="user@host", remote_dir="/backup")
            archive = self._archive(snap_dir, remote="user@host",
                                    remote_dir="/backup")
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items", {"return_value": iter([])}),
                 ("iter_archive_items_ssh",
                  {"return_value": iter([(ts, f"/backup/root.{ts}")])}),
                 ("_remote_size_of", {"return_value": 1024}),
                 ("piped_run", {"return_value": (1, self.ENOSPC)})],
            )
            self.assertEqual(rc, 1)
            self.assertIn("snapshot restore from remote failed", out)
            self.assertIn("No space left on device", out)
            self.assertIn("1 rebuild action(s) failed", err)

    def test_local_restore_exception_returns_nonzero(self):
        """An exception from the local piped send/receive already prints at -v 1,
        but the exit code still has to reflect it."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            (snap_dir / f"root.{ts}").mkdir()
            bak_dir = snap_dir / "backup"
            bak_dir.mkdir()
            cfg = self._cfg(snap_dir, backup_dir=bak_dir)
            archive = self._archive(snap_dir)
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents", {"return_value": []}),
                 ("_piped_send_to_local",
                  {"side_effect": RuntimeError(
                      "btrfs receive (local) failed: No space left on device")})],
            )
            self.assertEqual(rc, 1)
            self.assertIn("local backup restore failed", out)
            self.assertIn("1 rebuild action(s) failed", err)

    def test_all_legs_succeed_returns_zero(self):
        """The happy path must keep returning 0 and the original summary line."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            cfg = self._cfg(snap_dir, remote="user@host", remote_dir="/backup")
            archive = self._archive(snap_dir, remote="user@host",
                                    remote_dir="/backup")
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items", {"return_value": iter([])}),
                 ("iter_archive_items_ssh",
                  {"return_value": iter([(ts, f"/backup/root.{ts}")])}),
                 ("_remote_size_of", {"return_value": 1024}),
                 ("piped_run", {"return_value": (0, "")})],
            )
            self.assertEqual(rc, 0)
            self.assertIn("Rebuild complete: 1 restored.", out)
            self.assertEqual(err.strip(), "")

    def test_space_guard_skip_is_not_a_failure(self):
        """A leg the space guard chose not to attempt is a skip, not a failure:
        the run still exits 0 so cron/callers don't see an error for a pool
        that simply cannot fit a full send."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202610011200"
            snap = snap_dir / f"root.{ts}"
            snap.mkdir()
            (snap / "data.bin").write_bytes(b"x" * 4096)
            bak_dir = snap_dir / "backup"
            bak_dir.mkdir()
            cfg = self._cfg(snap_dir, backup_dir=bak_dir)
            archive = self._archive(snap_dir)
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents", {"return_value": []}),
                 ("_space_check", {"return_value": False})],
            )
            self.assertEqual(rc, 0)
            self.assertNotIn("failed", out)
            self.assertEqual(err.strip(), "")

    def test_mixed_success_and_failure_exits_nonzero(self):
        """One good leg does not mask one bad leg."""
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts1, ts2 = "202610011200", "202610021200"
            for ts in (ts1, ts2):
                (snap_dir / f"root.{ts}").mkdir()
            cfg = self._cfg(snap_dir, remote="user@host", remote_dir="/backup")
            archive = self._archive(snap_dir, remote="user@host",
                                    remote_dir="/backup")
            rc, out, err = self._run(
                [archive], cfg,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents_ssh", {"return_value": []}),
                 ("piped_run",
                  {"side_effect": [(0, ""), (1, self.ENOSPC)]})],
            )
            self.assertEqual(rc, 1)
            self.assertIn("Rebuild complete: 1 restored, 1 failed.", out)
            self.assertIn(f"root.{ts2}", err)
            self.assertNotIn(f"root.{ts1}", err)


if __name__ == "__main__":
    unittest.main()
