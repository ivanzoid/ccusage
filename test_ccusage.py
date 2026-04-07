"""Tests for ccusage.py"""

import json
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import ccusage
from ccusage import (
    _format_relative,
    _format_absolute,
    _parse_retry_after,
    _build_bar_str,
    _save_state,
    _load_state,
    _strip_ansi,
    _visual_rows,
    fetch_usage,
    usage_color,
    _draw_bar,
    CYAN, GREEN, YELLOW, ORANGE, RED, TIME_COLOR, RESET, BOLD, DIM,
    FILLED, EMPTY, MARKER,
    STATE_PATH,
)


class TestFormatRelative(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_format_relative(0), "now")

    def test_negative(self):
        self.assertEqual(_format_relative(-5), "now")

    def test_sub_minute(self):
        self.assertEqual(_format_relative(30), "1m")
        self.assertEqual(_format_relative(1), "1m")

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
        # High raw pct but low burn_ratio → cyan, unless >=95% which forces red
        self.assertEqual(usage_color(90, burn_ratio=0.1), CYAN)

    def test_extreme_utilization_always_red(self):
        # >=95% forces RED regardless of burn_ratio
        self.assertEqual(usage_color(95, burn_ratio=0.1), RED)


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
        # 1 min elapsed of 5h → elapsed_frac ≈ 0.003 < 0.02 → fallback to raw pct
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

    def test_reset_str_uses_time_color(self):
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        line = _draw_bar("5h", 50.0, reset_dt, 20, 5 * 3600)
        self.assertIn(TIME_COLOR, line)
        self.assertIn("in ", line)


class TestParseRetryAfter(unittest.TestCase):
    def _mock_resp(self, headers=None, json_body=None):
        resp = MagicMock()
        resp.headers = headers or {}
        if json_body is not None:
            resp.json.return_value = json_body
        else:
            resp.json.side_effect = ValueError("no json")
        return resp

    def test_numeric_header(self):
        resp = self._mock_resp({"Retry-After": "120"})
        self.assertEqual(_parse_retry_after(resp), 120.0)

    def test_lowercase_header(self):
        resp = self._mock_resp({"retry-after": "60"})
        self.assertEqual(_parse_retry_after(resp), 60.0)

    def test_json_retry_after(self):
        resp = self._mock_resp(json_body={"retry_after": 45})
        self.assertEqual(_parse_retry_after(resp), 45.0)

    def test_nested_json_retry_after(self):
        resp = self._mock_resp(json_body={"error": {"retry_after": 30}})
        self.assertEqual(_parse_retry_after(resp), 30.0)

    def test_no_retry_info(self):
        resp = self._mock_resp()
        self.assertIsNone(_parse_retry_after(resp))


class TestFetchUsage(unittest.TestCase):
    HEADERS = {"Authorization": "Bearer test-token"}
    GOOD_PAYLOAD = {
        "five_hour": {"utilization": 42.0, "resets_at": "2026-04-06T12:00:00+00:00"},
        "seven_day": {"utilization": 15.0, "resets_at": "2026-04-10T00:00:00+00:00"},
    }

    def setUp(self):
        # Reset module-level backoff state before each test
        ccusage._consecutive_429s = 0
        ccusage._backoff_until = 0.0

    def _mock_get(self, status_code, json_body=None, headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = headers or {}
        if json_body is not None:
            resp.json.return_value = json_body
        else:
            resp.json.side_effect = ValueError("no json")
        return resp

    def test_success_200(self):
        with patch("ccusage.requests.get", return_value=self._mock_get(200, self.GOOD_PAYLOAD)):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(err)
        self.assertEqual(data["five_hour"]["utilization"], 42.0)
        self.assertEqual(ccusage._consecutive_429s, 0)
        self.assertEqual(ccusage._backoff_until, 0.0)

    def test_success_clears_backoff(self):
        ccusage._consecutive_429s = 3
        ccusage._backoff_until = 0.0  # already expired
        with patch("ccusage.requests.get", return_value=self._mock_get(200, self.GOOD_PAYLOAD)):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(err)
        self.assertEqual(ccusage._consecutive_429s, 0)

    def test_429_with_retry_after_header(self):
        resp = self._mock_get(429, headers={"Retry-After": "90"})
        resp.json.side_effect = ValueError("no json")
        with patch("ccusage.requests.get", return_value=resp):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("Rate-limited", err)
        self.assertGreater(ccusage._backoff_until, time.time() + 80)

    def test_429_without_retry_after_uses_exponential_backoff(self):
        resp = self._mock_get(429)
        resp.json.side_effect = ValueError("no json")
        with patch("ccusage.requests.get", return_value=resp):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("Rate-limited", err)
        # First backoff: 300 * 2^0 = 300s; consecutive_429s bumped to 1
        self.assertEqual(ccusage._consecutive_429s, 1)
        self.assertGreater(ccusage._backoff_until, time.time() + 290)

    def test_429_exponential_backoff_increases(self):
        ccusage._consecutive_429s = 2
        resp = self._mock_get(429)
        resp.json.side_effect = ValueError("no json")
        with patch("ccusage.requests.get", return_value=resp):
            data, err = fetch_usage(self.HEADERS)
        # backoff = 300 * 2^2 = 1200s, consecutive_429s → 3
        self.assertEqual(ccusage._consecutive_429s, 3)
        self.assertGreater(ccusage._backoff_until, time.time() + 1190)

    def test_429_backoff_capped_at_3600(self):
        ccusage._consecutive_429s = 20  # would overflow without cap
        resp = self._mock_get(429)
        resp.json.side_effect = ValueError("no json")
        with patch("ccusage.requests.get", return_value=resp):
            fetch_usage(self.HEADERS)
        self.assertLessEqual(ccusage._backoff_until, time.time() + 3601)

    def test_skips_request_during_backoff(self):
        ccusage._backoff_until = time.time() + 500
        with patch("ccusage.requests.get") as mock_get:
            data, err = fetch_usage(self.HEADERS)
        mock_get.assert_not_called()
        self.assertIsNone(data)
        self.assertIn("Rate-limited", err)

    def test_401_unauthorized(self):
        with patch("ccusage.requests.get", return_value=self._mock_get(401)):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("401", err)

    def test_other_http_error(self):
        with patch("ccusage.requests.get", return_value=self._mock_get(500)):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("500", err)

    def test_network_error(self):
        import requests as req_lib
        with patch("ccusage.requests.get", side_effect=req_lib.exceptions.ConnectionError("refused")):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("Network error", err)

    def test_invalid_json_response(self):
        resp = self._mock_get(200)
        resp.json.side_effect = ValueError("bad json")
        with patch("ccusage.requests.get", return_value=resp):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIn("invalid JSON", err)

    def test_non_dict_response(self):
        resp = self._mock_get(200)
        resp.json.return_value = ["unexpected", "list"]
        with patch("ccusage.requests.get", return_value=resp):
            data, err = fetch_usage(self.HEADERS)
        self.assertIsNone(data)
        self.assertIsNotNone(err)


class TestRender(unittest.TestCase):
    """Smoke-tests for render() — checks line count and key strings."""

    def setUp(self):
        ccusage._first_draw = True

    def _capture_render(self, *args, **kwargs):
        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ccusage.render(*args, **kwargs)
        return buf.getvalue()

    def test_render_with_usage(self):
        usage = {
            "five_hour": {"utilization": 50.0, "resets_at": ""},
            "seven_day": {"utilization": 20.0, "resets_at": ""},
        }
        out = self._capture_render(usage)
        self.assertIn("5h", out)
        self.assertIn("7d", out)

    def test_render_without_usage_shows_fetching(self):
        out = self._capture_render(None)
        self.assertIn("Fetching", out)

    def test_render_top_status_included(self):
        out = self._capture_render(None, top_status="HELLO_STATUS")
        self.assertIn("HELLO_STATUS", out)

    def test_render_bottom_status_included(self):
        out = self._capture_render(None, bottom_status="BOTTOM_STATUS")
        self.assertIn("BOTTOM_STATUS", out)

    def test_render_synced_line_in_top_status(self):
        out = self._capture_render(
            {"five_hour": {"utilization": 30.0, "resets_at": ""},
             "seven_day": {"utilization": 10.0, "resets_at": ""}},
            top_status="\033[2msynced 5m ago (10:00)\033[0m",
        )
        self.assertIn("synced", out)
        self.assertIn("ago", out)

    def test_render_no_synced_line_when_top_status_empty(self):
        out = self._capture_render(
            {"five_hour": {"utilization": 30.0, "resets_at": ""},
             "seven_day": {"utilization": 10.0, "resets_at": ""}},
            top_status="",
        )
        self.assertNotIn("synced", out)

    def test_render_includes_marker_when_resets_at_known(self):
        reset_dt = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        usage = {
            "five_hour": {"utilization": 50.0, "resets_at": reset_dt},
            "seven_day": {"utilization": 20.0, "resets_at": reset_dt},
        }
        out = self._capture_render(usage)
        self.assertIn(ccusage.MARKER, out)

    def test_render_adapts_to_terminal_width(self):
        usage = {
            "five_hour": {"utilization": 50.0, "resets_at": ""},
            "seven_day": {"utilization": 20.0, "resets_at": ""},
        }
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=40)):
            out40 = self._capture_render(usage)
        ccusage._first_draw = True
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=120)):
            out120 = self._capture_render(usage)
        # Wider terminal → more bar characters
        bar_chars = lambda s: s.count("█") + s.count("░")
        self.assertGreater(bar_chars(out120), bar_chars(out40))


class TestStripAnsi(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(_strip_ansi("hello world"), "hello world")

    def test_removes_color_codes(self):
        self.assertEqual(_strip_ansi(f"{RED}error{RESET}"), "error")

    def test_removes_bold_dim(self):
        self.assertEqual(_strip_ansi(f"{BOLD}hi{DIM}there{RESET}"), "hithere")

    def test_empty_string(self):
        self.assertEqual(_strip_ansi(""), "")


class TestVisualRows(unittest.TestCase):
    def test_short_line_one_row(self):
        self.assertEqual(_visual_rows("hello", 80), 1)

    def test_empty_line_one_row(self):
        self.assertEqual(_visual_rows("", 80), 1)

    def test_exact_width_one_row(self):
        self.assertEqual(_visual_rows("x" * 80, 80), 1)

    def test_one_over_wraps(self):
        self.assertEqual(_visual_rows("x" * 81, 80), 2)

    def test_double_width(self):
        self.assertEqual(_visual_rows("x" * 160, 80), 2)

    def test_ansi_codes_not_counted(self):
        # 10 visible chars with ANSI wrapping — should be 1 row on 80-wide terminal
        line = f"{RED}{'x' * 10}{RESET}"
        self.assertEqual(_visual_rows(line, 80), 1)

    def test_long_status_with_ansi_wraps(self):
        # Simulate the real bug: a long status line with error that exceeds terminal width
        status = f"{DIM}synced 8h34m ago (04:37){RESET}  {RED}Token expired — run 'claude' to refresh{RESET}"
        visible = _strip_ansi(status)
        # On a 50-column terminal this should wrap
        self.assertEqual(_visual_rows(status, 50), 2)
        # On a 120-column terminal it fits in one row
        self.assertEqual(_visual_rows(status, 120), 1)


class TestRenderClearsWrappedLines(unittest.TestCase):
    """Verify render() tracks visual rows so wrapped lines get fully cleared."""

    def setUp(self):
        ccusage._first_draw = True
        ccusage._last_visual_rows = 0

    def _render_and_capture(self, usage, top_status="", term_w=80):
        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf), \
             patch("shutil.get_terminal_size", return_value=MagicMock(columns=term_w)):
            ccusage.render(usage, top_status=top_status)
        return buf.getvalue()

    def test_visual_rows_tracked_with_wrapping_status(self):
        """A status line wider than the terminal should increase _last_visual_rows."""
        usage = {
            "five_hour": {"utilization": 10.0, "resets_at": ""},
            "seven_day": {"utilization": 20.0, "resets_at": ""},
        }
        # Short status, narrow terminal — no wrapping: 3 logical = 3 visual
        self._render_and_capture(usage, top_status="ok", term_w=80)
        self.assertEqual(ccusage._last_visual_rows, 3)

    def test_visual_rows_increases_with_long_status(self):
        """A wrapping status line should count as 2+ visual rows."""
        usage = {
            "five_hour": {"utilization": 10.0, "resets_at": ""},
            "seven_day": {"utilization": 20.0, "resets_at": ""},
        }
        long_status = "synced 8h34m ago (04:37)  Token expired — run 'claude' to refresh"
        # On a 40-col terminal, this ~67-char line wraps to 2 visual rows
        # So total = 2 (status) + 1 (5h bar) + 1 (7d bar) = 4
        self._render_and_capture(usage, top_status=long_status, term_w=40)
        self.assertGreater(ccusage._last_visual_rows, 3)

    def test_second_render_emits_enough_clear_escapes(self):
        """On re-render with a wrapping status, enough \\033[1A sequences are emitted."""
        usage = {
            "five_hour": {"utilization": 10.0, "resets_at": ""},
            "seven_day": {"utilization": 20.0, "resets_at": ""},
        }
        long_status = "synced 8h34m ago (04:37)  Token expired — run 'claude' to refresh"
        # First render (sets _last_visual_rows)
        self._render_and_capture(usage, top_status=long_status, term_w=40)
        saved_rows = ccusage._last_visual_rows
        self.assertGreater(saved_rows, 3)

        # Second render — should clear saved_rows visual rows
        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf), \
             patch("shutil.get_terminal_size", return_value=MagicMock(columns=40)):
            ccusage.render(usage, top_status=long_status)
        output = buf.getvalue()
        # _clear_lines emits 1 \r\033[2K + (n-1) \033[1A\033[2K
        move_up_count = output.count("\033[1A")
        self.assertEqual(move_up_count, saved_rows - 1)


class TestBuildBarStr(unittest.TestCase):
    COLOR = "\033[38;2;80;200;80m"  # GREEN

    def test_no_marker_simple(self):
        s = _build_bar_str(10, 5, self.COLOR, None)
        self.assertEqual(s.count(FILLED), 5)
        self.assertEqual(s.count(EMPTY), 5)
        self.assertNotIn(MARKER, s)

    def test_no_marker_all_filled(self):
        s = _build_bar_str(10, 10, self.COLOR, None)
        self.assertEqual(s.count(FILLED), 10)
        self.assertEqual(s.count(EMPTY), 0)

    def test_no_marker_all_empty(self):
        s = _build_bar_str(10, 0, self.COLOR, None)
        self.assertEqual(s.count(FILLED), 0)
        self.assertEqual(s.count(EMPTY), 10)

    def test_marker_present(self):
        s = _build_bar_str(10, 5, self.COLOR, 3)
        self.assertIn(MARKER, s)

    def test_marker_reduces_filled_empty_by_one(self):
        # marker replaces one char slot → filled + empty = bar_width - 1
        s = _build_bar_str(10, 5, self.COLOR, 3)
        self.assertEqual(s.count(FILLED) + s.count(EMPTY), 9)

    def test_marker_at_zero(self):
        s = _build_bar_str(10, 5, self.COLOR, 0)
        self.assertIn(MARKER, s)
        self.assertEqual(s.count(FILLED) + s.count(EMPTY), 9)

    def test_marker_at_last(self):
        s = _build_bar_str(10, 5, self.COLOR, 9)
        self.assertIn(MARKER, s)

    def test_marker_clamped_below_zero(self):
        # time_pos < 0 → clamped to 0
        s = _build_bar_str(10, 5, self.COLOR, -5)
        self.assertIn(MARKER, s)

    def test_marker_clamped_above_width(self):
        # time_pos >= bar_width → clamped to bar_width - 1
        s = _build_bar_str(10, 5, self.COLOR, 20)
        self.assertIn(MARKER, s)

    def test_marker_in_filled_region(self):
        # time_pos < filled → marker is inside the filled section
        s = _build_bar_str(10, 7, self.COLOR, 3)
        # marker should appear before most of the filled chars
        marker_idx = s.index(MARKER)
        filled_after = s[marker_idx:].count(FILLED)
        self.assertGreater(filled_after, 0)

    def test_marker_in_empty_region(self):
        # time_pos > filled → marker is inside the empty section
        s = _build_bar_str(10, 3, self.COLOR, 7)
        marker_idx = s.index(MARKER)
        empty_after = s[marker_idx:].count(EMPTY)
        self.assertGreater(empty_after, 0)

    def test_draw_bar_includes_marker_with_reset_dt(self):
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        line = _draw_bar("5h", 50.0, reset_dt, 20, 5 * 3600)
        self.assertIn(MARKER, line)

    def test_draw_bar_no_marker_without_reset_dt(self):
        line = _draw_bar("5h", 50.0, None, 20, 5 * 3600)
        self.assertNotIn(MARKER, line)


class TestStateCache(unittest.TestCase):
    USAGE = {
        "five_hour": {"utilization": 55.0, "resets_at": "2026-04-06T15:00:00+00:00"},
        "seven_day": {"utilization": 20.0, "resets_at": "2026-04-10T00:00:00+00:00"},
    }

    def setUp(self):
        ccusage._backoff_until = 0.0
        ccusage._consecutive_429s = 0
        if STATE_PATH.exists():
            STATE_PATH.unlink()

    def tearDown(self):
        if STATE_PATH.exists():
            STATE_PATH.unlink()

    NOW = datetime.now(timezone.utc)

    def test_save_creates_file(self):
        _save_state(self.USAGE, self.NOW)
        self.assertTrue(STATE_PATH.exists())

    def test_save_none_usage_does_not_create_file(self):
        _save_state(None, self.NOW)
        self.assertFalse(STATE_PATH.exists())

    def test_save_none_fetched_at_does_not_create_file(self):
        _save_state(self.USAGE, None)
        self.assertFalse(STATE_PATH.exists())

    def test_round_trip_usage(self):
        _save_state(self.USAGE, self.NOW)
        result = _load_state()
        self.assertIsNotNone(result)
        usage, saved_at, delay = result
        self.assertEqual(usage["five_hour"]["utilization"], 55.0)

    def test_saved_at_matches_fetched_at(self):
        _save_state(self.USAGE, self.NOW)
        _, saved_at, _ = _load_state()
        self.assertAlmostEqual(saved_at.timestamp(), self.NOW.timestamp(), delta=0.001)

    def test_fresh_cache_gives_nonzero_delay(self):
        _save_state(self.USAGE, self.NOW)
        _, _, delay = _load_state()
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, ccusage.REFRESH_SECONDS)

    def test_old_cache_gives_zero_delay(self):
        # Pretend state was saved REFRESH_SECONDS + 10 seconds ago
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=ccusage.REFRESH_SECONDS + 10)).isoformat()
        STATE_PATH.write_text(json.dumps({
            "saved_at": old_time,
            "backoff_until": 0,
            "usage": self.USAGE,
        }))
        _, _, delay = _load_state()
        self.assertEqual(delay, 0.0)

    def test_backoff_restored_when_still_active(self):
        future_backoff = time.time() + 200
        STATE_PATH.write_text(json.dumps({
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "backoff_until": future_backoff,
            "usage": self.USAGE,
        }))
        _load_state()
        self.assertAlmostEqual(ccusage._backoff_until, future_backoff, delta=1)

    def test_expired_backoff_not_restored(self):
        past_backoff = time.time() - 10
        STATE_PATH.write_text(json.dumps({
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "backoff_until": past_backoff,
            "usage": self.USAGE,
        }))
        _load_state()
        self.assertEqual(ccusage._backoff_until, 0.0)

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(_load_state())

    def test_load_corrupt_file_returns_none(self):
        STATE_PATH.write_text("not json {{{")
        self.assertIsNone(_load_state())

    def test_load_no_usage_key_returns_none(self):
        STATE_PATH.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat()}))
        self.assertIsNone(_load_state())


if __name__ == "__main__":
    unittest.main()
