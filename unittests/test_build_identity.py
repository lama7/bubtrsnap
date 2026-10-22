#!/usr/bin/env python3
"""Unit tests for the build identity stamped into --version and the run log.

TODO item 26 follow-up: a dev build must self-identify (branch, commit, dirty
state) without making VERSION itself non-PEP 440 compliant.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()


class _GitResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _fake_git(commit: str, branch: str, dirty: bool):
    """Return a subprocess.run stand-in emulating a git checkout."""

    def runner(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            return _GitResult(f"{commit}\n", 0)
        if "branch" in cmd:
            return _GitResult(f"{branch}\n", 0)
        if "diff" in cmd:
            return _GitResult("", 1 if dirty else 0)
        raise OSError(f"unexpected git invocation: {cmd}")

    return runner


class TestBuildIdentity(unittest.TestCase):
    def setUp(self):
        self._saved = bs._BUILD_IDENTITY
        bs._BUILD_IDENTITY = None

    def tearDown(self):
        bs._BUILD_IDENTITY = self._saved

    def test_identity_from_git_checkout(self):
        with patch("bubtrsnap.subprocess.run", side_effect=_fake_git("28c32ba", "maintain", False)):
            identity = bs.build_identity()
        self.assertEqual(identity, "maintain 28c32ba")

    def test_dirty_checkout_flagged(self):
        with patch("bubtrsnap.subprocess.run", side_effect=_fake_git("28c32ba", "maintain", True)):
            identity = bs.build_identity()
        self.assertIn("28c32ba", identity)
        self.assertIn("uncommitted", identity)

    def test_no_git_falls_back_gracefully(self):
        def raise_no_git(*args, **kwargs):
            raise FileNotFoundError("git not installed")

        with patch("bubtrsnap.subprocess.run", side_effect=raise_no_git):
            identity = bs.build_identity()
        self.assertEqual(identity, "")

    def test_git_failure_falls_back_gracefully(self):
        def raise_os(*args, **kwargs):
            raise OSError("git blew up")

        with patch("bubtrsnap.subprocess.run", side_effect=raise_os):
            identity = bs.build_identity()
        self.assertEqual(identity, "")

    def test_identity_is_computed_once(self):
        calls = []
        base = _fake_git("28c32ba", "maintain", False)

        def runner(cmd, *args, **kwargs):
            calls.append(cmd)
            return base(cmd, *args, **kwargs)

        with patch("bubtrsnap.subprocess.run", side_effect=runner):
            first = bs.build_identity()
            second = bs.build_identity()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3, "build identity should not re-shell on every call")

    def test_identity_from_a_real_checkout_is_not_empty(self):
        """The tests run from the repo, so git metadata is genuinely available."""
        if not (Path(bs.__file__).resolve().parent / ".git").exists():
            self.skipTest("bubtrsnap.py is not inside a git checkout")
        identity = bs.build_identity()
        self.assertTrue(identity)
        self.assertRegex(identity, r"^[^\s]+ [0-9a-f]{7,40}( uncommitted)?$")

    def test_version_string_includes_identity(self):
        bs._BUILD_IDENTITY = "maintain 28c32ba"
        self.assertEqual(bs.version_string(), f"bubtrsnap {bs.VERSION} (maintain 28c32ba)")

    def test_version_string_without_identity(self):
        bs._BUILD_IDENTITY = ""
        self.assertEqual(bs.version_string(), f"bubtrsnap {bs.VERSION}")

    def test_version_is_pep440_clean(self):
        """VERSION must stay parseable: setuptools reads it as the project version."""
        from packaging import version as pep440

        pep440.Version(bs.VERSION)

    def test_cli_version_prints_identity(self):
        bs._BUILD_IDENTITY = "maintain 28c32ba"
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), patch.object(bs.sys, "argv", ["bubtrsnap", "--version"]):
                bs.main()
        except SystemExit:
            pass
        out = buf.getvalue()
        self.assertIn(bs.VERSION, out)
        self.assertIn("28c32ba", out)


if __name__ == "__main__":
    unittest.main()
