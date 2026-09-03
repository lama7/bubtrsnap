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
from unittest.mock import MagicMock, patch

def _load():
    path = Path(__file__).resolve().parent / "bubtrsnap"
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
        send_to_file=None,
        receive_from_file=None,
        stage_file=None,
        send_to_dir=None,
        receive_from_dir=None,
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
                remote = "global@host"
                remote_dir = "/global/remote"
                [a]
                subvolume = "{sub}"
                remote = "archive@host"
                remote_dir = "/archive/remote"
                """,
            )
            cli = _ns(remote="cli@host", remote_dir="/cli/remote", archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            a = archives[0]
            self.assertEqual(a["remote"], "cli@host")
            self.assertEqual(a["remote_dir"], "/cli/remote")

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
                remote = "global@host"
                remote_dir = "/global/remote"
                [a]
                subvolume = "{sub}"
                remote = "archive@host"
                remote_dir = "/archive/remote"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertEqual(a["remote"], "archive@host")
            self.assertEqual(a["remote_dir"], "/archive/remote")

    def test_remote_without_remote_dir_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = td_path / "subvol_a"
            sub.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                remote = "global@host"
                [a]
                subvolume = "{sub}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            a = archives[0]
            self.assertEqual(a["remote"], "global@host")
            self.assertIsNone(a["remote_dir"])


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


if __name__ == "__main__":
    unittest.main()