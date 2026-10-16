#!/usr/bin/env python3
"""Unit test for the remote->local snapshot restore branch of _run_rebuild.

When a snapshot is missing locally but present on the remote, rebuild must
recover it by `ssh send | local receive` into snapshot_dir (a top-level
readonly subvolume, matching the normal snapshot convention).

This exercises the new `from_remote` branch: the planning decision
(`in_r and not in_s and not in_b`), the snapshot-space guard, and the
Phase 4 execution that pipes an SSH send into a local receive.
"""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
