#!/usr/bin/env python3
"""Tests for _check_interrupted_rsync timeout handling (TODO item 20).

The ssh 'test -d' recovery check had no timeout: an unreachable remote
hung the whole run indefinitely. A timeout must be treated as
"no interruption detected" (proceed with a fresh transfer), not a hang.
"""

from __future__ import annotations

import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()


class TestInterruptedRsyncTimeout(unittest.TestCase):

    @patch("bubtrsnap.run")
    def test_ssh_check_passes_a_timeout(self, mock_run):
        """The ssh test -d call must carry a timeout kwarg."""
        mock_run.return_value = MagicMock(returncode=1)
        cfg = {"verbose": 0, "dry_run": False}
        bs._check_interrupted_rsync("user@host", "testarchive", cfg)
        kwargs = mock_run.call_args.kwargs
        self.assertIn("timeout", kwargs,
                      "ssh recovery check must not wait forever")

    @patch("builtins.print")
    @patch("bubtrsnap.run")
    def test_timeout_returns_false_not_crash(self, mock_run, mock_print):
        """A hung ssh (TimeoutExpired) must return False, not propagate."""
        mock_run.side_effect = subprocess.TimeoutExpired(["ssh"], 10)
        cfg = {"verbose": 0, "dry_run": False}
        result = bs._check_interrupted_rsync("user@host", "testarchive", cfg)
        self.assertFalse(result,
                         "unreachable remote must mean 'no interruption', not a crash")

    @patch("bubtrsnap.run")
    def test_oserror_returns_false_not_crash(self, mock_run):
        """ssh missing / connection refused at exec level must not propagate."""
        mock_run.side_effect = OSError("ssh: connect to host timed out")
        cfg = {"verbose": 0, "dry_run": False}
        result = bs._check_interrupted_rsync("user@host", "testarchive", cfg)
        self.assertFalse(result)

    @patch("bubtrsnap.run")
    def test_partial_dir_still_detected(self, mock_run):
        """Sanity: the happy path is unchanged."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = {"verbose": 0, "dry_run": False}
        self.assertTrue(bs._check_interrupted_rsync("user@host", "testarchive", cfg))


if __name__ == "__main__":
    unittest.main()
