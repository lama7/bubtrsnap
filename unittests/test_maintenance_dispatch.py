#!/usr/bin/env python3
"""Tests for maintenance-subcommand dispatch.

`bubtrsnap <archive>...` (legacy) and `bubtrsnap <subcommand> ...`
(backup/list/prune/rebuild) are distinguished by _maint_first_positional(),
which scans argv for the first positional token while skipping options and
their values.  Flags declared with action="store_true" take no value, so they
must NOT be listed as value-takers -- otherwise the scan consumes the
subcommand token and the run silently falls through to the legacy backup path.
"""

from __future__ import annotations

import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
if not _path.is_file():
    raise FileNotFoundError(f"Cannot find bubtrsnap.py at {_path}")
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()

STORE_TRUE_FLAGS = ("--snaps-only", "--local-sudo", "--remote-sudo", "--rsync")
VALUE_FLAGS = ("--config", "--snapshot-dir", "--backup-dir", "--export-file",
               "--import-file", "--stage-file", "--export-dir", "--import-dir",
               "--stage-dir", "--remote-host", "--remote-path", "--rsync-opts",
               "--keep-hourly", "--keep-daily", "--keep-weekly",
               "--keep-monthly", "--keep-yearly", "--week-startday", "--ts")


class TestMaintFirstPositional(unittest.TestCase):
    def test_store_true_flags_do_not_swallow_the_subcommand(self):
        for flag in STORE_TRUE_FLAGS:
            got = bs._maint_first_positional([flag, "list", "home"])
            self.assertEqual(
                got, 1,
                f"{flag} is store_true (takes no value); the scan must not "
                f"consume 'list' as its value (got {got})",
            )

    def test_store_true_flag_then_prune(self):
        self.assertEqual(bs._maint_first_positional(["--snaps-only", "prune", "home"]), 1)

    def test_value_flags_still_consume_their_value(self):
        for flag in VALUE_FLAGS:
            got = bs._maint_first_positional([flag, "/some/value", "list", "home"])
            self.assertEqual(got, 2, f"{flag} takes a value; 'list' is at index 2 (got {got})")

    def test_optional_value_flag(self):
        self.assertEqual(bs._maint_first_positional(["-v", "2", "list", "home"]), 2)
        self.assertEqual(bs._maint_first_positional(["-v", "list", "home"]), 1)

    def test_legacy_form_still_returns_none(self):
        self.assertIsNone(bs._maint_first_positional(["home=/home"]))
        self.assertIsNone(bs._maint_first_positional(["--snaps-only", "home=/home"]))


class TestMainDispatch(unittest.TestCase):
    """End-to-end: main() must route to the subcommand, not legacy backup."""

    @mock.patch.object(bs, "load_and_resolve_archives")
    @mock.patch.object(bs, "_run_list")
    @mock.patch.object(bs, "_run_backup")
    def test_list_after_store_true_flag(self, m_backup, m_list, m_load):
        m_list.return_value = 0
        m_backup.return_value = 0
        m_load.return_value = ({}, [{"name": "home", "subvolume": "/home"}])
        with mock.patch.object(sys, "argv", ["bubtrsnap", "--snaps-only", "list", "home"]):
            try:
                bs.main()
            except SystemExit as exc:
                self.fail(f"main() fell through to the legacy parser: {exc}")
        self.assertTrue(m_list.called, "main() dispatched to _run_backup instead of _run_list")
        self.assertFalse(m_backup.called)

    @mock.patch.object(bs, "load_and_resolve_archives")
    @mock.patch.object(bs, "_run_prune")
    @mock.patch.object(bs, "_run_backup")
    def test_prune_after_local_sudo(self, m_backup, m_prune, m_load):
        m_prune.return_value = 0
        m_backup.return_value = 0
        m_load.return_value = ({}, [{"name": "home", "subvolume": "/home"}])
        with mock.patch.object(sys, "argv", ["bubtrsnap", "--local-sudo", "prune", "home", "--yes"]):
            try:
                bs.main()
            except SystemExit as exc:
                self.fail(f"main() fell through to the legacy parser: {exc}")
        self.assertTrue(m_prune.called)
        self.assertFalse(m_backup.called)


if __name__ == "__main__":
    unittest.main()
