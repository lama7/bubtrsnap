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

import unittest
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


if __name__ == "__main__":
    unittest.main()
