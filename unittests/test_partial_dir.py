#!/usr/bin/env python3
"""Unit tests for per-archive rsync partial-dir detection.

Regression guard for the shared /tmp/.bubtrsnap-partial bug: the
interrupted-transfer check was per-remote, so a stale partial-dir from
archive A could make archive B believe its own transfer was interrupted.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from importlib.machinery import SourceFileLoader
from unittest.mock import patch


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()


class TestPartialDir(unittest.TestCase):
    def test_check_uses_archive_specific_dir(self):
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with patch.object(bs, "run", fake_run):
            self.assertTrue(bs._check_interrupted_rsync(
                "user@host", "corinne", {"verbose": 0}))
        self.assertEqual(cmds[0][-1], "/tmp/.bubtrsnap-partial-corinne")

    def test_other_archive_partial_does_not_flag_this_archive(self):
        def fake_run(cmd, **kwargs):
            # only archive 'alpha' has an interrupted transfer on the remote
            rc = 0 if ".bubtrsnap-partial-alpha" in cmd[-1] else 1
            return subprocess.CompletedProcess(cmd, rc)

        with patch.object(bs, "run", fake_run):
            self.assertFalse(bs._check_interrupted_rsync(
                "user@host", "beta", {"verbose": 0}))

    def test_archive_name_is_sanitized(self):
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with patch.object(bs, "run", fake_run):
            bs._check_interrupted_rsync("user@host", "my archive!", {"verbose": 0})
        self.assertEqual(cmds[0][-1], "/tmp/.bubtrsnap-partial-my_archive_")

    def test_rsync_uses_archive_specific_partial_dir(self):
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="At subvol corinne.202610150423", stderr="")

        with patch.object(bs, "run", fake_run):
            bs._rsync_and_receive(Path("/tmp/corinne.202610150423.btrfs"),
                                 "user@host", "/backup",
                                 {"verbose": 0, "dry_run": False},
                                 archive_name="corinne")
        rsync_cmd = next(c for c in cmds if c[0] == "rsync")
        self.assertEqual(rsync_cmd[rsync_cmd.index("--partial-dir") + 1],
                         ".bubtrsnap-partial-corinne")

    def test_receive_stream_threads_archive_name_to_rsync(self):
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="At subvol beta.202610150423", stderr="")

        with patch.object(bs, "run", fake_run):
            bs.receive_stream(Path("/tmp/beta.202610150423.btrfs"), None,
                              {"verbose": 0, "dry_run": False},
                              remote="user@host", remote_dir="/backup",
                              use_rsync=True, archive_name="beta")
        rsync_cmd = next(c for c in cmds if c[0] == "rsync")
        self.assertEqual(rsync_cmd[rsync_cmd.index("--partial-dir") + 1],
                         ".bubtrsnap-partial-beta")


if __name__ == "__main__":
    unittest.main()
