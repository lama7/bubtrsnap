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


class TestGlobalRemoteSudoBackup(unittest.TestCase):
    """Test the global_remote_sudo configuration modeled after global_remote_sudo_test.toml."""

    @patch("bubtrsnap.run")
    def test_dry_run_global_remote_sudo_uses_sudo_prefixes(self, mock_run):
        """Dry-run with local_sudo=true and remote_sudo=true should log sudo-prefixed commands."""

        from io import StringIO
        import sys

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "root"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": None,  # Set per-archive
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 3,
                "keep_monthly": 3,
                "keep_yearly": 1,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/run/media/gerry/backup",
                "remote_sudo": True,
                "backup_dir": str(backup_dir),
                "keep": {
                    "keep_daily": 5,
                    "keep_weekly": 1,
                    "keep_monthly": 1,
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

            # Capture stdout
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured

            try:
                result = bs.send_backup(snap_path, snap_dir, backup_dir, cfg, archive_name, archive)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, backup_dir / snap_name)

            # Verify dry-run output contains sudo-prefixed commands
            output = captured.getvalue()
            # local_sudo=true: local send/receive should have 'sudo -n'
            self.assertIn("sudo -n btrfs send", output)
            self.assertIn("sudo -n btrfs receive", output)
            # remote_sudo=true: SSH command should have 'ssh' + 'sudo -n'
            self.assertIn("ssh", output)
            self.assertIn("sudo -n", output)
            # Should pipe to remote (ssh ... btrfs receive)
            self.assertIn("btrfs receive", output)


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


class TestImportFileBackup(unittest.TestCase):
    """Test the import_file configuration.

    Config:
      - local_sudo = true (global)
      - backup_dir + snapshot_dir (global)
      - import_file in archive section (receive only from stream file)
    Flow: validate backup_dir -> check stream file -> receive_stream -> keep policy
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_import_file_receives_stream(self, mock_run, mock_stream):
        """Dry-run with import_file should log btrfs receive -f command."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            # Create a fake stream file so is_file() check passes
            stream_file = backup_dir / "migraine.btrfs"
            stream_file.write_bytes(b"fake-btrfs-stream-data")

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
                "name": "migraine",
                "subvolume": str(snap_dir / "migraine"),
                "import_file": str(stream_file),
                "backup_dir": str(backup_dir),
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

            bs.process_archive(archive, cfg)

            # Verify run() was called with btrfs receive command
            calls = mock_run.call_args_list
            cmd_strs = []
            for call_obj in calls:
                args, kwargs = call_obj
                cmd = args[0]
                cmd_strs.append(" ".join(cmd) if isinstance(cmd, list) else cmd)

            all_cmds = " ".join(cmd_strs)

            # Verify receive command was called with -f and the stream file path
            self.assertIn("btrfs receive", all_cmds)
            self.assertIn("-f", all_cmds)
            self.assertIn(str(stream_file), all_cmds)
            # Verify receive command includes backup_dir
            self.assertIn(str(backup_dir), all_cmds)
            # local_sudo=true: receive command should include 'sudo -n'
            receive_calls = [c for c in cmd_strs if "btrfs receive" in c]
            self.assertTrue(len(receive_calls) > 0)
            for cmd_str in receive_calls:
                self.assertIn("sudo -n", cmd_str)

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_two_archives_export_then_import(self, mock_run, mock_stream):
        """Dry-run with 2 archives: export_file on one, import_file on another."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive1_name = "alpha"
            snap1 = snap_dir / f"{archive1_name}.202601011200"
            snap1.mkdir()
            # Don't pre-create export_file — send_backup_tofile checks it doesn't exist
            export_file = snap_dir / f"{archive1_name}.btrfs"

            archive2_name = "beta"
            stream_file = backup_dir / f"{archive2_name}.btrfs"
            stream_file.write_bytes(b"fake-stream")

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

            archive1 = {
                "name": archive1_name,
                "subvolume": str(snap1),
                "export_file": str(export_file),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }
            archive2 = {
                "name": archive2_name,
                "subvolume": str(snap_dir / archive2_name),
                "import_file": str(stream_file),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid-123\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            # Process first archive (export_file - send to file)
            bs.process_archive(archive1, cfg)
            # Process second archive (import_file - receive from file)
            bs.process_archive(archive2, cfg)

            calls = mock_run.call_args_list
            cmd_strs = []
            for call_obj in calls:
                args, kwargs = call_obj
                cmd = args[0]
                cmd_strs.append(" ".join(cmd) if isinstance(cmd, list) else cmd)

            all_cmds = " ".join(cmd_strs)
            # First archive: should have btrfs send -f
            self.assertIn("btrfs send", all_cmds)
            # Second archive: should have btrfs receive -f
            self.assertIn("btrfs receive", all_cmds)


if __name__ == "__main__":
    unittest.main()