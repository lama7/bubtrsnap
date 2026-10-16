#!/usr/bin/env python3
"""Unit tests for piped_run: sender failures must not be swallowed."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()


class TestPipedRun(unittest.TestCase):
    def test_both_succeed(self):
        rc, err = bs.piped_run(["echo", "hi"], ["cat"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_sender_failure_returns_nonzero(self):
        # The core bug: a failing sender with a succeeding receiver
        # previously returned rc 0.
        rc, err = bs.piped_run(["false"], ["cat"])
        self.assertNotEqual(rc, 0)

    def test_sender_stderr_is_surfaced(self):
        rc, err = bs.piped_run(
            ["sh", "-c", "echo boom >&2; exit 3"], ["cat"]
        )
        self.assertEqual(rc, 3)
        self.assertIn("boom", err)

    def test_receiver_failure_still_reported(self):
        rc, err = bs.piped_run(
            ["echo", "hi"], ["sh", "-c", "echo recvfail >&2; exit 2"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("recvfail", err)

    def test_both_fail_reports_both(self):
        rc, err = bs.piped_run(
            ["sh", "-c", "echo sendbad >&2; exit 1"],
            ["sh", "-c", "echo recvbad >&2; exit 1"],
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("sendbad", err)
        self.assertIn("recvbad", err)

    def test_dry_run_returns_zero(self):
        rc, err = bs.piped_run(["false"], ["cat"], dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_display_uses_quoting(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            bs.piped_run(["sh", "-c", "echo a"], ["cat"], dry_run=True, verbosity=2)
        line = buf.getvalue()
        # Tokens with spaces must be quoted for display, matching run().
        self.assertIn('"echo a"', line)


if __name__ == "__main__":
    unittest.main()
