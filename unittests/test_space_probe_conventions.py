#!/usr/bin/env python3
"""Tests that remote space probes honour the run() helper convention (TODO item 18).

_remote_disk_free and _remote_size_of used raw subprocess.run, bypassing
run(): no [dry-run]/level-2 command logging, no display quoting, and any
future central change to run() (timeouts, sudo handling, logging) would
silently skip these probes.
"""

from __future__ import annotations

import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()


class TestRemoteSpaceProbesUseRun(unittest.TestCase):

    @patch("bubtrsnap.run")
    def test_remote_disk_free_goes_through_run(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=("Filesystem 1-blocks Used Available Use% Mounted on\n"
                    "/dev/sda1 1000000000 500000000 500000000 50% /backup\n"),
            stderr="",
        )
        cfg = {"verbose": 0, "dry_run": False}
        result = bs._remote_disk_free("user@host", "/backup", cfg)
        self.assertEqual(result, 500000000)
        self.assertEqual(mock_run.call_count, 1,
                         "df probe must go through the run() helper")
        kwargs = mock_run.call_args.kwargs
        self.assertTrue(kwargs.get("read_only", False),
                        "df is read-only: must run even during dry-run")
        self.assertIn("timeout", kwargs, "ssh probe must have a timeout")

    @patch("bubtrsnap.run")
    def test_remote_disk_free_dry_run_still_probes(self, mock_run):
        """read_only=True means run() executes the df even in dry-run mode."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=("Filesystem 1-blocks Used Available Use% Mounted on\n"
                    "/dev/sda1 1000000000 400000000 600000000 40% /backup\n"),
            stderr="",
        )
        cfg = {"verbose": 0, "dry_run": True}
        result = bs._remote_disk_free("user@host", "/backup", cfg)
        self.assertEqual(result, 600000000)

    @patch("bubtrsnap.run")
    def test_remote_disk_free_returns_none_when_probe_unavailable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ssh: connect failed")
        cfg = {"verbose": 0, "dry_run": False}
        self.assertIsNone(bs._remote_disk_free("user@host", "/backup", cfg))

    @patch("bubtrsnap.run")
    def test_remote_size_of_goes_through_run(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1234567\t/backup/subvol\n", stderr="")
        cfg = {"verbose": 0, "dry_run": False}
        result = bs._remote_size_of("user@host", "/backup/subvol", cfg)
        self.assertEqual(result, 1234567)
        self.assertEqual(mock_run.call_count, 1,
                         "du probe must go through the run() helper")
        kwargs = mock_run.call_args.kwargs
        self.assertTrue(kwargs.get("read_only", False))
        self.assertIn("timeout", kwargs)

    @patch("bubtrsnap.run")
    def test_sudo_flag_still_scoped_to_remote_command(self, mock_run):
        """remote_sudo prefixes df with sudo on the remote, never ssh itself."""
        mock_run.return_value = MagicMock(returncode=0, stdout="x 1 2 3 4%\n", stderr="")
        cfg = {"verbose": 0, "dry_run": False}
        bs._remote_disk_free("user@host", "/backup", cfg, remote_sudo=True)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("sudo", cmd)  # sudo runs the remote df, after ssh
        self.assertTrue(cmd.index("sudo") > cmd.index("ssh"))


if __name__ == "__main__":
    unittest.main()
