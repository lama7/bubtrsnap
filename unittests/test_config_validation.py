#!/usr/bin/env python3
"""Unit tests for keep-policy count and forced-keep timestamp validation.

Covers TODO items 10 (negative keep_* counts accepted) and 11 (CLI
--forced-keep values never format-validated).
"""

from __future__ import annotations

import argparse
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
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
        remote_host=None,
        remote_path=None,
        remote_sudo=False,
        rsync=False,
        rsync_opts=None,
        week_startday=None,
        forced_keep=None,
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


def _run(fn, *args, **kwargs):
    """Call fn, capturing stderr; returns (exception_or_None, stderr_text)."""
    err = None
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            fn(*args, **kwargs)
    except SystemExit as exc:
        err = exc
    return err, buf.getvalue()


class TestKeepCountValidation(unittest.TestCase):
    """TODO 10: negative keep_* counts must be rejected at validation time."""

    def test_global_negative_keep_rejected(self):
        exc, err = _run(bs.validate_config, {"keep_daily": -5})
        self.assertIsInstance(exc, SystemExit, "negative global keep_daily was accepted")
        self.assertIn("keep_daily", err)
        self.assertIn("0", err)

    def test_archive_negative_keep_rejected(self):
        cfg = {"arch": {"subvolume": "/var/lib", "keep_hourly": -1}}
        exc, err = _run(bs.validate_config, cfg)
        self.assertIsInstance(exc, SystemExit, "negative archive keep_hourly was accepted")
        self.assertIn("keep_hourly", err)

    def test_every_keep_key_is_checked(self):
        for key in ("keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"):
            exc, _ = _run(bs.validate_config, {key: -1})
            self.assertIsInstance(exc, SystemExit, f"{key}={-1} was accepted")

    def test_zero_and_positive_counts_still_accepted(self):
        bs.validate_config({"keep_daily": 0, "keep_yearly": 3})
        bs.validate_config({"arch": {"subvolume": "/var/lib", "keep_daily": 0}})

    def test_cli_negative_keep_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "sub").mkdir()
            (td_path / "snap").mkdir()
            cli = _ns(
                snapshot_dir=str(td_path / "snap"),
                keep_daily=-5,
                archives=[f"a={td_path / 'sub'}"],
            )
            with patch("bubtrsnap.chk_btrfs_subvolume"):
                exc, err = _run(bs.load_and_resolve_archives, cli, None)
            self.assertIsInstance(exc, SystemExit, "CLI --keep-daily -5 was accepted")
            self.assertIn("keep-daily", err.replace("_", "-"))


class TestForcedKeepValidation(unittest.TestCase):
    """TODO 11: CLI --forced-keep values must be format-validated like config ones."""

    def test_merge_rejects_malformed_cli_timestamp(self):
        exc, err = _run(bs._merge_keep, None, ["garbage"])
        self.assertIsInstance(exc, SystemExit, "malformed CLI forced-keep was accepted")
        self.assertIn("forced-keep", err.replace("_", "-"))
        self.assertIn("YYYYMMDDhhmm", err)

    def test_merge_rejects_short_and_long_timestamps(self):
        for bad in ("2026", "2026010112000", "2026-01-01", "20260101120a"):
            exc, _ = _run(bs._merge_keep, None, [bad])
            self.assertIsInstance(exc, SystemExit, f"{bad!r} was accepted")

    def test_merge_accepts_valid_timestamps(self):
        self.assertEqual(
            bs._merge_keep("202601011200", ["202601021200", "202601031200,202601041200"]),
            ["202601011200", "202601021200", "202601031200", "202601041200"],
        )

    def test_config_forced_keep_still_validated(self):
        exc, err = _run(bs.validate_config, {"arch": {"subvolume": "/var/lib", "forced_keep": "nope"}})
        self.assertIsInstance(exc, SystemExit)
        self.assertIn("forced_keep", err)

    def test_cli_forced_keep_bad_value_end_to_end(self):
        """The value must be rejected during resolution, not warn-and-ignored later."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "sub").mkdir()
            cli = _ns(
                subvolume=None,
                forced_keep=["2026garbage"],
                archives=[f"a={td_path / 'sub'}"],
            )
            with patch("bubtrsnap.chk_btrfs_subvolume"):
                exc, err = _run(bs.load_and_resolve_archives, cli, None)
            self.assertIsInstance(exc, SystemExit, "malformed --forced-keep reached the keep policy")
            self.assertIn("forced-keep", err.replace("_", "-"))

    def test_cli_forced_keep_good_value_survives_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "sub").mkdir()
            cli = _ns(
                forced_keep=["202601011200"],
                archives=[f"a={td_path / 'sub'}"],
            )
            with patch("bubtrsnap.chk_btrfs_subvolume"):
                _, archives = bs.load_and_resolve_archives(cli, None)
            self.assertEqual(archives[0]["forced_keep"], ["202601011200"])


if __name__ == "__main__":
    unittest.main()
