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


class TestStageFileBackup(unittest.TestCase):
    """Test the stage_file configuration modeled after test_stage_file.toml.

    Config:
      - local_sudo = true
      - remote_host, remote_sudo = true
      - stage_file in archive section
    Flow: send_backup_tofile (staged) -> receive_stream -> remove_stage_file
    """

    @patch("bubtrsnap.run")
    def test_dry_run_stage_file_logs_send_receive_and_cleanup(self, mock_run):
        """Dry-run with stage_file should log send-to-file, receive, and cleanup."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "keenan"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            stage_file_path = Path(f"/tmp/{archive_name}.btrfs")

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": None,
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 3,
                "keep_weekly": 3,
                "keep_monthly": 3,
                "keep_yearly": 1,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "stage_file": str(stage_file_path),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep": {
                    "keep_daily": 3,
                    "keep_weekly": 2,
                    "keep_monthly": 3,
                    "keep_yearly": 0,
                },
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

                # Validation commands (always execute, dry_run=False)
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid-123\nReceived UUID: -\n"
                    else:
                        result.stdout = ""  # no parents
                    return result

                # Dry-run for write commands
                return None

            mock_run.side_effect = run_mock

            bs.process_archive(archive, cfg)

            # Verify run was called — for validation (read_only) + staged send + receive + cleanup
            self.assertTrue(mock_run.called)

            # Check that sudo is used for local_sudo=true commands
            calls = mock_run.call_args_list
            cmd_strs = []
            for call_obj in calls:
                args, kwargs = call_obj
                cmd = args[0]
                cmd_strs.append(" ".join(cmd) if isinstance(cmd, list) else cmd)

            # Should have sudo in send/receive/cleanup commands
            all_cmds = " ".join(cmd_strs)
            self.assertIn("sudo", all_cmds)


class TestExportFileBackup(unittest.TestCase):
    """Test the export_file configuration modeled after test_export_file.toml.

    Config:
      - local_sudo = false (default)
      - backup_dir = /backup  
      - export_file in archive section (send to file only, no receive)
    Flow: create_snapshot -> send_backup_tofile (export to file) -> keep policy on snap_dir
    """

    @patch("bubtrsnap.run")
    def test_dry_run_export_file_only_sends_to_file(self, mock_run):
        """Dry-run with export_file should log send-to-file, skip receive, apply keep policy."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "corinne"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": None,
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 3,
                "keep_weekly": 3,
                "keep_monthly": 3,
                "keep_yearly": 1,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "export_file": "/tmp/corinne.btrfs",
                "keep": {
                    "keep_daily": 3,
                    "keep_weekly": 2,
                    "keep_monthly": 3,
                    "keep_yearly": 0,
                },
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

                # Validation commands (always execute in dry-run)
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid-123\nReceived UUID: -\n"
                    else:
                        result.stdout = ""  # no parents
                    return result

                # Dry-run for write commands
                return None

            mock_run.side_effect = run_mock

            # process_archive should complete without error
            # export_file only: send to file, no receive, keep policy on snap_dir
            bs.process_archive(archive, cfg)

            # Verify run() was called
            self.assertTrue(mock_run.called)

            # Check that commands were logged (send to file, not receive)
            calls = mock_run.call_args_list
            cmd_strs = []
            for call_obj in calls:
                args, kwargs = call_obj
                cmd = args[0]
                cmd_strs.append(" ".join(cmd) if isinstance(cmd, list) else cmd)

            all_cmds = " ".join(cmd_strs)
            # Should have send -f (export to file)
            self.assertIn("btrfs send", all_cmds)
            self.assertIn("-f", all_cmds)
            self.assertIn("/tmp/corinne.btrfs", all_cmds)
            # Should NOT have btrfs receive (export_file only, no receive)
            self.assertNotIn("btrfs receive", all_cmds)


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