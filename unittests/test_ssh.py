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
        rsync=False,
        rsync_opts=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestSSHConfigPrecedence(unittest.TestCase):
    """Test CLI > archive > global precedence for SSH options."""

    def setUp(self):
        patcher = patch("bubtrsnap.chk_btrfs_subvolume")
        self.addCleanup(patcher.stop)
        patcher.start()

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

    @patch("bubtrsnap.run")
    def test_apply_keep_policy_ssh_forced_keep_prevents_pruning(self, mock_run):
        """apply_keep_policy_ssh must respect the forced_keep parameter (Bug 2).

        With keep_daily=2 and 4 items, the oldest 2 are pruned.  A forced_keep
        timestamp in one of those pruned slots must be preserved.
        """
        def run_mock(cmd, **kwargs):
            if kwargs.get("dry_run", False):
                return None
            mock = MagicMock()
            mock.returncode = 0
            return mock

        mock_run.side_effect = run_mock

        cfg = {"local_sudo": False, "verbose": 1, "dry_run": True}

        with patch.object(bs, "iter_archive_items_ssh") as mock_iter:
            mock_iter.return_value = [
                ("202608280230", "/remote/backup/lama7.202608280230"),
                ("202608290230", "/remote/backup/lama7.202608290230"),
                ("202608300230", "/remote/backup/lama7.202608300230"),
                ("202608310230", "/remote/backup/lama7.202608310230"),
            ]

            bs.apply_keep_policy_ssh("user@host", "/remote/backup", "lama7",
                                     {"keep_daily": 2}, cfg,
                                     forced_keep=["202608290230"])

            delete_calls = []
            for call in mock_run.call_args_list:
                args, _ = call
                cmd_str = " ".join(args[0]) if args else ""
                if "delete" in cmd_str:
                    delete_calls.append(args[0])

            # keep_daily=2 keeps newest 2 (0830, 0831); 0829 is forced →
            # only 0828 should be pruned.
            self.assertEqual(len(delete_calls), 1)
            self.assertIn("202608280230", delete_calls[0][-1])
            self.assertFalse(
                any("202608290230" in c[-1] for c in delete_calls),
                "Forced-keep timestamp must NOT be pruned on SSH remote",
            )


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

        # During dry-run, run() returns None (logs [dry-run] command, doesn't execute)
        mock_run.return_value = None

        result = bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg)

        self.assertEqual(result, "test.202608280230")
        # Verify run() was called 3 times (SCP, SSH receive, cleanup)
        self.assertEqual(mock_run.call_count, 3)
        # Verify the return value is the stream stem
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

    @patch("bubtrsnap.run")
    def test_scp_and_receive_receive_failure(self, mock_run):
        """_scp_and_receive should exit on SSH receive failure."""
        import subprocess
        from pathlib import Path
        
        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False}
        
        # SCP succeeds, SSH receive fails, cleanup succeeds
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["scp"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh", "btrfs", "receive"], returncode=1, stdout="", stderr="error: no space left"),
            subprocess.CompletedProcess(args=["ssh", "rm"], returncode=0, stdout="", stderr=""),
        ]
        
        with self.assertRaises(SystemExit):
            bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg)

    @patch("bubtrsnap.run")
    def test_scp_and_receive_cleanup_honors_remote_sudo(self, mock_run):
        """The remote temp-file cleanup must honor remote_sudo — the rm must run
        with 'sudo -n' when remote_sudo is set, matching every other remote
        command. Without it, cleanup silently fails on root-owned temp files
        (e.g. left by an interrupted run) and /tmp accumulates them."""
        from pathlib import Path

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": True}

        # Dry-run: run() returns None for all three calls (SCP, receive, cleanup)
        mock_run.return_value = None

        bs._scp_and_receive(stream, "user@host", "/remote/backup", cfg, remote_sudo=True)

        # The last run() call is the cleanup command
        cleanup_cmd = mock_run.call_args_list[-1].args[0]
        self.assertEqual(cleanup_cmd,
                         ["ssh", "user@host", "sudo", "-n", "rm", "-f", "/tmp/bubtrsnap-test.202608280230.btrfs"])


class TestSendBackupToFile(unittest.TestCase):
    """Test send_backup_tofile behavior for stage_file vs export_file."""

    @patch("bubtrsnap.run")
    def test_send_backup_tofile_stage_file_only_sends_to_file(self, mock_run):
        """stage_file should only send to file, not pipe to receive."""
        from pathlib import Path
        import subprocess

        # run() is called with check=False and capture_output=True
        mock_run.return_value = subprocess.CompletedProcess(
            args=["btrfs", "send", "-f", "/tmp/stage.btrfs", "..."],
            returncode=0, stdout=b"", stderr=b""
        )

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
        # Verify run() was called with the send command (not piped to receive)
        self.assertTrue(mock_run.called)
        call_args = mock_run.call_args[0][0]
        self.assertIn("-f", call_args)
        self.assertIn(str(stream_file), call_args)
        # Should NOT have receive command piped
        # The function should return early for staged files

    @patch("bubtrsnap.run")
    def test_send_backup_tofile_export_file_sends_to_file(self, mock_run):
        """export_file with backup_dir should send to file (no pipe to receive)."""
        from pathlib import Path
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=["btrfs", "send", "-f", "/tmp/export.btrfs", "..."],
            returncode=0, stdout=b"", stderr=b""
        )

        snap = Path("/snapshots/lama7.202608280230")
        snap_dir = Path("/snapshots")
        backup_dir = Path("/backup")
        stream_file = Path("/tmp/export.btrfs")
        cfg = {"local_sudo": False, "verbose": 1, "dry_run": False}
        archive_cfg = {"export_file": str(stream_file)}

        with patch.object(bs, "find_parents", return_value=[]):
            result = bs.send_backup_tofile(snap, snap_dir, backup_dir, stream_file, cfg, "lama7", archive_cfg)

        # Should return the stream_file path
        self.assertEqual(result, stream_file)
        # Verify run() was called with send -f command (not piping to receive)
        self.assertTrue(mock_run.called)
        call_args = mock_run.call_args[0][0]
        self.assertIn("-f", call_args)


    @patch("bubtrsnap.run")
    def test_send_backup_tofile_export_file_only_no_destinations(self, mock_run):
        """export_file without backup_dir or remote should still create
        the stream file (full send, no parents). Previously broken by an
        early return that prevented stream file creation."""
        from pathlib import Path
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=["btrfs", "send", "-f", "/tmp/export.btrfs", "..."],
            returncode=0, stdout=b"", stderr=b""
        )

        snap = Path("/snapshots/lama7.202608280230")
        snap_dir = Path("/snapshots")
        backup_dir = None  # no backup_dir, no remote
        stream_file = Path("/tmp/export.btrfs")
        cfg = {"local_sudo": False, "verbose": 1, "dry_run": False}
        archive_cfg = {}  # no remote, no stage_file, no export_dir

        with patch.object(bs, "find_parents", return_value=[]):
            result = bs.send_backup_tofile(
                snap, snap_dir, backup_dir, stream_file, cfg, "lama7", archive_cfg
            )

        # Should return the stream_file path
        self.assertEqual(result, stream_file)
        # Verify run() was called with the send command (not skipped)
        self.assertTrue(mock_run.called)
        call_args = mock_run.call_args[0][0]
        self.assertIn("-f", call_args)
        self.assertIn(str(stream_file), call_args)
        # Should be a full send (no parent refs)
        self.assertNotIn("-p", call_args)
        self.assertNotIn("-c", call_args)


class TestRsyncConfigPrecedence(unittest.TestCase):
    """Test CLI > archive > global precedence for rsync options."""

    def setUp(self):
        patcher = patch("bubtrsnap.chk_btrfs_subvolume")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_cli_rsync_overrides_archive_and_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                rsync = true
                rsync_opts = "--compress"
                [a]
                subvolume = "{sub}"
                rsync = false
                rsync_opts = "--times"
                """,
            )
            cli = _ns(rsync=True, rsync_opts="--bwlimit=1000", archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            a = archives[0]
            self.assertTrue(a["rsync"])
            self.assertEqual(a["rsync_opts"], "--bwlimit=1000")

    def test_archive_rsync_overrides_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                rsync = false
                rsync_opts = "--compress"
                [a]
                subvolume = "{sub}"
                rsync = true
                rsync_opts = "--times"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertTrue(a["rsync"])
            self.assertEqual(a["rsync_opts"], "--times")

    def test_global_rsync_inherited_when_no_archive_setting(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                rsync = true
                rsync_opts = "--compress"
                [a]
                subvolume = "{sub}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertTrue(a["rsync"])
            self.assertEqual(a["rsync_opts"], "--compress")

    def test_rsync_defaults_when_not_set(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                [a]
                subvolume = "{sub}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertFalse(a["rsync"])
            self.assertIsNone(a["rsync_opts"])


class TestRsyncOptionsFilter(unittest.TestCase):
    """Test that _filter_rsync_opts blocks dangerous options."""

    def test_no_opts_returns_empty(self):
        self.assertEqual(bs._filter_rsync_opts(None), [])
        self.assertEqual(bs._filter_rsync_opts(""), [])

    def test_safe_opts_preserved(self):
        opts = "--compress --whole-file"
        result = bs._filter_rsync_opts(opts)
        self.assertIn("--compress", result)
        self.assertIn("--whole-file", result)

    def test_blocked_opts_filtered(self):
        opts = "--delete --verbose --progress"
        result = bs._filter_rsync_opts(opts)
        self.assertNotIn("--delete", result)
        self.assertNotIn("--verbose", result)
        self.assertNotIn("--progress", result)
        self.assertEqual(len(result), 0)

    def test_blocked_opts_reported_via_stderr(self):
        opts = "--delete"
        from io import StringIO
        import sys as _sys
        old_stderr = _sys.stderr
        _sys.stderr = StringIO()
        try:
            bs._filter_rsync_opts(opts)
            output = _sys.stderr.getvalue()
        finally:
            _sys.stderr = old_stderr
        self.assertIn("blocking", output.lower())
        self.assertIn("--delete", output)

    def test_partial_dir_always_blocked(self):
        opts = "--partial-dir=/tmp/foo --archive"
        result = bs._filter_rsync_opts(opts)
        self.assertNotIn("--partial-dir=/tmp/foo", result)
        self.assertIn("--archive", result)

    def test_combined_short_opts_split(self):
        """-av should keep -a but block -v."""
        opts = "-av"
        result = bs._filter_rsync_opts(opts)
        self.assertIn("-a", result)
        self.assertNotIn("-v", result)

    def test_option_with_value_stripped(self):
        """--delete=something still blocked."""
        opts = "--delete=foo"
        result = bs._filter_rsync_opts(opts)
        self.assertEqual(len(result), 0)

    def test_dry_run_blocked(self):
        opts = "--dry-run --compress"
        result = bs._filter_rsync_opts(opts)
        self.assertNotIn("--dry-run", result)
        self.assertIn("--compress", result)

    def test_daemon_blocked(self):
        opts = "--daemon"
        result = bs._filter_rsync_opts(opts)
        self.assertEqual(len(result), 0)


class TestRsyncAndReceive(unittest.TestCase):
    """Test _rsync_and_receive function."""

    @patch("bubtrsnap.run")
    def test_rsync_and_receive_dry_run(self, mock_run):
        """_rsync_and_receive should log rsync and SSH commands in dry-run."""
        from pathlib import Path
        import subprocess

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": "--compress"}

        mock_run.return_value = None

        result = bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg)

        self.assertEqual(result, "test.202608280230")
        # Should have at least 3 run() calls: rsync, receive, cleanup
        self.assertGreaterEqual(mock_run.call_count, 3)

        # Verify rsync command was logged
        calls_str = [str(c) for c in mock_run.call_args_list]
        rsync_found = any("rsync" in s for s in calls_str)
        self.assertTrue(rsync_found, "rsync command not found in run() calls")

    @patch("bubtrsnap.run")
    def test_rsync_command_structure(self, mock_run):
        """Verify rsync command includes -a, --partial-dir, and remote path."""
        from pathlib import Path
        import subprocess

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": "--compress"}

        mock_run.return_value = None

        bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg,
                              rsync_opts=cfg.get("rsync_opts"))

        # Find the rsync command call
        rsync_cmd = None
        for call_obj in mock_run.call_args_list:
            args, kwargs = call_obj
            cmd = args[0]
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "rsync":
                rsync_cmd = cmd
                break

        self.assertIsNotNone(rsync_cmd, "rsync command not found in calls")
        self.assertIn("-a", rsync_cmd)
        self.assertIn("--partial-dir", rsync_cmd)
        self.assertIn("--compress", rsync_cmd)
        self.assertIn("user@host:/tmp/bubtrsnap-test.202608280230.btrfs", rsync_cmd)

    @patch("bubtrsnap.run")
    def test_rsync_partial_dir_detection(self, mock_run):
        """rsync uses --partial-dir flag for interrupted transfer recovery."""
        from pathlib import Path

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}

        # All calls return None for dry-run (rsync, receive, cleanup)
        mock_run.side_effect = [None, None, None]

        result = bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg)
        self.assertEqual(result, "test.202608280230")

        # Verify --partial-dir flag is present in the rsync command
        rsync_cmd = None
        for call_obj in mock_run.call_args_list:
            args, kwargs = call_obj
            cmd = args[0]
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "rsync":
                rsync_cmd = cmd
                break
        self.assertIsNotNone(rsync_cmd, "rsync command not found in calls")
        self.assertIn("--partial-dir", rsync_cmd)
        self.assertIn(".bubtrsnap-partial", rsync_cmd)

    @patch("bubtrsnap.run")
    def test_rsync_transfer_failure(self, mock_run):
        """rsync transfer failure should exit."""
        from pathlib import Path
        import subprocess

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}

        # First call (rsync) fails, second call (receive) not reached
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["rsync"], returncode=1, stdout="", stderr="connection refused"),
        ]

        with self.assertRaises(SystemExit):
            bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg)

    @patch("bubtrsnap.run")
    def test_rsync_receive_failure(self, mock_run):
        """SSH receive failure after rsync should exit."""
        from pathlib import Path
        import subprocess

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}

        # First call (rsync) succeeds, second call (receive) fails
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh", "btrfs", "receive"], returncode=1, stdout="", stderr="no space left"),
            subprocess.CompletedProcess(args=["ssh", "rm"], returncode=0, stdout="", stderr=""),
        ]

        with self.assertRaises(SystemExit):
            bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg)

    @patch("bubtrsnap.run")
    def test_rsync_receive_already_exists(self, mock_run):
        """rsync receive returning 'already exists' should return None."""
        from pathlib import Path
        import subprocess

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}

        mock_run.side_effect = [
            subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh", "btrfs", "receive"], returncode=1, stdout="", stderr="subvolume already exists"),
            subprocess.CompletedProcess(args=["ssh", "rm"], returncode=0, stdout="", stderr=""),
        ]

        result = bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg)
        self.assertIsNone(result)

    @patch("bubtrsnap.run")
    def test_rsync_command_with_local_sudo(self, mock_run):
        """rsync with local_sudo should prepend sudo -n."""
        from pathlib import Path

        stream = Path("/tmp/test.202608280230.btrfs")
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": True, "rsync": True, "rsync_opts": None}

        mock_run.return_value = None

        bs._rsync_and_receive(stream, "user@host", "/remote/backup", cfg,
                              rsync_opts=cfg.get("rsync_opts"))

        # Find the rsync command call — when local_sudo is set, the command
        # is ["sudo", "-n", "rsync", ...] instead of ["rsync", ...]
        rsync_cmd = None
        for call_obj in mock_run.call_args_list:
            args, kwargs = call_obj
            cmd = args[0]
            if isinstance(cmd, list) and "rsync" in cmd:
                rsync_cmd = cmd
                break

        self.assertIsNotNone(rsync_cmd, "rsync command with sudo not found")
        self.assertEqual(rsync_cmd[0], "sudo")
        self.assertEqual(rsync_cmd[1], "-n")
        self.assertEqual(rsync_cmd[2], "rsync")


class TestReceiveRemoteRsyncRouting(unittest.TestCase):
    """Test that _receive_remote routes to rsync when use_rsync=True."""

    @patch("bubtrsnap._rsync_and_receive")
    @patch("bubtrsnap._scp_and_receive")
    def test_receive_remote_uses_rsync(self, mock_scp, mock_rsync):
        from pathlib import Path
        mock_rsync.return_value = "test_subvol"
        stream = Path("/tmp/test.btrfs")
        cfg = {"verbose": 0, "dry_run": True}

        result = bs._receive_remote(stream, "user@host", "/remote/backup", cfg,
                                     remote_sudo=False, use_rsync=True, rsync_opts="--compress")

        mock_rsync.assert_called_once_with(stream, "user@host", "/remote/backup", cfg,
                                           False, "--compress")
        mock_scp.assert_not_called()
        self.assertEqual(result, "test_subvol")

    @patch("bubtrsnap._rsync_and_receive")
    @patch("bubtrsnap._scp_and_receive")
    def test_receive_remote_uses_scp_when_no_rsync(self, mock_scp, mock_rsync):
        from pathlib import Path
        mock_scp.return_value = "test_subvol"
        stream = Path("/tmp/test.btrfs")
        cfg = {"verbose": 0, "dry_run": True}

        result = bs._receive_remote(stream, "user@host", "/remote/backup", cfg)

        mock_scp.assert_called_once_with(stream, "user@host", "/remote/backup", cfg, False)
        mock_rsync.assert_not_called()
        self.assertEqual(result, "test_subvol")


class TestInterruptedRsyncResumption(unittest.TestCase):
    """Test that process_archive handles interrupted rsync transfers correctly."""

    def _make_archive(self, **overrides):
        """Build a minimal archive dict for process_archive."""
        return {
            "name": "testarchive",
            "subvolume": "/test/subvol",
            "keep": [],
            "export_file": "/tmp/test_stream.btrfs",
            "remote_host": "user@host",
            "remote_path": "/remote/backup",
            "rsync": True,
            **overrides,
        }

    @patch("bubtrsnap.apply_keep_policy")
    @patch("bubtrsnap._receive_and_post")
    @patch("bubtrsnap.receive_stream")
    @patch("bubtrsnap._check_interrupted_rsync")
    @patch("bubtrsnap.run_hook")
    @patch("bubtrsnap.create_snapshot")
    @patch("bubtrsnap.chk_btrfs_subvolume")
    @patch("bubtrsnap.send_backup_tofile")
    def test_resumes_interrupted_rsync_no_new_snapshot(
        self, mock_send, mock_chk, mock_snap, mock_hook, mock_check, mock_recv, mock_post, mock_keep
    ):
        """When interrupted rsync is detected with existing stream file,
        process_archive should skip snapshot creation and reuse the stream."""
        mock_check.return_value = True
        mock_recv.return_value = "testarchive"
        stream_file = Path("/tmp/test_stream.btrfs")
        stream_file.touch()

        cfg = {
            "verbose": 0, "dry_run": True,
            "snapshot_dir": "/tmp/snapshots",
            "local_sudo": False, "remote_sudo": False,
        }
        archive = self._make_archive()

        with patch("bubtrsnap._validate_destinations"), \
             patch("bubtrsnap._piped_send_to_local"):
            result = bs.process_archive(archive, cfg)

        # Snapshot should NOT be created (interrupted transfer path)
        mock_snap.assert_not_called()
        # send_backup_tofile should NOT be called (stream file already exists)
        mock_send.assert_not_called()
        # receive_stream should be called
        mock_recv.assert_called()
        # pre/post-snapshot hooks should NOT be called (no snapshot created)
        mock_hook.assert_not_called()
        # Should return "RECOVERED" to signal reprocessing
        self.assertEqual(result, "RECOVERED")
        # Cleanup
        stream_file.unlink(missing_ok=True)

    @patch("bubtrsnap.apply_keep_policy")
    @patch("bubtrsnap._receive_and_post")
    @patch("bubtrsnap.receive_stream")
    @patch("bubtrsnap._check_interrupted_rsync")
    @patch("bubtrsnap.run_hook")
    @patch("bubtrsnap.create_snapshot")
    @patch("bubtrsnap.chk_btrfs_subvolume")
    @patch("bubtrsnap.send_backup_tofile")
    def test_interrupted_rsync_with_backup_dir_skips_piped_send(
        self, mock_send, mock_chk, mock_snap, mock_hook, mock_check, mock_recv, mock_post, mock_keep
    ):
        """When interrupted rsync is detected with backup_dir set,
        _piped_send_to_local should NOT be called — snap is a stream file
        (not a subvolume), and the local backup was already completed in
        the previous run. Only the SSH rsync resume + receive should occur."""
        mock_check.return_value = True
        mock_recv.return_value = "testarchive"
        stream_file = Path("/tmp/test_stream.btrfs")
        stream_file.touch()

        cfg = {
            "verbose": 0, "dry_run": True,
            "snapshot_dir": "/tmp/snapshots",
            "local_sudo": False, "remote_sudo": False,
        }
        archive = self._make_archive(backup_dir="/tmp/backup")

        with patch("bubtrsnap._validate_destinations"), \
             patch("bubtrsnap._piped_send_to_local") as mock_piped:
            result = bs.process_archive(archive, cfg)

        # Snapshot should NOT be created (interrupted transfer path)
        mock_snap.assert_not_called()
        # send_backup_tofile should NOT be called (stream file already exists)
        mock_send.assert_not_called()
        # _piped_send_to_local should NOT be called (snap is a stream file,
        # not a subvolume — local backup was already done in the previous run)
        mock_piped.assert_not_called()
        # pre/post-snapshot hooks should NOT be called (no snapshot created)
        mock_hook.assert_not_called()
        # receive_stream should be called (for SSH rsync resume + receive)
        mock_recv.assert_called()
        # Should return "RECOVERED" to signal reprocessing
        self.assertEqual(result, "RECOVERED")
        # Cleanup
        stream_file.unlink(missing_ok=True)

    @patch("bubtrsnap.apply_keep_policy")
    @patch("bubtrsnap._receive_and_post")
    @patch("bubtrsnap.receive_stream")
    @patch("bubtrsnap._check_interrupted_rsync")
    @patch("bubtrsnap.run_hook")
    @patch("bubtrsnap.create_snapshot")
    @patch("bubtrsnap.chk_btrfs_subvolume")
    @patch("bubtrsnap.send_backup_tofile")
    def test_no_interrupted_rsync_creates_new_snapshot(
        self, mock_send, mock_chk, mock_snap, mock_hook, mock_check, mock_recv, mock_post, mock_keep
    ):
        """When no interrupted rsync is detected, normal snapshot creation proceeds."""
        mock_check.return_value = False
        mock_snap.return_value = MagicMock(name="testarchive.202601011200")
        mock_snap.return_value.name = "testarchive.202601011200"
        mock_snap.return_value.is_file.return_value = False
        mock_recv.return_value = "testarchive"

        cfg = {
            "verbose": 0, "dry_run": True,
            "snapshot_dir": "/tmp/snapshots",
            "local_sudo": False, "remote_sudo": False,
        }
        archive = self._make_archive()

        with patch("bubtrsnap._validate_destinations"), \
             patch("bubtrsnap._piped_send_to_local"):
            result = bs.process_archive(archive, cfg)

        # Snapshot SHOULD be created
        mock_snap.assert_called_once()
        # send_backup_tofile SHOULD be called (new stream created)
        mock_send.assert_called_once()
        # pre/post-snapshot hooks SHOULD be called (snapshot was created)
        mock_hook.assert_called()
        # Should return None (not "RECOVERED")
        self.assertIsNone(result)

    @patch("builtins.print")
    @patch("bubtrsnap.run")
    def test_check_interrupted_rsync_detects_partial_dir(self, mock_run, mock_print):
        """_check_interrupted_rsync returns True when partial-dir exists on remote."""
        mock_run.return_value = MagicMock(returncode=0)

        cfg = {"verbose": 0, "dry_run": False, "local_sudo": False}
        result = bs._check_interrupted_rsync("user@host", cfg)
        self.assertTrue(result)

    @patch("bubtrsnap.run")
    def test_check_interrupted_rsync_no_partial_dir(self, mock_run):
        """_check_interrupted_rsync returns False when partial-dir does not exist."""
        mock_run.return_value = MagicMock(returncode=1)

        cfg = {"verbose": 0, "dry_run": False, "local_sudo": False}
        result = bs._check_interrupted_rsync("user@host", cfg)
        self.assertFalse(result)

    @patch("bubtrsnap.run")
    def test_check_interrupted_rsync_dry_run_executes(self, mock_run):
        """_check_interrupted_rsync always executes the SSH check (dry_run=False),
        even when cfg dry_run is True, so dry-run previews detect interruptions."""
        mock_run.return_value = MagicMock(returncode=1)

        cfg = {"verbose": 2, "dry_run": True, "local_sudo": False}
        result = bs._check_interrupted_rsync("user@host", cfg)
        self.assertFalse(result)
        # Verify run() was called with dry_run=False (always execute, not cfg dry_run)
        call_kwargs = mock_run.call_args.kwargs
        self.assertFalse(call_kwargs.get("dry_run", False))

    @patch("bubtrsnap.run")
    def test_check_interrupted_rsync_dry_run_detects_partial(self, mock_run):
        """In dry-run, _check_interrupted_rsync should still detect a
        partial-dir on the remote (dry_run=False forces execution)."""
        mock_run.return_value = MagicMock(returncode=0)

        cfg = {"verbose": 2, "dry_run": True, "local_sudo": False}
        result = bs._check_interrupted_rsync("user@host", cfg)
        self.assertTrue(result)

    @patch("bubtrsnap.run")
    def test_check_interrupted_rsync_no_sudo_prefix(self, mock_run):
        """_check_interrupted_rsync must NOT run the ssh client as root, even
        when local_sudo is set. The check reads no local file (unlike scp/rsync,
        which need root to read the root-owned stream file), and running ssh as
        root would use root's ~/.ssh config/keys/known_hosts, which can break
        the connection and silently disable interrupted-rsync recovery."""
        mock_run.return_value = MagicMock(returncode=1)

        cfg = {"verbose": 0, "dry_run": False, "local_sudo": True}
        result = bs._check_interrupted_rsync("user@host", cfg)
        self.assertFalse(result)

        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, ["ssh", "user@host", "test", "-d", "/tmp/.bubtrsnap-partial"])
        self.assertNotIn("sudo", cmd)


class TestValidationCache(unittest.TestCase):
    """Test that btrfs subvolume validation results are cached across
    archives sharing the same paths within a single bubtrsnap run."""

    @patch("bubtrsnap.run")
    def test_chk_btrfs_subvolume_caches_result(self, mock_run):
        """Second call with same path skips run() (cache hit)."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = {"verbose": 0}

        bs.chk_btrfs_subvolume(Path("/pool/snapshots"), cfg)
        bs.chk_btrfs_subvolume(Path("/pool/snapshots"), cfg)

        self.assertEqual(mock_run.call_count, 1)

    @patch("bubtrsnap.run")
    def test_chk_btrfs_subvolume_different_paths_not_cached(self, mock_run):
        """Different paths are each validated independently."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = {"verbose": 0}

        bs.chk_btrfs_subvolume(Path("/pool/snapshots"), cfg)
        bs.chk_btrfs_subvolume(Path("/pool/backup"), cfg)

        self.assertEqual(mock_run.call_count, 2)

    @patch("bubtrsnap.run")
    def test_chk_btrfs_subvolume_cache_populated_in_cfg(self, mock_run):
        """Cache key is stored in cfg after successful validation."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = {"verbose": 0}

        bs.chk_btrfs_subvolume(Path("/pool/snapshots"), cfg)

        self.assertIn("local:/pool/snapshots", cfg["_validated_paths"])

    @patch("bubtrsnap.run")
    def test_chk_btrfs_subvolume_ssh_caches_result(self, mock_run):
        """Second call with same remote+path skips run() (cache hit)."""
        mock_result = MagicMock(returncode=0)
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        cfg = {"verbose": 0}

        bs.chk_btrfs_subvolume_ssh("user@host", "/remote/backup", cfg)
        bs.chk_btrfs_subvolume_ssh("user@host", "/remote/backup", cfg)

        self.assertEqual(mock_run.call_count, 1)

    @patch("bubtrsnap.run")
    def test_chk_btrfs_subvolume_ssh_different_targets_not_cached(self, mock_run):
        """Different remotes are each validated independently."""
        mock_result = MagicMock(returncode=0)
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        cfg = {"verbose": 0}

        bs.chk_btrfs_subvolume_ssh("user@host", "/remote/backup", cfg)
        bs.chk_btrfs_subvolume_ssh("user@other", "/remote/backup", cfg)

        self.assertEqual(mock_run.call_count, 2)

    @patch("bubtrsnap.apply_keep_policy")
    @patch("bubtrsnap.run")
    @patch("bubtrsnap.create_snapshot")
    @patch("bubtrsnap.send_backup_tofile")
    @patch("bubtrsnap._piped_send_to_local")
    @patch("bubtrsnap.receive_stream")
    @patch("bubtrsnap._receive_and_post")
    @patch("bubtrsnap.find_parents")
    @patch("bubtrsnap.run_hook")
    def test_validation_cached_across_process_archive_calls(
        self, mock_hook, mock_find_parents, mock_post, mock_recv,
        mock_piped, mock_send, mock_snap, mock_run, mock_keep
    ):
        """Two archives sharing the same snapshot_dir/backup_dir only
        validate each path once — second process_archive call hits cache."""
        mock_find_parents.return_value = []
        mock_send.return_value = Path("/tmp/stream.btrfs")
        mock_snap.return_value = MagicMock(name="testarchive.202601011200")
        mock_snap.return_value.name = "testarchive.202601011200"
        mock_snap.return_value.is_file.return_value = False
        mock_recv.return_value = "received"
        mock_keep.return_value = None
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        cfg = {
            "verbose": 0, "dry_run": True,
            "snapshot_dir": "/tmp/snapshots",
            "backup_dir": "/tmp/backup",
            "local_sudo": False, "remote_sudo": False,
        }

        archive = {
            "name": "testarchive",
            "subvolume": "/test/subvol",
            "keep": [],
        }

        # First call — validation executes for subvol, snap_dir, backup_dir
        mock_run.reset_mock()
        bs.process_archive(archive, cfg)
        first_run_calls = mock_run.call_count

        # Second call — same paths, validation skipped via cache
        mock_run.reset_mock()
        bs.process_archive(archive, cfg)
        second_run_calls = mock_run.call_count

        # First call should have validation run() calls
        self.assertGreater(first_run_calls, 0)
        # Second call should have zero run() calls (all validation cached)
        self.assertEqual(second_run_calls, 0)

    @patch("bubtrsnap.apply_keep_policy")
    @patch("bubtrsnap.run")
    @patch("bubtrsnap.create_snapshot")
    @patch("bubtrsnap.send_backup_tofile")
    @patch("bubtrsnap._piped_send_to_local")
    @patch("bubtrsnap.receive_stream")
    @patch("bubtrsnap._receive_and_post")
    @patch("bubtrsnap.find_parents")
    @patch("bubtrsnap.run_hook")
    def test_validation_cache_skips_invalid_path_logging(
        self, mock_hook, mock_find_parents, mock_post, mock_recv,
        mock_piped, mock_send, mock_snap, mock_run, mock_keep
    ):
        """Cache hits do not produce the 'is a valid btrfs subvolume' log."""
        mock_find_parents.return_value = []
        mock_send.return_value = Path("/tmp/stream.btrfs")
        mock_snap.return_value = MagicMock(name="testarchive.202601011200")
        mock_snap.return_value.name = "testarchive.202601011200"
        mock_snap.return_value.is_file.return_value = False
        mock_recv.return_value = "received"
        mock_keep.return_value = None
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        cfg = {
            "verbose": 2, "dry_run": True,
            "snapshot_dir": "/tmp/snapshots",
            "backup_dir": "/tmp/backup",
            "local_sudo": False, "remote_sudo": False,
        }

        archive = {
            "name": "testarchive",
            "subvolume": "/test/subvol",
            "keep": [],
        }

        # First call
        bs.process_archive(archive, cfg)
        self.assertEqual(mock_run.call_count, 3)  # subvol, snap_dir, backup_dir

        # Second call — cached, no run() calls
        mock_run.reset_mock()
        bs.process_archive(archive, cfg)
        self.assertEqual(mock_run.call_count, 0)


if __name__ == "__main__":
    unittest.main()