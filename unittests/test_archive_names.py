#!/usr/bin/env python3
"""Unit tests for archive-name validation.

Regression guard for TODO item 16: archive names were used raw in filesystem
paths and glob patterns.  `snap_dir / f"{archive}.{ts}"` escaped the snapshot
directory for a name containing '/', and `glob(f"{archive}.*")` in
find_parents(), _archive_snapshots() and _received_uuids_local() treated
metacharacters as pattern syntax — an archive named `foo[0-9]` matched
`foo1.202601010000` and `foo9.202601010000`, so parent discovery could pick
another archive's snapshot as an incremental parent.  Names are now validated
once at load time, which closes both halves at the boundary.
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


class TestArchiveNameValidation(unittest.TestCase):
    """TODO 16: archive names must be safe as path components and glob roots."""

    BAD_NAMES = (
        "foo[0-9]",      # glob metacharacters: matches foo1.*, foo9.*
        "foo*",          # glob wildcard
        "foo?bar",       # glob single char
        "a/b",           # path separator: escapes the snapshot directory
        "../escape",     # path traversal
        "..",
        "-foo",          # leading dash reads as an option to some tools
        ".hidden",       # leading dot: hidden file, and glob ambiguity
        "",              # empty
        "foo bar",       # space: breaks the whitespace-split remote listings
        "fooé",          # non-ASCII
    )

    def test_config_metacharacter_name_rejected(self):
        for name in ("foo[0-9]", "foo*", "foo?bar"):
            exc, err = _run(bs.validate_config, {name: {"subvolume": "/var/lib"}})
            self.assertIsInstance(exc, SystemExit, f"archive {name!r} was accepted")
            self.assertIn(name, err, f"error does not name the offending archive {name!r}")

    def test_config_path_separator_name_rejected(self):
        exc, err = _run(bs.validate_config, {"a/b": {"subvolume": "/var/lib"}})
        self.assertIsInstance(exc, SystemExit, "archive name with '/' was accepted")
        self.assertIn("a/b", err)

    def test_config_traversal_name_rejected(self):
        exc, _ = _run(bs.validate_config, {"../escape": {"subvolume": "/var/lib"}})
        self.assertIsInstance(exc, SystemExit, "archive name '../escape' was accepted")

    def test_config_leading_dash_and_dot_rejected(self):
        for name in ("-foo", ".hidden"):
            exc, _ = _run(bs.validate_config, {name: {"subvolume": "/var/lib"}})
            self.assertIsInstance(exc, SystemExit, f"archive name {name!r} was accepted")

    def test_config_empty_name_rejected(self):
        exc, err = _run(bs.validate_config, {"": {"subvolume": "/var/lib"}})
        self.assertIsInstance(exc, SystemExit, "empty archive name was accepted")
        self.assertIn("empty", err.lower())

    def test_every_bad_name_in_the_table_is_rejected(self):
        for name in self.BAD_NAMES:
            exc, _ = _run(bs.validate_config, {name: {"subvolume": "/var/lib"}})
            self.assertIsInstance(exc, SystemExit, f"archive name {name!r} was accepted")

    def test_safe_names_are_accepted(self):
        for name in ("lama7Maildir", "my_archive", "Archive-1", "a", "varlib"):
            bs.validate_config({name: {"subvolume": "/var/lib"}})


class TestCliArchiveNames(unittest.TestCase):
    """The same rule applies to ARCHIVE[=SUBVOLUME] arguments on the CLI."""

    def test_cli_name_with_slash_rejected(self):
        exc, err = _run(bs.parse_cli_archives, ["a/b=/var/lib"])
        self.assertIsInstance(exc, SystemExit, "CLI archive name with '/' was accepted")
        self.assertIn("a/b", err)

    def test_cli_glob_name_rejected(self):
        exc, err = _run(bs.parse_cli_archives, ["foo[0-9]=/var/lib"])
        self.assertIsInstance(exc, SystemExit, "CLI archive name 'foo[0-9]' was accepted")
        self.assertIn("foo[0-9]", err)

    def test_cli_leading_dash_rejected(self):
        exc, _ = _run(bs.parse_cli_archives, ["-foo=/var/lib"])
        self.assertIsInstance(exc, SystemExit, "CLI archive name '-foo' was accepted")

    def test_cli_safe_names_still_work(self):
        self.assertEqual(
            bs.parse_cli_archives(["lama7Maildir=/var/lib", "a-b_1"]),
            {"lama7Maildir": "/var/lib", "a-b_1": None},
        )

    def test_bad_name_is_rejected_end_to_end(self):
        """load_and_resolve_archives is the single boundary every name passes."""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "bad.toml"
            _write_config(cfg_path, """
                ["foo[0-9]"]
                subvolume = "/var/lib"
            """)
            exc, err = _run(bs.load_and_resolve_archives, _ns(config=cfg_path), cfg_path)
            self.assertIsInstance(exc, SystemExit, "config archive 'foo[0-9]' reached the run")
            self.assertIn("foo[0-9]", err)

    def test_valid_name_survives_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "good.toml"
            _write_config(cfg_path, """
                [lama7Maildir]
                subvolume = "/var/lib"
            """)
            with patch("bubtrsnap.chk_btrfs_subvolume"):
                _, archives = bs.load_and_resolve_archives(_ns(config=cfg_path), cfg_path)
            self.assertEqual(archives[0]["name"], "lama7Maildir")


class TestArchiveItemMatchingIsLiteral(unittest.TestCase):
    """Defence in depth: parent discovery compares names, never glob patterns.

    `glob(f"{archive}.*")` in _archive_snapshots() and _received_uuids_local()
    read '[' , ']' and '*' in an archive name as pattern syntax.
    """

    def _dirs(self, td: str) -> Path:
        d = Path(td)
        for name in ("foo1.202601010000", "foo9.202601010000", "foo.202601010000"):
            (d / name).mkdir()
        return d

    def test_snapshot_discovery_is_literal(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._dirs(td)
            cfg = {"verbose": 0}
            with patch("bubtrsnap.run", return_value=None), \
                 patch("bubtrsnap.get_subvol_uuids", return_value=("uuid", None)):
                got = sorted(p.name for _, p, _ in bs._archive_snapshots("foo[0-9]", d, cfg))
            self.assertEqual(got, [], "glob metacharacters in an archive name still matched")

    def test_received_uuid_discovery_is_literal(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._dirs(td)
            cfg = {"verbose": 0}
            with patch("bubtrsnap.run", return_value=None), \
                 patch("bubtrsnap.get_subvol_uuids", return_value=(None, "recv-uuid")):
                got = bs._received_uuids_local("foo[0-9]", d, cfg)
            self.assertEqual(got, set(), "glob metacharacters in an archive name still matched")

    def test_plain_archive_still_finds_its_own_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._dirs(td)
            cfg = {"verbose": 0}
            with patch("bubtrsnap.run", return_value=None), \
                 patch("bubtrsnap.get_subvol_uuids", return_value=("uuid", None)):
                got = sorted(p.name for _, p, _ in bs._archive_snapshots("foo", d, cfg))
            self.assertEqual(got, ["foo.202601010000"])


if __name__ == "__main__":
    unittest.main()
