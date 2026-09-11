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


class TestExportDirBackup(unittest.TestCase):
    """Test the export_dir configuration modeled after test_export_dir.toml.

    Config:
      - local_sudo = true (global)
      - remote_host = "gerry@thorin" (global, no remote_path)
      - export_dir = ~/btrfsstreams (global)
      - Archive: subvolume + keep policy (archive-level)
    Flow: create_snapshot -> send_backup_tofile (export_dir) -> keep policy
    """

    @patch("bubtrsnap.run")
    def test_dry_run_export_dir_sends_to_file_stream(self, mock_run):
        """Dry-run with export_dir should log btrfs send -f with stream file path."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)
            export_dir = snap_dir / "btrfsstreams"
            export_dir.mkdir()

            archive_name = "lama7"
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
                "export_dir": str(export_dir),
                "remote_host": "gerry@thorin",
                "keep": {
                    "keep_daily": 5,
                    "keep_weekly": 3,
                    "keep_monthly": 2,
                },
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

            bs.process_archive(archive, cfg)

            # Verify send command includes -f and stream path
            calls = mock_run.call_args_list
            cmd_strs = [" ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0] for c in calls]
            all_cmds = " ".join(cmd_strs)
            self.assertIn("btrfs send", all_cmds)
            self.assertIn("-f", all_cmds)
            self.assertIn(str(export_dir), all_cmds)


class TestImportDirBackup(unittest.TestCase):
    """Test the import_dir configuration modeled after test_import_dir.toml.

    Config:
      - local_sudo = true (global)
      - remote_host = "gerry@thorin" (global, no remote_path)
      - import_dir = ~/btrfsstreams (global)
      - Archive: subvolume + keep policy (archive-level)
    Flow: check stream file in import_dir -> receive_stream -> keep policy
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_import_dir_receives_from_file(self, mock_run, mock_stream):
        """Dry-run with import_dir should log btrfs receive -f from stream file."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)
            import_dir = snap_dir / "btrfsstreams"
            import_dir.mkdir()

            # Create a fake stream file in import_dir
            stream_file = import_dir / "lama7Maildir.202601011200.btrfs"
            stream_file.write_bytes(b"fake-btrfs-stream")

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": None,
                "remote_sudo": False,
                "keep_daily": 3,
                "keep_weekly": 3,
                "keep_monthly": 3,
                "keep_yearly": 1,
                "week_startday": "sunday",
            }

            archive = {
                "name": "lama7Maildir",
                "subvolume": str(snap_dir / "lama7Maildir"),
                "import_dir": str(import_dir),
                "remote_host": "gerry@thorin",
                "keep": {
                    "keep_daily": 5,
                    "keep_weekly": 3,
                    "keep_monthly": 2,
                },
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

            # Mock find_newest_stream_for_archive to return the stream file
            # (dry-run prevents actual file reading via dd/sudo)
            with patch.object(bs, "find_newest_stream_for_archive", return_value=stream_file):
                bs.process_archive(archive, cfg)

            # Verify receive command was called
            calls = mock_run.call_args_list
            cmd_strs = [" ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0] for c in calls]
            all_cmds = " ".join(cmd_strs)
            self.assertIn("btrfs receive", all_cmds)
            self.assertIn("-f", all_cmds)
            self.assertIn(str(stream_file), all_cmds)


class TestExportImportDirBackup(unittest.TestCase):
    """Test the import_dir + export_dir configuration (staged).

    Config:
      - local_sudo = true (global)
      - remote_host = "gerry@thorin" (global, no remote_path)
      - export_dir = ~/btrfsstreams (same as import_dir)
      - import_dir = ~/btrfsstreams (same as export_dir)
      - Archive: subvolume + keep policy (archive-level)
    Flow: create_snapshot -> send_backup_tofile (export_dir) ->
          find_newest_stream_for_archive -> receive_stream -> cleanup (no stage_dir cleanup)
    """

    @patch("bubtrsnap.stream_snapshot_name", return_value="lama7Maildir.202601011200")
    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_export_and_import_dir_staged_flow(self, mock_run, mock_stream, mock_snap_name):
        """Dry-run with export_dir + import_dir should send to file then receive from file."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)
            stage_dir = snap_dir / "btrfsstreams"
            stage_dir.mkdir()

            archive_name = "lama7Maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            # Pre-create the stream file in the stage_dir so find_newest_stream_for_archive finds it
            stream_file = stage_dir / f"{snap_name}.btrfs"
            stream_file.write_bytes(b"fake-btrfs-stream")

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
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
                "export_dir": str(stage_dir),
                "import_dir": str(stage_dir),
                "remote_host": "gerry@thorin",
                "keep": {
                    "keep_daily": 5,
                    "keep_weekly": 3,
                    "keep_monthly": 2,
                },
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

            bs.process_archive(archive, cfg)

            # Verify both send and receive were called
            calls = mock_run.call_args_list
            cmd_strs = [" ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0] for c in calls]
            all_cmds = " ".join(cmd_strs)
            # Should have send -f (write stream to file)
            self.assertIn("btrfs send", all_cmds)
            self.assertIn("-f", all_cmds)
            # Should have receive -f (read stream from file)
            self.assertIn("btrfs receive", all_cmds)


class TestRemoteExportFileBackup(unittest.TestCase):
    """Test the remote + export_file configuration modeled after test_remote_w_export_file.toml.
      - local_sudo = true (global)
      - remote_host = "gerry@thorin" (global)
      - remote_sudo = true (global)
      - backup_dir = /backup (global)
      - Archive: subvolume, remote_path = /run/media/gerry/backup, export_file = ~/lama7Maildir.btrfs
    Flow: send_backup_tofile (export to file + SCP to remote) AND
          normal piped send|receive to local backup_dir
    """

    @patch("bubtrsnap.run")
    def test_dry_run_export_file_with_remote_and_local(self, mock_run):
        """Dry-run with export_file, backup_dir, and remote should do both file export and local piped send.

        The send_backup_tofile function handles the file-based export + SCP to remote
        via run() calls. The local piped send uses piped_run() (Popen), which logs
        directly to stdout. We capture stdout to verify both flows appear.
        """
        from io import StringIO
        import sys

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "lama7Maildir"
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
                "remote_path": "/run/media/gerry/backup",
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
                "remote_path": "/run/media/gerry/backup",
                "export_file": str(backup_dir / f"{archive_name}.btrfs"),
                "keep": {
                    "keep_daily": 7,
                    "keep_monthly": 1,
                    "keep_yearly": 1,
                },
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

            # Capture stdout to verify piped_run logging (not captured by mock_run)
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured

            try:
                bs.process_archive(archive, cfg)
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            cmd_lines = output.split("\n")

            # Should have send to file (export_file with -f) via run() mock
            run_cmds = " ".join(
                " ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0]
                for c in mock_run.call_args_list
            )
            self.assertIn("btrfs send", run_cmds)
            self.assertIn("-f", run_cmds)

            # Should have piped send to local backup_dir (via piped_run, captured in stdout)
            # Look for the pipe pattern: send ... | receive ...
            pipe_lines = [l for l in cmd_lines if "|" in l and "btrfs send" in l and "btrfs receive" in l]
            self.assertTrue(len(pipe_lines) >= 1, f"Expected piped send|receive in output, got:\n{output}")

            # The piped send should go to the local backup_dir, not remote
            local_pipe = [l for l in pipe_lines if str(backup_dir) in l and "ssh" not in l]
            self.assertTrue(len(local_pipe) >= 1, f"Expected local piped send|receive to {backup_dir}, got: {output}")

            # Should NOT have a piped send|receive to the SSH remote (that's handled by file transfer)
            ssh_pipe = [l for l in pipe_lines if "ssh" in l and "btrfs receive" in l]
            self.assertEqual(len(ssh_pipe), 0, f"Should not have piped SSH send, got: {output}")

            # Verify timing is logged for piped send/receive
            self.assertIn("completed in", output,
                          f"Expected timing log in output: {output}")


class TestScpLocalSudo(unittest.TestCase):
    """Test that scp command gets local_sudo prefix when local_sudo=true.

    Regression test for bug where _scp_and_receive built scp_cmd without
    checking cfg.get("local_sudo"). When btrfs send -f runs as root (due to
    local_sudo), the stream file is root-owned, so scp must also use sudo.
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_scp_with_local_sudo_shows_sudo_prefix(self, mock_run, mock_stream):
        """Dry-run with local_sudo=true + remote should prefix scp with sudo -n."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
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
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            stream_file = backup_dir / f"{archive_name}.btrfs"

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "export_file": str(stream_file),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                # For all other commands (dry-run), return None without logging
                return None

            mock_run.side_effect = run_mock

            # Capture stdout to verify timing logs
            from io import StringIO
            import sys as sys_mod
            old_stdout = sys_mod.stdout
            captured = StringIO()
            sys_mod.stdout = captured
            try:
                bs.process_archive(archive, cfg)
            finally:
                sys_mod.stdout = old_stdout

            # Verify that run() was called with scp command prefixed with sudo -n
            all_calls = mock_run.call_args_list
            scp_calls = [
                c for c in all_calls
                if isinstance(c.args[0], list) and c.args[0][0] == "sudo" and "scp" in c.args[0]
            ]
            self.assertTrue(len(scp_calls) > 0, "Expected scp command with sudo -n prefix")

            # Verify timing is logged for SCP + receive sequence
            output = captured.getvalue()
            self.assertIn("SCP + receive sequence completed in", output,
                          f"Expected SCP+receive timing log in output")


class TestScpLocalSudoFalse(unittest.TestCase):
    """Test that scp command does NOT get local_sudo prefix when local_sudo=false.

    Counterpart to TestScpLocalSudo — ensures we don't blindly prefix scp
    with sudo when local_sudo is not set.
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_scp_without_local_sudo_no_sudo_prefix(self, mock_run, mock_stream):
        """Dry-run with local_sudo=false should NOT prefix scp with sudo -n."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            stream_file = backup_dir / f"{archive_name}.btrfs"

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "export_file": str(stream_file),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            bs.process_archive(archive, cfg)

            # Verify that run() was called with an scp command but NOT prefixed with sudo
            all_calls = mock_run.call_args_list
            scp_calls = [
                c for c in all_calls
                if isinstance(c.args[0], list) and "scp" in c.args[0]
            ]
            self.assertTrue(len(scp_calls) > 0, "Expected scp command to be called")
            for c in scp_calls:
                self.assertNotEqual(c.args[0][0], "sudo",
                                    f"scp should not be prefixed with sudo when local_sudo=false")


class TestReceiveStreamReturnsSubvolName(unittest.TestCase):
    """Test that receive_stream returns the received subvolume name on success.

    Regression test for bug where receive_stream's local mode returned None
    on success (fixed in commit 3937cef). When send_backup returns None,
    process_archive skips apply_keep_policy on backup_dir.
    """

    @patch("bubtrsnap.run")
    def test_receive_stream_local_returns_stem(self, mock_run):
        """receive_stream in local mode should return stream.stem on success."""

        with tempfile.TemporaryDirectory() as backup_td:
            backup_dir = Path(backup_td)
            stream_file = Path("/tmp/test_receive.btrfs")

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                # Simulate successful receive (no read_only validation needed)
                result = MagicMock()
                result.returncode = 0
                return result

            mock_run.side_effect = run_mock

            result = bs.receive_stream(stream_file, backup_dir, cfg={"dry_run": False, "local_sudo": False, "verbose": 2})

            # Should return the stream file stem, not None
            self.assertEqual(result, "test_receive")
            self.assertIsNotNone(result, "receive_stream must return subvol name on success")


class TestExportImportNoLocalPipedSend(unittest.TestCase):
    """Test that export_file + import_file does NOT do a local piped send->receive to backup_dir.

    Regression test for bug where process_archive's local piped send block
    ran even when recv_file (import_file) was set, causing unnecessary
    send|receive to backup_dir. The condition should be:
    'if backup_dir and not recv_file' — not just 'if backup_dir'.
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.find_newest_stream_for_archive", return_value=Path("/tmp/test.btrfs"))
    @patch("bubtrsnap.run")
    def test_dry_run_export_and_import_no_local_pipe(self, mock_run, mock_find, mock_stream):
        """With both export_file and import_file set, no local piped send|receive should appear."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            export_file = snap_dir / f"{archive_name}.btrfs"
            import_file = snap_dir / f"{archive_name}.imported.btrfs"

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
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "export_file": str(export_file),
                "import_file": str(import_file),
                "backup_dir": str(backup_dir),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            from io import StringIO
            import sys
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured
            try:
                bs.process_archive(archive, cfg)
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            cmd_lines = output.split("\n")

            # Should NOT have a local piped send|receive (to backup_dir without ssh)
            local_pipe_lines = [
                l for l in cmd_lines
                if "|" in l and "btrfs send" in l and "btrfs receive" in l and "ssh" not in l
            ]
            self.assertEqual(len(local_pipe_lines), 0,
                           f"Should not have local piped send|receive when import_file is set: {local_pipe_lines}")


class TestExportFileRemoteNoDuplicateSend(unittest.TestCase):
    """Regression test for duplicate btrfs send command in send_backup_tofile.

    When export_file + backup_dir + remote_host + remote_path are all set,
    send_backup_tofile used to run ssh_send_cmd (a second local 'btrfs send -f'
    to the same stream file) in addition to local_send_cmd. In dry-run this was
    masked (run() returns None → early return), but in live execution the
    duplicate send actually ran. The SSH transfer is handled separately by
    _scp_and_receive in the do_receive flow.
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_export_file_remote_calls_send_to_file_once(self, mock_run, mock_stream):
        """With export_file + remote + backup_dir, send_backup_tofile should call
        btrfs send -f exactly once (not twice for local + ssh_send)."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            export_file = backup_dir / f"{archive_name}.btrfs"

            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "export_file": str(export_file),
                "backup_dir": str(backup_dir),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            bs.process_archive(archive, cfg)

            # Count btrfs send -f calls (send to file)
            send_to_file_calls = 0
            for c in mock_run.call_args_list:
                args, kwargs = c
                cmd = args[0]
                if isinstance(cmd, list) and "btrfs" in cmd and "send" in cmd and "-f" in cmd:
                    send_to_file_calls += 1

            self.assertEqual(send_to_file_calls, 1,
                             f"Expected exactly 1 'btrfs send -f' call, got {send_to_file_calls}")

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_export_file_remote_uses_ssh_parents_for_send(self, mock_run, mock_stream):
        """When remote is configured, send_backup_tofile should use find_parents_ssh
        (remote parents) not find_parents (local backup_dir parents) for the stream file.

        This ensures the stream file's incremental send references subvolumes
        that already exist on the remote, so btrfs receive on remote won't fail.
        """
        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            export_file = backup_dir / f"{archive_name}.btrfs"

            cfg = {
                "local_sudo": False,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "export_file": str(export_file),
                "backup_dir": str(backup_dir),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            with patch.object(bs, "find_parents_ssh") as mock_ssh, \
                 patch.object(bs, "find_parents") as mock_local:
                # find_parents_ssh returns a mock parent path
                mock_ssh.return_value = [Path("/remote/parent/.snapshots/maildir.202601010000")]
                mock_local.return_value = [Path("/backup/.snapshots/maildir.202601010000")]

                bs.process_archive(archive, cfg)

                # find_parents_ssh should have been called (for the stream file sent to remote)
                self.assertTrue(mock_ssh.called,
                                "find_parents_ssh should be called when remote is configured")
                # find_parents should NOT have been called in send_backup_tofile
                # (it may be called in the local piped send in process_archive, but
                # we verify it wasn't used to build the send -f command)

            # Verify the send-to-file command used SSH parents
            send_cmds = []
            for c in mock_run.call_args_list:
                args, kwargs = c
                cmd = args[0]
                if isinstance(cmd, list) and "btrfs" in cmd and "send" in cmd and "-f" in cmd:
                    send_cmds.append(cmd)

            self.assertEqual(len(send_cmds), 1,
                             f"Expected 1 send -f command, got {len(send_cmds)}")
            send_cmd_str = " ".join(send_cmds[0])
            self.assertIn("/remote/parent", send_cmd_str,
                          "send -f command should use remote parents from find_parents_ssh")

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_ssh_receive_includes_remote_path_as_destination(self, mock_run, mock_stream):
        """The SSH btrfs receive command must include remote_path as destination.

        Regression test for bug where _scp_and_receive built the SSH receive
        command as 'ssh remote sudo -n btrfs receive -f /tmp/stream.btrfs'
        without a destination directory. The correct command is:
        'ssh remote sudo -n btrfs receive -f /tmp/stream.btrfs <remote_path>'.
        Without the destination, btrfs receive doesn't know where to create
        the subvolume on the remote.
        """
        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            export_file = backup_dir / f"{archive_name}.btrfs"

            cfg = {
                "local_sudo": True,
                "verbose": 2,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/run/media/gerry/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/run/media/gerry/backup",
                "remote_sudo": True,
                "export_file": str(export_file),
                "backup_dir": str(backup_dir),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            from io import StringIO
            import sys
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured
            try:
                bs.process_archive(archive, cfg)
            finally:
                sys.stdout = old_stdout

            # Check the mock_run call args for the SSH receive command
            all_calls = mock_run.call_args_list
            ssh_receive_call_found = False
            for c in all_calls:
                args, kwargs = c
                cmd = args[0]
                if isinstance(cmd, list) and "ssh" in cmd and "btrfs" in cmd and "receive" in cmd:
                    ssh_receive_call_found = True
                    cmd_str = " ".join(cmd)
                    self.assertIn("-f", cmd_str)
                    self.assertIn("/run/media/gerry/backup", cmd_str,
                                  "SSH receive must include remote_path as destination")
                    break
            self.assertTrue(ssh_receive_call_found,
                            "Expected an SSH receive command in mock_run calls")


class TestExportFileOverwritesExisting(unittest.TestCase):
    """Test that export_file overwrites an existing stream file instead of erroring.

    Regression test for change where stream_file.exists() check was replaced
    with unconditional overwrite (unlink + send).
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_overwrites_existing_export_file(self, mock_run, mock_stream):
        """When export_file points to an existing file, dry-run should log overwrite + send."""

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            # Pre-create the export file to simulate it already existing
            export_file = backup_dir / f"{archive_name}.btrfs"
            export_file.write_bytes(b"old-stream-data")

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
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "export_file": str(export_file),
                "keep": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0},
            }

            def run_mock(cmd, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "subvolume show" in cmd_str or "subvolume list" in cmd_str:
                    result = MagicMock()
                    result.returncode = 0
                    if "subvolume show" in cmd_str:
                        result.stdout = "UUID: some-uuid\nReceived UUID: -\n"
                    else:
                        result.stdout = ""
                    return result
                return None

            mock_run.side_effect = run_mock

            # Capture stdout to check for "Overwriting" log message
            from io import StringIO
            import sys
            old_stdout = sys.stdout
            captured = StringIO()
            sys.stdout = captured
            try:
                bs.process_archive(archive, cfg)
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            # Should log that we're overwriting the existing file
            self.assertIn("Overwriting existing stream file", output,
                          f"Expected overwrite message in dry-run output: {output}")
            # Verify timing is logged for send-to-file
            self.assertIn("btrfs send to file completed in", output,
                          f"Expected timing log in output: {output}")


class TestNoDuplicateKeepPolicy(unittest.TestCase):
    """Regression test for keep policy being applied twice to snap_dir.

    When export_file + remote + backup_dir are all configured,
    _receive_and_post applies keep policy to snap_dir, backup_dir, and
    remote via _apply_all_keep_policies. The final apply_keep_policy(snap_dir)
    should NOT run again — otherwise snap_dir keep policy is applied twice.
    """

    @patch("bubtrsnap.is_btrfs_stream", return_value=True)
    @patch("bubtrsnap.run")
    def test_dry_run_no_duplicate_keep_policy_on_snap_dir(self, mock_run, mock_stream):
        """With export_file + remote + backup_dir, snap_dir keep policy should run once."""

        from io import StringIO
        import sys as sys_mod

        with tempfile.TemporaryDirectory() as snap_td, tempfile.TemporaryDirectory() as backup_td:
            snap_dir = Path(snap_td)
            backup_dir = Path(backup_td)

            archive_name = "lama7Maildir"
            snap_name = f"{archive_name}.202601011200"
            snap_path = snap_dir / snap_name
            snap_path.mkdir()

            export_file = backup_dir / f"{archive_name}.btrfs"

            cfg = {
                "local_sudo": True,
                "verbose": 1,
                "dry_run": True,
                "snapshot_dir": str(snap_dir),
                "backup_dir": str(backup_dir),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "keep_daily": 3,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
                "week_startday": "sunday",
            }

            archive = {
                "name": archive_name,
                "subvolume": str(snap_path),
                "remote_host": "gerry@thorin",
                "remote_path": "/remote/backup",
                "remote_sudo": True,
                "export_file": str(export_file),
                "backup_dir": str(backup_dir),
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

            old_stdout = sys_mod.stdout
            captured = StringIO()
            sys_mod.stdout = captured
            try:
                bs.process_archive(archive, cfg)
            finally:
                sys_mod.stdout = old_stdout

            output = captured.getvalue()
            # Count how many times keep policy is applied to the snapshot directory
            snap_dir_applies = output.count(f"Applying to {archive_name} in {snap_dir}")
            self.assertEqual(snap_dir_applies, 1,
                             f"Expected keep policy applied once to snap_dir, got {snap_dir_applies}:\\n{output}")


if __name__ == "__main__":
    unittest.main()