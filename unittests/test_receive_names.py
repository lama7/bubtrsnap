#!/usr/bin/env python3
"""Unit tests for received-subvolume name parsing (no stem guessing)."""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()

STREAM = Path("corinne.202610150423.btrfs")
REAL_NAME = "corinne.202610150423"
CFG = {"verbose": 0, "dry_run": False}


class TestParseAtSubvol(unittest.TestCase):
    def test_plain_marker(self):
        self.assertEqual(
            bs._parse_at_subvol("At subvol root.202610151200"),
            "root.202610151200",
        )

    def test_quoted_marker(self):
        self.assertEqual(
            bs._parse_at_subvol("At subvol 'snap.202601010000'"),
            "snap.202601010000",
        )

    def test_receiving_variant(self):
        self.assertEqual(
            bs._parse_at_subvol("Receiving subvol foo.202601010000"),
            "foo.202601010000",
        )

    def test_no_marker(self):
        self.assertIsNone(bs._parse_at_subvol("some unrelated output"))
        self.assertIsNone(bs._parse_at_subvol(""))
        self.assertIsNone(bs._parse_at_subvol(None))


class TestArchiveNameFromReceived(unittest.TestCase):
    def test_timestamp_stripped(self):
        self.assertEqual(bs.archive_name_from_received("corinne.202610150423"), "corinne")

    def test_bare_name_kept(self):
        self.assertEqual(bs.archive_name_from_received("corinne"), "corinne")


class TestReceiveLocal(unittest.TestCase):
    def setUp(self):
        self._orig = bs._timed_run

    def tearDown(self):
        bs._timed_run = self._orig

    def _fake(self, rc=0, stdout="", stderr=""):
        bs._timed_run = lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, stderr)

    def _run(self, cfg=CFG):
        err = io.StringIO()
        with redirect_stderr(err):
            name = bs._receive_local(STREAM, Path("/backup"), cfg)
        return name, err.getvalue()

    def test_name_parsed_from_stdout(self):
        self._fake(stdout=f"At subvol {REAL_NAME}\n")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_name_parsed_from_stderr(self):
        self._fake(stderr=f"At subvol {REAL_NAME}")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_fallback_warns_and_uses_stem(self):
        self._fake(stdout="receive finished with no marker")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)  # stem == real name here by convention
        self.assertIn("Warning", err)
        self.assertIn("could not determine", err.lower())

    def test_dry_run_preview_no_warning(self):
        bs._timed_run = lambda *a, **k: None
        name, err = self._run({"verbose": 0, "dry_run": True})
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_already_exists_returns_none(self):
        self._fake(rc=1, stderr="ERROR: subvolume already exists")
        name, _ = self._run()
        self.assertIsNone(name)

    def test_real_error_exits(self):
        self._fake(rc=1, stderr="ERROR: short read")
        with self.assertRaises(SystemExit):
            bs._receive_local(STREAM, Path("/backup"), CFG)


class TestReceiveStreamMismatch(unittest.TestCase):
    """A received name from a different archive must warn, not silently pass."""

    def setUp(self):
        self._orig_local = bs._receive_local

    def tearDown(self):
        bs._receive_local = self._orig_local

    def test_mismatch_warns(self):
        bs._receive_local = lambda *a, **k: "otherarchive.202610150423"
        err = io.StringIO()
        with redirect_stderr(err):
            received = bs.receive_stream(STREAM, Path("/backup"), CFG)
        self.assertEqual(received, "otherarchive.202610150423")
        self.assertIn("does not match archive", err.getvalue())

    def test_match_no_warning(self):
        bs._receive_local = lambda *a, **k: REAL_NAME
        err = io.StringIO()
        with redirect_stderr(err):
            received = bs.receive_stream(STREAM, Path("/backup"), CFG)
        self.assertEqual(received, REAL_NAME)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
