"""Tests for ccusage.py"""

import unittest
from datetime import datetime, timezone, timedelta

from ccusage import (
    _format_relative,
    _format_absolute,
    usage_color,
    _draw_bar,
    CYAN, GREEN, YELLOW, ORANGE, RED,
)


class TestFormatRelative(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_format_relative(0), "now")

    def test_negative(self):
        self.assertEqual(_format_relative(-5), "now")

    def test_sub_minute(self):
        self.assertEqual(_format_relative(30), "<1m")

    def test_minutes_only(self):
        self.assertEqual(_format_relative(90), "1m")
        self.assertEqual(_format_relative(3540), "59m")

    def test_hours_only(self):
        self.assertEqual(_format_relative(7200), "2h")

    def test_hours_and_minutes(self):
        self.assertEqual(_format_relative(7380), "2h3m")

    def test_hours_suppresses_zero_minutes(self):
        self.assertEqual(_format_relative(3600), "1h")

    def test_days_only(self):
        self.assertEqual(_format_relative(86400), "1d")
        self.assertEqual(_format_relative(86400 * 2), "2d")

    def test_days_and_hours(self):
        self.assertEqual(_format_relative(90000), "1d1h")

    def test_days_suppresses_zero_hours(self):
        self.assertEqual(_format_relative(86400 * 3), "3d")

    def test_no_spaces(self):
        result = _format_relative(7380)   # "2h3m"
        self.assertNotIn(" ", result)
        result = _format_relative(90000)  # "1d1h"
        self.assertNotIn(" ", result)


class TestUsageColor(unittest.TestCase):
    # Raw pct (no burn_ratio)
    def test_raw_cyan(self):
        self.assertEqual(usage_color(0), CYAN)
        self.assertEqual(usage_color(39.9), CYAN)

    def test_raw_green(self):
        self.assertEqual(usage_color(40), GREEN)
        self.assertEqual(usage_color(64.9), GREEN)

    def test_raw_yellow(self):
        self.assertEqual(usage_color(65), YELLOW)
        self.assertEqual(usage_color(79.9), YELLOW)

    def test_raw_orange(self):
        self.assertEqual(usage_color(80), ORANGE)
        self.assertEqual(usage_color(91.9), ORANGE)

    def test_raw_red(self):
        self.assertEqual(usage_color(92), RED)
        self.assertEqual(usage_color(150), RED)

    # Burn-ratio coloring
    def test_burn_cyan(self):
        self.assertEqual(usage_color(50, burn_ratio=0.0), CYAN)
        self.assertEqual(usage_color(50, burn_ratio=0.49), CYAN)

    def test_burn_green(self):
        self.assertEqual(usage_color(50, burn_ratio=0.5), GREEN)
        self.assertEqual(usage_color(50, burn_ratio=0.99), GREEN)

    def test_burn_yellow(self):
        self.assertEqual(usage_color(50, burn_ratio=1.0), YELLOW)
        self.assertEqual(usage_color(50, burn_ratio=1.49), YELLOW)

    def test_burn_orange(self):
        self.assertEqual(usage_color(50, burn_ratio=1.5), ORANGE)
        self.assertEqual(usage_color(50, burn_ratio=1.99), ORANGE)

    def test_burn_red(self):
        self.assertEqual(usage_color(50, burn_ratio=2.0), RED)
        self.assertEqual(usage_color(50, burn_ratio=10.0), RED)

    def test_burn_overrides_raw(self):
        # High raw pct but low burn_ratio → should be cyan not red
        self.assertEqual(usage_color(95, burn_ratio=0.1), CYAN)


class TestDrawBar(unittest.TestCase):
    def test_label_in_output(self):
        line = _draw_bar("5h", 50.0, None, 20, 5 * 3600)
        self.assertIn("5h", line)

    def test_pct_integer_format(self):
        line = _draw_bar("5h", 42.0, None, 20, 5 * 3600)
        self.assertIn("42%", line)
        self.assertNotIn("42.0", line)

    def test_pct_rounds(self):
        line = _draw_bar("5h", 42.6, None, 20, 5 * 3600)
        self.assertIn("43%", line)

    def test_pct_clamped_high(self):
        line = _draw_bar("5h", 150.0, None, 20, 5 * 3600)
        self.assertIn("100%", line)

    def test_pct_clamped_low(self):
        line = _draw_bar("5h", -5.0, None, 20, 5 * 3600)
        self.assertIn("  0%", line)

    def test_no_reset_str_without_dt(self):
        line = _draw_bar("5h", 50.0, None, 20, 5 * 3600)
        self.assertNotIn("in ", line)

    def test_reset_str_present_with_dt(self):
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        line = _draw_bar("5h", 50.0, reset_dt, 20, 5 * 3600)
        self.assertIn("in ", line)

    def test_bar_total_width(self):
        line = _draw_bar("5h", 50.0, None, 10, 5 * 3600)
        self.assertEqual(line.count("█") + line.count("░"), 10)

    def test_bar_filled_fraction(self):
        line = _draw_bar("5h", 0.0, None, 10, 5 * 3600)
        self.assertEqual(line.count("█"), 0)
        line = _draw_bar("5h", 100.0, None, 10, 5 * 3600)
        self.assertEqual(line.count("█"), 10)

    def test_burn_ratio_applied_late_in_window(self):
        # 1 min left of 5h → elapsed_frac ≈ 0.997; pct=10 → ratio ≈ 0.1 → CYAN
        reset_dt = datetime.now(timezone.utc) + timedelta(minutes=1)
        line = _draw_bar("5h", 10.0, reset_dt, 20, 5 * 3600)
        self.assertIn(CYAN, line)

    def test_burn_ratio_skipped_early_in_window(self):
        # 1 min elapsed of 5h → elapsed_frac ≈ 0.003 < 0.1 → fallback to raw pct
        # pct=80 → raw → ORANGE
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=4, minutes=59)
        line = _draw_bar("5h", 80.0, reset_dt, 20, 5 * 3600)
        self.assertIn(ORANGE, line)

    def test_over_budget_coloring(self):
        # 50% elapsed, 90% used → ratio=1.8 → ORANGE
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        line = _draw_bar("5h", 90.0, reset_dt, 20, 5 * 3600)
        self.assertIn(ORANGE, line)

    def test_on_budget_coloring(self):
        # 50% elapsed, 40% used → ratio=0.8 → GREEN
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        line = _draw_bar("5h", 40.0, reset_dt, 20, 5 * 3600)
        self.assertIn(GREEN, line)


if __name__ == "__main__":
    unittest.main()
