#!/usr/bin/env python3
"""Unit tests for bubtrsnap file-related option combinations and precedence."""

from __future__ import annotations

import argparse
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

def _load():
    path = Path(__file__).resolve().parent / "bubtrsnap"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()


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


def _write_config(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip())


def _with_subvol_dirs(tmp: Path, *names: str) -> dict[str, Path]:
    """Create fake subvolume paths that exist on disk (Path.exists only)."""
    out = {}
    for n in names:
        p = tmp / f"subvol_{n}"
        p.mkdir()
        out[n] = p
    return out


class TestCLICombinations(unittest.TestCase):
    """Illegal CLI combinations must abort (SystemExit)."""

    def test_stage_file_exclusive_with_send_to_file(self):
        cli = _ns(stage_file="/tmp/s.btrfs", send_to_file="/tmp/a.btrfs", archives=["a"])
        with self.assertRaises(SystemExit):
            bs.load_and_resolve_archives(cli, None)

    def test_stage_file_exclusive_with_stage_dir(self):
        cli = _ns(stage_file="/tmp/s.btrfs", stage_dir="/tmp/streams", archives=["a"])
        with self.assertRaises(SystemExit):
            bs.load_and_resolve_archives(cli, None)

    def test_stage_dir_exclusive_with_send_to_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d1 = Path(td) / "s"
            d2 = Path(td) / "t"
            d1.mkdir()
            d2.mkdir()
            cli = _ns(stage_dir=str(d1), send_to_dir=str(d2), archives=["a"])
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, None)

    def test_mix_file_and_dir_on_cli(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "streams"
            d.mkdir()
            cli = _ns(
                send_to_file="/tmp/a.btrfs",
                send_to_dir=str(d),
                archives=["a"],
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, None)

    def test_snaps_only_with_send_to_file(self):
        """Prefer main()-level check; resolver also rejects per-archive."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                [a]
                subvolume = "{sub}"
                """,
            )
            cli = _ns(
                snaps_only=True,
                send_to_file=str(td_path / "out.btrfs"),
                archives=["a"],
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, cfg)


class TestConfigCombinations(unittest.TestCase):
    def test_stage_dir_with_send_to_dir_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            d1 = td_path / "s"
            d2 = td_path / "t"
            d1.mkdir()
            d2.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                stage_dir = "{d1}"
                send_to_dir = "{d2}"
                [a]
                subvolume = "{sub}"
                """,
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(_ns(), cfg)

    def test_stage_file_with_send_to_file_on_archive(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                [a]
                subvolume = "{sub}"
                stage_file = "{td_path}/stage.btrfs"
                send_to_file = "{td_path}/send.btrfs"
                """,
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(_ns(), cfg)

    def test_snaps_only_with_stage_dir_global(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            streams = td_path / "streams"
            streams.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                snaps_only = true
                stage_dir = "{streams}"
                [a]
                subvolume = "{sub}"
                """,
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(_ns(), cfg)


class TestPrecedence(unittest.TestCase):
    """Rules: CLI wins all six; archive opts block globals; globals only if bare."""

    def test_cli_send_to_dir_ignores_config_stage_dir_and_archive_file(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            streams_cfg = td_path / "cfg_streams"
            streams_cli = td_path / "cli_streams"
            streams_cfg.mkdir()
            streams_cli.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                stage_dir = "{streams_cfg}"
                [a]
                subvolume = "{sub}"
                send_to_file = "{td_path}/ignored.btrfs"
                """,
            )
            cli = _ns(send_to_dir=str(streams_cli), archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            self.assertEqual(len(archives), 1)
            a = archives[0]
            self.assertEqual(a["send_to_dir"], str(streams_cli))
            self.assertIsNone(a["stage_dir"])
            self.assertIsNone(a["send_to_file"])
            self.assertIsNone(a["receive_from_file"])
            self.assertIsNone(a["stage_file"])
            self.assertIsNone(a["receive_from_dir"])

    def test_archive_file_opts_suppress_global_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            streams = td_path / "streams"
            streams.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                send_to_dir = "{streams}"
                [a]
                subvolume = "{subs['a']}"
                send_to_file = "{td_path}/a.btrfs"
                [b]
                subvolume = "{subs['b']}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            by_name = {x["name"]: x for x in archives}
            self.assertEqual(by_name["a"]["send_to_file"], str(td_path / "a.btrfs"))
            self.assertIsNone(by_name["a"]["send_to_dir"])
            self.assertIsNone(by_name["a"]["stage_dir"])
            self.assertEqual(by_name["b"]["send_to_dir"], str(streams))
            self.assertIsNone(by_name["b"]["send_to_file"])

    def test_cli_stage_file_ignores_config_receive_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sub = _with_subvol_dirs(td_path, "a")["a"]
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                backup_dir = "{td_path}"
                [a]
                subvolume = "{sub}"
                receive_from_file = "{td_path}/old.btrfs"
                """,
            )
            stage = str(td_path / "stage.btrfs")
            cli = _ns(stage_file=stage, archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            a = archives[0]
            self.assertEqual(a["stage_file"], stage)
            self.assertIsNone(a["receive_from_file"])
            self.assertIsNone(a["send_to_file"])


class TestSingleArchiveRequirement(unittest.TestCase):
    def test_send_to_file_two_cli_archives(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                [a]
                subvolume = "{subs['a']}"
                [b]
                subvolume = "{subs['b']}"
                """,
            )
            cli = _ns(
                send_to_file=str(td_path / "x.btrfs"),
                archives=["a", "b"],
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, cfg)

    def test_stage_file_no_cli_archive_two_in_config(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f"""
                snapshot_dir = "{td_path}"
                [a]
                subvolume = "{subs['a']}"
                [b]
                subvolume = "{subs['b']}"
                """,
            )
            cli = _ns(stage_file=str(td_path / "s.btrfs"))
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, cfg)


if __name__ == "__main__":
    unittest.main()
