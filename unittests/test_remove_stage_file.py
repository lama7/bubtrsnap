#!/usr/bin/env python3
"""Tests for remove_stage_file error reporting (TODO item 19).

The sudo-rm path ran with check=False and never inspected the returncode,
so a failed rm was logged as "Removed staged stream file" anyway.
"""

from __future__ import annotations

import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()


class TestRemoveStageFile(unittest.TestCase):

    @patch("builtins.print")
    @patch("bubtrsnap.run")
    def test_sudo_rm_failure_warns_instead_of_claiming_success(self, mock_run, mock_print):
        """A non-zero sudo rm exit must warn, not log success."""
        mock_run.return_value = MagicMock(returncode=1, stderr="sudo: permission denied")
        cfg = {"verbose": 1, "dry_run": False, "local_sudo": True}

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stage.btrfs"
            path.write_text("fake stream")
            bs.remove_stage_file(path, cfg)

        printed = [str(a[0]) for a in mock_print.call_args_list if a[0]]
        self.assertTrue(any("could not remove" in p.lower() for p in printed),
                        f"failed rm must produce a warning, got: {printed}")
        self.assertFalse(any("Removed staged stream file" in p for p in printed),
                         f"failed rm must not claim success, got: {printed}")

    @patch("builtins.print")
    @patch("bubtrsnap.run")
    def test_sudo_rm_success_logs_removed(self, mock_run, mock_print):
        """A zero-exit sudo rm still logs the removal."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        cfg = {"verbose": 1, "dry_run": False, "local_sudo": True}

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stage.btrfs"
            path.write_text("fake stream")
            bs.remove_stage_file(path, cfg)

        printed = [str(a[0]) for a in mock_print.call_args_list if a[0]]
        self.assertTrue(any("Removed staged stream file" in p for p in printed),
                        f"successful rm should log removal, got: {printed}")

    @patch("builtins.print")
    def test_plain_unlink_failure_warns(self, mock_print):
        """Non-sudo path: unlink raising OSError already warns; keep it."""
        cfg = {"verbose": 1, "dry_run": False, "local_sudo": False}
        path = Path("/definitely/not/here/stage.btrfs")
        # path.exists() is False -> silent no-op, no warning needed
        bs.remove_stage_file(path, cfg)
        printed = [str(a[0]) for a in mock_print.call_args_list if a[0]]
        self.assertFalse(any("Removed staged stream file" in p for p in printed))


if __name__ == "__main__":
    unittest.main()
