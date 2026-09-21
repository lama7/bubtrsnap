#!/usr/bin/env python3
"""Unit tests for bubtrsnap file-related option combinations and precedence."""

from __future__ import annotations

import argparse
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch
from pathlib import Path

def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap"
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

    def test_stage_file_exclusive_with_export_file(self):
        cli = _ns(stage_file="/tmp/s.btrfs", export_file="/tmp/a.btrfs", archives=["a"])
        with self.assertRaises(SystemExit):
            bs.load_and_resolve_archives(cli, None)

    def test_stage_file_exclusive_with_stage_dir(self):
        cli = _ns(stage_file="/tmp/s.btrfs", stage_dir="/tmp/streams", archives=["a"])
        with self.assertRaises(SystemExit):
            bs.load_and_resolve_archives(cli, None)

    def test_stage_dir_exclusive_with_export_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d1 = Path(td) / "s"
            d2 = Path(td) / "t"
            d1.mkdir()
            d2.mkdir()
            cli = _ns(stage_dir=str(d1), export_dir=str(d2), archives=["a"])
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, None)

    def test_mix_file_and_dir_on_cli(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "streams"
            d.mkdir()
            cli = _ns(
                export_file="/tmp/a.btrfs",
                export_dir=str(d),
                archives=["a"],
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, None)

    def test_snaps_only_with_export_file(self):
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
                export_file=str(td_path / "out.btrfs"),
                archives=["a"],
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(cli, cfg)


class TestConfigCombinations(unittest.TestCase):
    def test_stage_dir_with_export_dir_global(self):
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
                export_dir = "{d2}"
                [a]
                subvolume = "{sub}"
                """,
            )
            with self.assertRaises(SystemExit):
                bs.load_and_resolve_archives(_ns(), cfg)

    def test_stage_file_with_export_file_on_archive(self):
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
                export_file = "{td_path}/send.btrfs"
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

    def setUp(self):
        patcher = patch("bubtrsnap.chk_btrfs_subvolume")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_cli_export_dir_ignores_config_stage_dir_and_archive_file(self):
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
                export_file = "{td_path}/ignored.btrfs"
                """,
            )
            cli = _ns(export_dir=str(streams_cli), archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            self.assertEqual(len(archives), 1)
            a = archives[0]
            self.assertEqual(a["export_dir"], str(streams_cli))
            self.assertIsNone(a["stage_dir"])
            self.assertIsNone(a["export_file"])
            self.assertIsNone(a["import_file"])
            self.assertIsNone(a["stage_file"])
            self.assertIsNone(a["import_dir"])

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
                export_dir = "{streams}"
                [a]
                subvolume = "{subs['a']}"
                export_file = "{td_path}/a.btrfs"
                [b]
                subvolume = "{subs['b']}"
                """,
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            by_name = {x["name"]: x for x in archives}
            self.assertEqual(by_name["a"]["export_file"], str(td_path / "a.btrfs"))
            self.assertIsNone(by_name["a"]["export_dir"])
            self.assertIsNone(by_name["a"]["stage_dir"])
            self.assertEqual(by_name["b"]["export_dir"], str(streams))
            self.assertIsNone(by_name["b"]["export_file"])

    def test_mixed_archives_global_export_dir(self):
        """Archive A has import_file; archive B has none.
        Global export_dir applies only to B, A suppresses it."""

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            streams = td_path / "streams"
            streams.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f'''\n                snapshot_dir = "{td_path}"
                export_dir = "{streams}"
                [a]
                subvolume = "{subs['a']}"
                import_file = "{td_path}/a.btrfs"
                [b]
                subvolume = "{subs['b']}"
                ''',
            )
            _global, archives = bs.load_and_resolve_archives(_ns(archives=["a","b"]), cfg)
            by_name = {x["name"]: x for x in archives}
            self.assertEqual(by_name["a"]["import_file"], str(td_path / "a.btrfs"))
            self.assertIsNone(by_name["a"]["export_dir"])
            self.assertIsNone(by_name["a"]["export_file"])
            self.assertEqual(by_name["b"]["export_dir"], str(streams))
            self.assertIsNone(by_name["b"]["import_file"])

    def test_cli_stage_file_ignores_config_import_file(self):
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
                import_file = "{td_path}/old.btrfs"
                """,
            )
            stage = str(td_path / "stage.btrfs")
            cli = _ns(stage_file=stage, archives=["a"])
            _global, archives = bs.load_and_resolve_archives(cli, cfg)
            a = archives[0]
            self.assertEqual(a["stage_file"], stage)
            self.assertIsNone(a["import_file"])
            self.assertIsNone(a["export_file"])

    def test_archive_export_dir_overrides_global(self):
        """Per-archive export_dir should override the global default."""

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            streams_global = td_path / "global_streams"
            streams_a = td_path / "a_streams"
            streams_global.mkdir()
            streams_a.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f'''
                snapshot_dir = "{td_path}"
                export_dir = "{streams_global}"
                [a]
                subvolume = "{subs["a"]}"
                export_dir = "{streams_a}"
                [b]
                subvolume = "{subs["b"]}"
                ''',
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            by_name = {x["name"]: x for x in archives}
            # Archive A overrides global → should use its own dir
            self.assertEqual(by_name["a"]["export_dir"], str(streams_a))
            # Archive B has no per-archive setting → inherits global
            self.assertEqual(by_name["b"]["export_dir"], str(streams_global))

    def test_archive_import_dir_overrides_global(self):
        """Per-archive import_dir should override the global default."""

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            recv_global = td_path / "global_recv"
            recv_a = td_path / "a_recv"
            recv_global.mkdir()
            recv_a.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f'''
                snapshot_dir = "{td_path}"
                import_dir = "{recv_global}"
                [a]
                subvolume = "{subs["a"]}"
                import_dir = "{recv_a}"
                [b]
                subvolume = "{subs["b"]}"
                ''',
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            by_name = {x["name"]: x for x in archives}
            self.assertEqual(by_name["a"]["import_dir"], str(recv_a))
            self.assertEqual(by_name["b"]["import_dir"], str(recv_global))

    def test_archive_stage_dir_overrides_global(self):
        """Per-archive stage_dir should override the global default."""

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            subs = _with_subvol_dirs(td_path, "a", "b")
            stage_global = td_path / "global_staging"
            stage_a = td_path / "a_staging"
            stage_global.mkdir()
            stage_a.mkdir()
            cfg = td_path / "c.toml"
            _write_config(
                cfg,
                f'''
                snapshot_dir = "{td_path}"
                stage_dir = "{stage_global}"
                [a]
                subvolume = "{subs["a"]}"
                stage_dir = "{stage_a}"
                [b]
                subvolume = "{subs["b"]}"
                ''',
            )
            _global, archives = bs.load_and_resolve_archives(_ns(), cfg)
            by_name = {x["name"]: x for x in archives}
            self.assertEqual(by_name["a"]["stage_dir"], str(stage_a))
            self.assertEqual(by_name["b"]["stage_dir"], str(stage_global))


class TestSingleArchiveRequirement(unittest.TestCase):
    def test_export_file_two_cli_archives(self):
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
                export_file=str(td_path / "x.btrfs"),
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
