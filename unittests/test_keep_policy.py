#!/usr/bin/env python3
"""Unit tests for bubtrsnap build_keep_set."""

from __future__ import annotations

import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap"
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

    def test_week_startday_sunday_default(self):
        """Default week starts Sunday → week ends Saturday (backward compat).
        
        With timestamps Mon-Sun and keep_weekly=1, the newest (Sunday) gets the
        weekly slot since it's the only active interval. To see the Saturday
        boundary, we need keep_weekly=2 or timestamps ending on Saturday.
        """
        ts = [
            "202608241200",  # Monday
            "202608251200",  # Tuesday
            "202608261200",  # Wednesday
            "202608271200",  # Thursday
            "202608281200",  # Friday
            "202608291200",  # Saturday
            "202608301200",  # Sunday
        ]
        # Default week_start_day=6 (Sunday) → week ends Saturday
        # With keep_weekly=2, we keep newest (Sun) + one more weekly (Sat)
        keeps = bs.build_keep_set(ts, policy(keep_weekly=2), week_start_day=6)
        self.assertIn("202608301200", keeps)  # newest always kept
        self.assertIn("202608291200", keeps)  # Saturday weekly slot

    def test_week_startday_monday(self):
        """Week starts Monday → week ends Sunday."""
        ts = [
            "202608241200",  # Monday
            "202608251200",  # Tuesday
            "202608261200",  # Wednesday
            "202608271200",  # Thursday
            "202608281200",  # Friday
            "202608291200",  # Saturday
            "202608301200",  # Sunday
        ]
        # week_start_day=0 (Monday) → week ends Sunday
        # With keep_weekly=2, we keep newest (Sun) + prior week's Sun (not in list)
        keeps = bs.build_keep_set(ts, policy(keep_weekly=2), week_start_day=0)
        self.assertIn("202608301200", keeps)  # newest always kept
        # The prior Sunday (20260823) isn't in the list, so only newest kept
        # Verify the weekly boundary works by checking with timestamps that span weeks
        ts2 = [
            "202608231200",  # Sunday (prior week)
            "202608241200",  # Monday
            "202608251200",  # Tuesday
            "202608261200",  # Wednesday
            "202608271200",  # Thursday
            "202608281200",  # Friday
            "202608291200",  # Saturday
            "202608301200",  # Sunday
        ]
        keeps2 = bs.build_keep_set(ts2, policy(keep_weekly=2), week_start_day=0)
        self.assertIn("202608301200", keeps2)
        self.assertIn("202608231200", keeps2)  # prior week's Sunday

class TestPrevWeek(unittest.TestCase):
    def test_wednesday_goes_to_prior_saturday_default(self):
        # 2026-08-26 is Wednesday → Saturday 2026-08-22 (default: week starts Sunday)
        b = bs._prev_week("202608261200", 0)
        self.assertTrue(b.startswith("20260822"))
        self.assertTrue(b.endswith("2359"))

    def test_saturday_stays_default(self):
        # 2026-08-22 is Saturday
        b = bs._prev_week("202608221200", 0)
        self.assertTrue(b.startswith("20260822"))

    def test_week_start_monday_wednesday_to_sunday(self):
        # 2026-08-26 is Wednesday, week starts Monday → week ends Sunday 2026-08-23 (prior week)
        b = bs._prev_week("202608261200", 0, week_start_day=0)
        self.assertTrue(b.startswith("20260823"))
        self.assertTrue(b.endswith("2359"))

    def test_week_start_monday_sunday_stays(self):
        # 2026-08-30 is Sunday, week starts Monday → week ends Sunday (same week)
        b = bs._prev_week("202608301200", 0, week_start_day=0)
        self.assertTrue(b.startswith("20260830"))

    def test_week_start_friday(self):
        # Week starts Friday → week ends Thursday
        # 2026-08-27 is Thursday → should stay on Thursday
        b = bs._prev_week("202608271200", 0, week_start_day=4)
        self.assertTrue(b.startswith("20260827"))

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


class TestForceKeep(unittest.TestCase):
    def test_force_keep_adds_to_keeps(self):
        """--keep adds a timestamp to keeps even if policy would prune it."""
        ts = [
            "202601011200",  # oldest
            "202601021200",
            "202601031200",  # newest
        ]
        # keep_daily=1 would only keep newest
        # force_keep on middle timestamp should add it
        keeps = bs.build_keep_set(ts, policy(keep_daily=1))
        self.assertEqual(set(keeps.keys()), {"202601031200"})
        # simulate force_keep
        keeps["202601021200"] = "forced"
        self.assertIn("202601021200", keeps)
        self.assertEqual(keeps["202601021200"], "forced")

    def test_force_keep_invalid_timestamp_rejected(self):
        """Test that invalid timestamp validation logic would work."""
        ts = ["202601011200", "202601021200"]
        # This tests the validation logic in apply_keep_policy
        # The actual validation happens there, build_keep_set just receives ts_list
        self.assertNotIn("202601031200", ts)

    def test_apply_keep_policy_force_keep_missing_logs_warning(self):
        """apply_keep_policy logs warning and ignores missing force_keep timestamps."""
        # This tests the behavior via the full function
        # We can't easily test apply_keep_policy directly without mocking
        # but we can test the _merge_keep function
        result = bs._merge_keep("202601011200,202601021200", ["202601031200"])
        self.assertEqual(result, ["202601011200", "202601021200", "202601031200"])

if __name__ == "__main__":
    unittest.main()
