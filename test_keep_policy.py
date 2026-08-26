#!/usr/bin/env python3
"""Unit tests for bubtrsnap build_keep_set."""

from __future__ import annotations

import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

def _load():
    path = Path(__file__).resolve().parent / "bubtrsnap"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()

bs = _load()

def policy(**kw):
    base = dict(
        keep_hourly=0, keep_daily=0, keep_weekly=0,
        keep_monthly=0, keep_yearly=0,
    )
    base.update(kw)
    return base

class TestBuildKeepSet(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bs.build_keep_set([], policy(keep_daily=3)), {})

    def test_total_zero_keeps_newest_only(self):
        ts = ["202601011200", "202601021200"]
        keeps = bs.build_keep_set(ts, policy())
        self.assertEqual(keeps, {"202601021200": "d"})

    def test_single_daily(self):
        ts = ["202601011200", "202601021200", "202601031200"]
        keeps = bs.build_keep_set(ts, policy(keep_daily=1))
        self.assertEqual(set(keeps), {"202601031200"})

    def test_hourly_two(self):
        ts = ["202601011000", "202601011100", "202601011200"]
        keeps = bs.build_keep_set(ts, policy(keep_hourly=2))
        self.assertIn("202601011200", keeps)
        self.assertEqual(len(keeps), 2)

    def test_daily_two_across_days(self):
        ts = [
            "202601011000",
            "202601011200",
            "202601021000",
            "202601031000",
        ]
        keeps = bs.build_keep_set(ts, policy(keep_daily=2))
        self.assertIn("202601031000", keeps)
        self.assertEqual(len(keeps), 2)

    def test_newest_always_present_when_policy_nonzero(self):
        ts = ["202601010000", "202601020000", "202601030000"]
        keeps = bs.build_keep_set(ts, policy(keep_weekly=1))
        self.assertIn("202601030000", keeps)

class TestPrevWeekSaturday(unittest.TestCase):
    def test_wednesday_goes_to_prior_saturday(self):
        # 2026-08-26 is Wednesday → Saturday 2026-08-22
        b = bs._prev_week("202608261200", 0)
        self.assertTrue(b.startswith("20260822"))
        self.assertTrue(b.endswith("2359"))

    def test_saturday_stays(self):
        # 2026-08-22 is Saturday
        b = bs._prev_week("202608221200", 0)
        self.assertTrue(b.startswith("20260822"))

class TestArchiveRegex(unittest.TestCase):
    def test_match(self):
        m = bs._ARCHIVE_TS_RE.match("home.202601011200")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "home")

    def test_twelve_digits_required(self):
        self.assertIsNone(bs._ARCHIVE_TS_RE.match("home.20260101"))

    def test_dotted_archive_name(self):
        m = bs._ARCHIVE_TS_RE.match("home.backup.202601011200")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "home.backup")
        self.assertEqual(m.group(2), "202601011200")

if __name__ == "__main__":
    unittest.main()
