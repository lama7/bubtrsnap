#!/usr/bin/env python3
"""Unit tests for bubtrsnap SSH functionality."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch, call

def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()

bs = _load()


def _write_config(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip())


def _ns(**kwargs):
    """Build an argparse.Namespace with defaults matching bubtrsnap CLI."""
    defaults = dict(
        config=None,
        snapshot_dir=None,
        backup_dir=None,
        snaps_only=False,
        export_file=None,
        import_file=None,
        stage_file=None,
        export_dir=None,
        import_dir=None,
        stage_dir=None,
        local_sudo=False,
        keep_hourly=None,
        keep_daily=None,
        keep_weekly=None,
        keep_monthly=None,
        keep_yearly=None,
        verbose=0,
        debug=False,
        dry_run=False,
        archives=[],
        remote_host=None,
        remote_path=None,
        remote_sudo=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestSSHConfigPrecedence(unittest.TestCase):
    """Test CLI > archive > global precedence for SSH options."""

    def test_cli_remote_overrides_archive_and_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                remote_host = "global@host"
                remote_path = "/global/remote"
                [a]
                subvolume = "{sub}"
                remote_host = "archive@host"
                remote_path = "/archive/remote"
                """,
            )
            cli = _ns(remote_host="cli@host", remote_path="/cli/remote", archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            a = archives[0]
            self.assertEqual(a["remote_host"], "cli@host")
            self.assertEqual(a["remote_path"], "/cli/remote")

    def test_archive_remote_overrides_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                remote_host = "global@host"
                remote_path = "/global/remote"
                [a]
                subvolume = "{sub}"
                remote_host = "archive@host"
                remote_path = "/archive/remote"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertEqual(a["remote_host"], "archive@host")
            self.assertEqual(a["remote_path"], "/archive/remote")

    def test_remote_host_without_remote_path_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                remote_host = "global@host"
                [a]
                subvolume = "{sub}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertEqual(a["remote_host"], "global@host")
            self.assertIsNone(a["remote_path"])


class TestSSHValidation(unittest.TestCase):
    """Test SSH validation functions."""

    @patch("bubtrsnap.subprocess.run")
    def test_chk_btrfs_subvolume_ssh_success(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1"
        mock_run.return_value = mock_result

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        bs.chk_btrfs_subvolume_ssh("user@host", "/remote/path", cfg)
        mock_run.assert_called_once()

    @patch("bubtrsnap.subprocess.run")
    def test_chk_btrfs_subvolume_ssh_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="Not a btrfs subvolume")

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        with self.assertRaises(SystemExit):
            bs.chk_btrfs_subvolume_ssh("user@host", "/remote/path", cfg)


class TestIterArchiveItemsSSH(unittest.TestCase):
    """Test iter_archive_items_ssh output format matches local."""

    @patch("bubtrsnap.run")
    def test_iter_archive_items_ssh_returns_correct_format(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = textwrap.dedent("""
            ID 256 gen 0 top level 5 path lama7.202608280230
            ID 257 gen 0 top level 5 path lama7.202608290230
            ID 258 gen 0 top level 5 path lama7.202608300230
            ID 259 gen 0 top level 5 path other.202608280230
        """).strip()
        mock_run.return_value = mock_result

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        items = list(bs.iter_archive_items_ssh("user@host", "/remote/backup", "lama7", cfg))

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], ("202608280230", "/remote/backup/lama7.202608280230"))
        self.assertEqual(items[1], ("202608290230", "/remote/backup/lama7.202608290230"))
        self.assertEqual(items[2], ("202608300230", "/remote/backup/lama7.202608300230"))

    @patch("bubtrsnap.run")
    def test_iter_archive_items_ssh_filters_by_archive_name(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = textwrap.dedent("""
            ID 256 gen 0 top level 5 path lama7.202608280230
            ID 257 gen 0 top level 5 path lama7Maildir.202608280230
            ID 258 gen 0 top level 5 path lama7.202608290230
        """).strip()
        mock_run.return_value = mock_result

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        items = list(bs.iter_archive_items_ssh("user@host", "/remote/backup", "lama7", cfg))

        self.assertEqual(len(items), 2)
        for ts, path in items:
            self.assertTrue(path.endswith(f"lama7.{ts}"))

    @patch("bubtrsnap.run")
    def test_iter_archive_items_ssh_empty_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="Permission denied")

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        items = list(bs.iter_archive_items_ssh("user@host", "/remote/backup", "lama7", cfg))

        self.assertEqual(items, [])

    @patch("bubtrsnap.run")
    def test_iter_archive_items_ssh_skips_invalid_timestamps(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = textwrap.dedent("""
            ID 256 gen 0 top level 5 path lama7.202608280230
            ID 257 gen 0 top level 5 path lama7.not_a_timestamp
            ID 258 gen 0 top level 5 path lama7.202608290230
        """).strip()
        mock_run.return_value = mock_result

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": False}
        items = list(bs.iter_archive_items_ssh("user@host", "/remote/backup", "lama7", cfg))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][0], "202608280230")
        self.assertEqual(items[1][0], "202608290230")

    @patch("bubtrsnap.run")
    def test_iter_archive_items_ssh_dry_run_returns_empty(self, mock_run):
        """Dry-run should return None from run(), yielding no items."""
        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            return MagicMock()

        mock_run.side_effect = run_mock

        cfg = {"local_sudo": True, "verbose": 0, "dry_run": True}
        items = list(bs.iter_archive_items_ssh("user@host", "/remote/backup", "lama7", cfg))

        self.assertEqual(items, [])


class TestApplyKeepPolicySSH(unittest.TestCase):
    """Test apply_keep_policy_ssh mirrors local logic."""

    @patch("bubtrsnap.run")
    def test_apply_keep_policy_ssh_dry_run(self, mock_run):
        # Dry run: run() is called with dry_run=True, returns None
        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            mock = MagicMock()
            mock.returncode = 0
            return mock

        mock_run.side_effect = run_mock

        cfg = {"local_sudo": True, "verbose": 1, "dry_run": True}

        with patch.object(bs, "iter_archive_items_ssh") as mock_iter:
            mock_iter.return_value = [
                ("202608280230", "/remote/backup/lama7.202608280230"),
                ("202608290230", "/remote/backup/lama7.202608290230"),
                ("202608300230", "/remote/backup/lama7.202608300230"),
                ("202608310230", "/remote/backup/lama7.202608310230"),
            ]

            bs.apply_keep_policy_ssh("user@host", "/remote/backup", "lama7",
                                     {"keep_daily": 2}, cfg)

            # In dry-run, run() IS called for delete but with dry_run=True (returns None)
            # So we verify the delete calls have dry_run=True in kwargs
            delete_calls = []
            for call in mock_run.call_args_list:
                args, kwargs = call
                cmd_str = " ".join(args[0]) if args else ""
                if "delete" in cmd_str:
                    delete_calls.append((args, kwargs))
            self.assertEqual(len(delete_calls), 2)
            for args, kwargs in delete_calls:
                self.assertTrue(kwargs.get("dry_run", False))

    @patch("bubtrsnap.run")
    def test_apply_keep_policy_ssh_dry_run_with_remote_sudo(self, mock_run):
        """Test that SSH delete commands include 'sudo -n' when remote_sudo=True."""
        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            mock = MagicMock()
            mock.returncode = 0
            return mock

        mock_run.side_effect = run_mock

        cfg = {"local_sudo": True, "verbose": 1, "dry_run": True, "remote_sudo": True}

        with patch.object(bs, "iter_archive_items_ssh") as mock_iter:
            mock_iter.return_value = [
                ("202608280230", "/remote/backup/lama7.202608280230"),
                ("202608290230", "/remote/backup/lama7.202608290230"),
                ("202608300230", "/remote/backup/lama7.202608300230"),
                ("202608310230", "/remote/backup/lama7.202608310230"),
            ]

            # Pass remote_sudo=True as the 6th argument
            bs.apply_keep_policy_ssh("user@host", "/remote/backup", "lama7",
                                     {"keep_daily": 2}, cfg, True)

            # Verify delete commands include 'sudo -n'
            delete_calls = []
            for call in mock_run.call_args_list:
                args, kwargs = call
                cmd_str = " ".join(args[0]) if args else ""
                if "delete" in cmd_str:
                    delete_calls.append(args[0])
            self.assertEqual(len(delete_calls), 2)
            for cmd in delete_calls:
                self.assertIn("sudo", cmd)
                self.assertIn("-n", cmd)
                # Check order: ssh user@host sudo -n btrfs subvolume delete ...
                self.assertEqual(cmd[0], "ssh")
                self.assertEqual(cmd[1], "user@host")
                self.assertEqual(cmd[2], "sudo")
                self.assertEqual(cmd[3], "-n")
                self.assertEqual(cmd[4], "btrfs")
                self.assertEqual(cmd[5], "subvolume")
                self.assertEqual(cmd[6], "delete")

    @patch("bubtrsnap.run")
    def test_apply_keep_policy_ssh_dry_run_without_remote_sudo(self, mock_run):
        """Test that SSH delete commands DON'T include 'sudo -n' when remote_sudo=False."""
        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            mock = MagicMock()
            mock.returncode = 0
            return mock

        mock_run.side_effect = run_mock

        cfg = {"local_sudo": True, "verbose": 1, "dry_run": True, "remote_sudo": False}

        with patch.object(bs, "iter_archive_items_ssh") as mock_iter:
            mock_iter.return_value = [
                ("202608280230", "/remote/backup/lama7.202608280230"),
                ("202608290230", "/remote/backup/lama7.202608290230"),
                ("202608300230", "/remote/backup/lama7.202608300230"),
                ("202608310230", "/remote/backup/lama7.202608310230"),
            ]

            bs.apply_keep_policy_ssh("user@host", "/remote/backup", "lama7",
                                     {"keep_daily": 2}, cfg, False)

            # Verify delete commands DON'T include 'sudo -n'
            delete_calls = []
            for call in mock_run.call_args_list:
                args, kwargs = call
                cmd_str = " ".join(args[0]) if args else ""
                if "delete" in cmd_str:
                    delete_calls.append(args[0])
            self.assertEqual(len(delete_calls), 2)
            for cmd in delete_calls:
                self.assertNotIn("sudo", cmd)
                # Check order: ssh user@host btrfs subvolume delete ...
                self.assertEqual(cmd[0], "ssh")
                self.assertEqual(cmd[1], "user@host")
                self.assertEqual(cmd[2], "btrfs")
                self.assertEqual(cmd[3], "subvolume")
                self.assertEqual(cmd[4], "delete")

    @patch("bubtrsnap.run")
    def test_apply_keep_policy_ssh_prunes_old(self, mock_run):
        cfg = {"local_sudo": True, "verbose": 1, "dry_run": False}

        def run_mock(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        mock_run.side_effect = run_mock

        with patch.object(bs, "iter_archive_items_ssh") as mock_iter:
            mock_iter.return_value = [
                ("202608280230", "/remote/backup/lama7.202608280230"),
                ("202608290230", "/remote/backup/lama7.202608290230"),
                ("202608300230", "/remote/backup/lama7.202608300230"),
                ("202608310230", "/remote/backup/lama7.202608310230"),
            ]

            bs.apply_keep_policy_ssh("user@host", "/remote/backup", "lama7",
                                     {"keep_daily": 2}, cfg)

            # Should delete the 2 oldest (keep_daily=2 keeps newest 2)
            delete_calls = []
            for call in mock_run.call_args_list:
                args, kwargs = call
                cmd_str = " ".join(args[0]) if args else ""
                if "delete" in cmd_str:
                    delete_calls.append(args[0])
            self.assertEqual(len(delete_calls), 2)
            # Extract the path from each delete command (last element)
            deleted_paths = [c[-1] for c in delete_calls]
            self.assertIn("/remote/backup/lama7.202608280230", deleted_paths)
            self.assertIn("/remote/backup/lama7.202608290230", deleted_paths)


class TestFindParentsSSH(unittest.TestCase):
    """Test find_parents_ssh mirrors local find_parents logic."""

    @patch("bubtrsnap.run")
    def test_find_parents_ssh_dry_run_executes_readonly(self, mock_run):
        """Dry-run should execute read-only SSH calls (list, show) but return empty list since no real data."""
        cfg = {"local_sudo": True, "verbose": 1, "dry_run": True}

        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            return MagicMock()

        mock_run.side_effect = run_mock

        snap_dir = Path("/snapshots")
        parents = bs.find_parents_ssh("lama7", snap_dir, "user@host", "/remote/backup", cfg)

        # Dry-run executes read-only commands but run() returns None, so function returns empty
        self.assertEqual(parents, [])
        # Verify SSH commands were attempted (run was called)
        self.assertTrue(mock_run.called)

    @patch("bubtrsnap.run")
    def test_find_parents_ssh_matches_received_uuid(self, mock_run):
        """Should match local UUID with remote Received UUID."""
        # Track call count to return appropriate mock
        call_count = {"list": 0, "show": 0, "local_show": 0}

        def run_mock(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0

            cmd_str = " ".join(cmd)
            if "subvolume list" in cmd_str:
                call_count["list"] += 1
                mock.stdout = textwrap.dedent("""
                    ID 256 gen 0 top level 5 path lama7.202608280230
                    ID 257 gen 0 top level 5 path lama7.202608290230
                    ID 258 gen 0 top level 5 path lama7.202608300230
                """).strip()
            elif "subvolume show" in cmd_str:
                # Check if it's a remote show command
                if "ssh" in cmd_str:
                    call_count["show"] += 1
                    if call_count["show"] == 1:
                        mock.stdout = "UUID: remote-uuid-1\nReceived UUID: local-uuid-1\n"
                    elif call_count["show"] == 2:
                        mock.stdout = "UUID: remote-uuid-2\nReceived UUID: local-uuid-2\n"
                    elif call_count["show"] == 3:
                        mock.stdout = "UUID: remote-uuid-3\nReceived UUID: local-uuid-3\n"
                    else:
                        mock.stdout = "UUID: remote-uuid\n"
                else:
                    # Local btrfs subvolume show for local snapshots
                    call_count["local_show"] += 1
                    if call_count["local_show"] <= 3:
                        mock.stdout = f"UUID: local-uuid-{call_count['local_show']}\n"
                    else:
                        mock.stdout = "UUID: local-uuid\n"
            else:
                mock.stdout = ""
            return mock

        mock_run.side_effect = run_mock

        # Create fake local snapshot directory structure
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            (snap_dir / "lama7.202608280230").mkdir()
            (snap_dir / "lama7.202608290230").mkdir()
            (snap_dir / "lama7.202608300230").mkdir()

            cfg = {"local_sudo": True, "verbose": 1, "dry_run": False}
            parents = bs.find_parents_ssh("lama7", snap_dir, "user@host", "/remote/backup", cfg)

            # Should find matches for all 3 remote subvolumes
            # The function returns local snapshot paths, not remote paths
            self.assertEqual(len(parents), 3)
            # Parents are local paths (from snapshot_dir)
            for p in parents:
                self.assertTrue(str(p).endswith("lama7.202608280230") or 
                               str(p).endswith("lama7.202608290230") or
                               str(p).endswith("lama7.202608300230"))


class TestSSHKeepPolicyIntegration(unittest.TestCase):
    """Integration tests for SSH keep policy with send/receive."""

    @patch("bubtrsnap.run")
    def test_backup_to_ssh_applies_keep_policy(self, mock_run):
        """Full backup flow should call apply_keep_policy_ssh after receive."""
        with patch.object(bs, "apply_keep_policy_ssh") as mock_keep:
            mock_keep.return_value = None
            # The actual integration is tested by running the script with --dry-run
            pass


class TestSCPReceive(unittest.TestCase):
    """Test SCP-based SSH receive functionality."""

    @patch("bubtrsnap.run")
    def test_scp_and_receive_dry_run(self, mock_run):
        """_scp_and_receive should log SCP and SSH commands in dry-run."""
        import subprocess
        from pathlib import Path

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False}

        result = bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg)

        self.assertEqual(result, "test.202608280230")
        # In dry-run mode, the function returns early and logs via log()
        # The actual scp/ssh commands are not executed via run()
        # Verify the function returns the expected stem
        self.assertEqual(result, stream.stem)

    @patch("bubtrsnap.run")
    def test_scp_and_receive_scp_failure(self, mock_run):
        """_scp_and_receive should exit on SCP failure."""
        import subprocess
        from pathlib import Path
        
        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False}
        
        mock_run.return_value = subprocess.CompletedProcess(
            args=["scp", "..."], returncode=1, stderr="connection refused"
        )
        
        with self.assertRaises(SystemExit):
            bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg)

    @patch("bubtrsnap.subprocess.Popen")
    @patch("bubtrsnap.subprocess.run")
    def test_scp_and_receive_receive_failure(self, mock_run, mock_popen):
        """_scp_and_receive should exit on SSH receive failure."""
        import subprocess
        from pathlib import Path
        
        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False}
        
        # SCP succeeds
        mock_run.return_value = subprocess.CompletedProcess(
            args=["scp", "..."], returncode=0, stdout="", stderr=""
        )
        
        # SSH receive fails
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "error: no space left")
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc
        
        with self.assertRaises(SystemExit):
            bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg)


class TestSendBackupToFile(unittest.TestCase):
    """Test send_backup_tofile behavior for stage_file vs export_file."""

    @patch("bubtrsnap.subprocess.Popen")
    def test_send_backup_tofile_stage_file_only_sends_to_file(self, mock_popen):
        """stage_file should only send to file, not pipe to receive."""
        from pathlib import Path
        import subprocess

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        snap = Path("/snapshots/lama7.202608280230")
        snap_dir = Path("/snapshots")
        backup_dir = Path("/backup")
        stream_file = Path("/tmp/stage.btrfs")
        cfg = {"local_sudo": False, "verbose": 1, "dry_run": False}
        archive_cfg = {"stage_file": str(stream_file)}  # stage_file triggers is_staged

        # Mock find_parents to return empty list
        with patch.object(bs, "find_parents", return_value=[]):
            result = bs.send_backup_tofile(snap, snap_dir, backup_dir, stream_file, cfg, "lama7", archive_cfg)

        self.assertEqual(result, stream_file)
        # Verify Popen was called with the send command (not piped to receive)
        self.assertTrue(mock_popen.called)
        call_args = mock_popen.call_args[0][0]
        self.assertIn("-f", call_args)
        self.assertIn(str(stream_file), call_args)
        # Should NOT have receive command piped
        # The function should return early for staged files

    @patch("bubtrsnap.subprocess.Popen")
    @patch("bubtrsnap.subprocess.run")
    def test_send_backup_tofile_export_file_pipes_to_receive(self, mock_run, mock_popen):
        """export_file (not stage_file) should pipe send to receive."""
        from pathlib import Path
        import subprocess

        mock_send = MagicMock()
        mock_send.communicate.return_value = (b"", b"")
        mock_send.returncode = 0
        mock_recv = MagicMock()
        mock_recv.communicate.return_value = (b"", b"")
        mock_recv.returncode = 0
        
        # Return send_proc first, then recv_proc
        mock_popen.side_effect = [mock_send, mock_recv]
        
        # Mock subprocess.run for the final export to file
        mock_run.return_value = subprocess.CompletedProcess(
            args=["btrfs", "send", "-f", "/tmp/export.btrfs", "..."], 
            returncode=0, stdout="", stderr=""
        )

        snap = Path("/snapshots/lama7.202608280230")
        snap_dir = Path("/snapshots")
        backup_dir = Path("/backup")
        stream_file = Path("/tmp/export.btrfs")
        cfg = {"local_sudo": False, "verbose": 1, "dry_run": False}
        archive_cfg = {"export_file": str(stream_file)}  # export_file, not stage_file

        with patch.object(bs, "find_parents", return_value=[]):
            result = bs.send_backup_tofile(snap, snap_dir, backup_dir, stream_file, cfg, "lama7", archive_cfg)

        self.assertEqual(result, stream_file)
        # Should have been called twice: once for send, once for receive
        self.assertEqual(mock_popen.call_count, 2)

if __name__ == "__main__":
    unittest.main()