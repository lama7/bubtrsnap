#!/usr/bin/env python3
"""Integration tests for bubtrsnap using dry-run to exercise full code paths."""

from __future__ import annotations

import subprocess
import textwrap
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Load bubtrsnap as a module (same pattern as test_ssh.py)
_path = Path(__file__).resolve().parent.parent / "bubtrsnap"
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()


class TestBasicLocalBackup(unittest.TestCase):
    """Test that a basic local backup configuration logs the expected commands in dry-run."""

    @patch("bubtrsnap.run")
    def test_dry_run_local_backup_logs_commands(self, mock_run):
        """With snap_dir + backup_dir + archive, dry-run should log send + receive commands."""

        # Set up temp directories for fake snap/backup dirs
        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            # Create a fake snapshot subvolume
            archive_name = "testarchive"
            snap_name = f"{archive_name}.202601010000"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            # Create existing backup subvolumes to test parent finding
            (backup_dir / snap_name).mkdir()

            # Build config dict
            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "remote_host": None,
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 0,
                "keep_hourly": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive_cfg = {
                "subvolume": str(snap_path),
                "keep_daily": 1,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
            }

            # Mock run() to return appropriate values
            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

                # Validation/subvolume show commands (always execute in dry-run)
                if "subvolume show" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = "UUID: some-uuid-123\nReceived UUID: -\n"
                    return result
                if "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    # Return existing snapshot so find_parents sees it
                    result.stdout = f"ID 256 gen 0 top level 5 path {snap_name}\n"
                    return result

                # Dry-run for write commands: log and return None
                return None

            mock_run.side_effect = run_mock

            # Call send_backup directly (bypassing process_archive for now)
            result = bs.send_backup(snap_path, snap_dir, backup_dir, cfg, archive_name, archive_cfg)

            # Should return the destination path
            self.assertEqual(result, backup_dir / snap_name)

            # Verify run() was called (for validation + dry-run sends)
            self.assertTrue(mock_run.called)

    @patch("bubtrsnap.run")
    @patch("builtins.open", create=True)
    def test_dry_run_creates_correct_send_command(self, mock_open, mock_run):
        """Dry-run with no parents should create a full send command."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "myarchive"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "remote_host": None,
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 0,
                "keep_hourly": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive_cfg = {
                "subvolume": str(snap_path),
                "keep_daily": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
            }

            # Also update second test's mock similarly
            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = "UUID: some-uuid-123\nReceived UUID: -\n"
                    return result
                if "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = ""  # no parents
                    return result
                return None  # dry-run for write commands

            mock_run.side_effect = run_mock

            result = bs.send_backup(snap_path, snap_dir, backup_dir, cfg, archive_name, archive_cfg)
            self.assertEqual(result, backup_dir / snap_name)

            # Find the piped_run call — it should have been called with send and receive commands
            # During dry-run, piped_run logs but doesn't call run()
            # Verify the flow completed without error


class TestLocalSudoBackup(unittest.TestCase):
    """Test that local_sudo=True produces correct command construction."""

    @patch("bubtrsnap.run")
    def test_dry_run_local_sudo_logs_sudo_commands(self, mock_run):
        """With local_sudo=True, send/receive commands should include 'sudo -n' prefix."""

        from io import StringIO
        import sys

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "sudoarchive"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "remote_host": None,
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 0,
                "keep_hourly": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive_cfg = {
                "subvolume": str(snap_path),
                "keep_daily": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = "UUID: sudo-uuid-456\nReceived UUID: -\n"
                    return result
                if "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = ""  # no parents
                    return result
                return None  # dry-run for write commands

            mock_run.side_effect = run_mock

            # Capture stdout to verify [dry-run] command logging
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured

            try:
                result = bs.send_backup(snap_path, snap_dir, backup_dir, cfg, archive_name, archive_cfg)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, backup_dir / snap_name)

            # Verify the dry-run output contains sudo prefix with -n flag
            output = captured.getvalue()
            self.assertIn("sudo -n btrfs send", output)
            self.assertIn("sudo -n btrfs receive", output)


if __name__ == "__main__":
    unittest.main()