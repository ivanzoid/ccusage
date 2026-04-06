#!/usr/bin/env python3
"""Claude Code usage monitor — shows 5h and 7d rate limit bars."""

import json
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# ── Configuration ────────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
STATE_PATH        = Path.home() / ".ccusage_cache.json"
API_BASE = "https://api.anthropic.com"
USAGE_ENDPOINT = "/api/oauth/usage"
REFRESH_SECONDS = 180

# ── ANSI helpers ─────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"

def _color(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"

CYAN       = _color(40, 200, 220)
GREEN      = _color(80, 200, 80)
YELLOW     = _color(220, 200, 40)
ORANGE     = _color(255, 140, 0)
RED        = _color(220, 50, 50)
TIME_COLOR = _color(120, 160, 220)   # fixed color for reset countdowns
DIM        = "\033[2m"

def usage_color(pct: float, burn_ratio: float | None = None) -> str:
    """Color by burn rate (actual/expected) when available, else raw utilisation."""
    if burn_ratio is not None:
        if burn_ratio >= 2.0:
            return RED
        if burn_ratio >= 1.5:
            return ORANGE
        if burn_ratio >= 1.0:
            return YELLOW
        if burn_ratio >= 0.5:
            return GREEN
        return CYAN
    if pct >= 92:
        return RED
    if pct >= 80:
        return ORANGE
    if pct >= 65:
        return YELLOW
    if pct >= 40:
        return GREEN
    return CYAN

# ── Auth ──────────────────────────────────────────────────────────────────────

def _load_credentials_file() -> tuple:
    """Return (credentials_dict, error_str)."""
    try:
        with open(CREDENTIALS_PATH) as f:
            data = json.load(f)
        if "claudeAiOauth" not in data:
            return None, "No OAuth credentials in ~/.claude/.credentials.json"
        return data["claudeAiOauth"], None
    except FileNotFoundError:
        return None, "~/.claude/.credentials.json not found — run 'claude' first"
    except Exception as e:
        return None, str(e)

def _extract_macos_keychain() -> tuple:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout.strip())
        if "claudeAiOauth" not in data:
            return None, "No OAuth credentials in Keychain"
        return data["claudeAiOauth"], None
    except Exception as e:
        return None, str(e)

def _is_token_expired(creds: dict) -> bool:
    expires_at = creds.get("expiresAt", 0)
    now_ms = datetime.now().timestamp() * 1000
    return now_ms >= (expires_at - 5 * 60 * 1000)

def load_auth_headers() -> tuple:
    """Return (headers_dict, error_str)."""
    creds, err = _load_credentials_file()

    if err and platform.system() == "Darwin":
        creds, err = _extract_macos_keychain()

    if err:
        return None, err

    if _is_token_expired(creds):
        if platform.system() == "Darwin":
            fresh, err2 = _extract_macos_keychain()
            if not err2:
                creds = fresh
            else:
                return None, "Token expired — run 'claude' to refresh"
        else:
            return None, "Token expired — run 'claude' to refresh"

    return {
        "Authorization": f'Bearer {creds["accessToken"]}',
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-usage/1.0",
    }, None

# ── API ───────────────────────────────────────────────────────────────────────

_consecutive_429s = 0
_backoff_until = 0.0

def _parse_retry_after(resp) -> float | None:
    """Return seconds to wait from a 429 response, or None if not specified."""
    header = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            try:
                dt = parsedate_to_datetime(header)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass
    try:
        body = resp.json()
        for path in [("retry_after",), ("error", "retry_after"), ("retryAfter",)]:
            val = body
            for key in path:
                val = val.get(key) if isinstance(val, dict) else None
            if val is not None:
                return float(val)
    except Exception:
        pass
    return None

def fetch_usage(headers: dict) -> tuple:
    """Return (usage_dict, error_str)."""
    global _consecutive_429s, _backoff_until

    if time.time() < _backoff_until:
        remaining = _backoff_until - time.time()
        return None, f"Rate-limited — retry in {_format_relative(remaining)}"

    url = f"{API_BASE}{USAGE_ENDPOINT}"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    return None, "API returned invalid JSON"
                if not isinstance(payload, dict):
                    return None, "API returned an unexpected response"
                _consecutive_429s = 0
                _backoff_until = 0.0
                return payload, None
            elif resp.status_code == 429:
                api_wait = _parse_retry_after(resp)
                if api_wait is not None and api_wait > 0:
                    duration = api_wait
                else:
                    duration = min(300 * (2 ** _consecutive_429s), 3600)
                    _consecutive_429s += 1
                _backoff_until = time.time() + duration
                return None, f"Rate-limited — retry in {_format_relative(duration)}"
            elif resp.status_code == 401:
                return None, "Unauthorized (401) — run 'claude' to re-authenticate"
            else:
                return None, f"API error {resp.status_code}"
        except requests.exceptions.RequestException as e:
            return None, f"Network error: {e}"
    return None, "Failed after 3 attempts"

# ── Time formatting ───────────────────────────────────────────────────────────

def _format_relative(seconds: float) -> str:
    """Return human-readable relative duration, e.g. '2h 30m' or '5d 12h'."""
    if seconds <= 0:
        return "now"
    s = max(1, int(seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes = s // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else (f"{s}s" if s else "now")

def _format_absolute(reset_dt: datetime) -> str:
    """Return absolute time: HH:MM if today, else 'Mon HH:MM'."""
    now = datetime.now(timezone.utc)
    local_reset = reset_dt.astimezone()
    local_now = now.astimezone()
    if local_reset.date() == local_now.date():
        return local_reset.strftime("%H:%M")
    return local_reset.strftime("%a %H:%M")

def _parse_reset(resets_at: str) -> datetime | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at)
    except ValueError:
        return None

def _format_timestamp(dt: datetime) -> str:
    """Return a local wall-clock timestamp for status updates."""
    return dt.astimezone().strftime("%H:%M")

# ── Bar drawing ───────────────────────────────────────────────────────────────

FILLED = "█"
EMPTY  = "░"
MARKER = "│"  # time-position indicator

def _build_bar_str(bar_width: int, filled: int, color: str, time_pos: int | None) -> str:
    """Build bar string, optionally inserting a │ marker at `time_pos`."""
    if time_pos is None:
        return color + FILLED * filled + DIM + EMPTY * (bar_width - filled) + RESET

    time_pos = max(0, min(time_pos, bar_width - 1))
    parts = []
    cur = None  # tracks last-emitted ANSI code to avoid redundant emissions

    def _emit(code: str) -> None:
        nonlocal cur
        if cur != code:
            parts.append(code)
            cur = code

    for i in range(bar_width):
        if i == time_pos:
            parts.append(RESET + BOLD + MARKER + RESET)
            cur = None  # force re-emit on next char
        else:
            _emit(color if i < filled else DIM)
            parts.append(FILLED if i < filled else EMPTY)

    parts.append(RESET)
    return "".join(parts)


def _draw_bar(label: str, pct: float, reset_dt: datetime | None, bar_width: int, window_s: int) -> str:
    """Return a single colored bar line (no trailing newline)."""
    pct = max(0.0, min(pct, 100.0))
    now_utc = datetime.now(timezone.utc)

    burn_ratio = None
    elapsed_frac = None
    secs_left = None
    if reset_dt:
        secs_left = max(0.0, (reset_dt - now_utc).total_seconds())
        elapsed_frac = max(0.0, (window_s - secs_left) / window_s)
        if elapsed_frac >= 0.1:
            burn_ratio = pct / (elapsed_frac * 100.0)

    filled   = round(bar_width * pct / 100)
    time_pos = round(bar_width * elapsed_frac) if elapsed_frac is not None else None
    color    = usage_color(pct, burn_ratio)
    bar      = _build_bar_str(bar_width, filled, color, time_pos)

    pct_str = f"{round(pct):3d}%"

    if reset_dt and secs_left is not None:
        rel  = _format_relative(secs_left)
        abso = _format_absolute(reset_dt)
        reset_str = f"  {TIME_COLOR}in {rel}{RESET} {DIM}({abso}){RESET}"
    else:
        reset_str = ""

    return f"{BOLD}{label}{RESET} {color}{BOLD}{pct_str}{RESET} {bar}{reset_str}"

# ── State persistence ────────────────────────────────────────────────────────

def _save_state(usage: dict | None, fetched_at: datetime | None) -> None:
    """Persist usage + backoff state so the next run can resume without an immediate API call."""
    if usage is None or fetched_at is None:
        return
    try:
        STATE_PATH.write_text(json.dumps({
            "saved_at":      fetched_at.isoformat(),
            "backoff_until": _backoff_until,
            "usage":         usage,
        }))
    except Exception:
        pass


def _load_state() -> tuple | None:
    """Return (usage, saved_at_dt, initial_fetch_delay_s) or None if cache is absent/stale."""
    try:
        state     = json.loads(STATE_PATH.read_text())
        saved_at  = datetime.fromisoformat(state["saved_at"])
        usage     = state.get("usage")
        if not isinstance(usage, dict):
            return None
        age_s     = (datetime.now(timezone.utc) - saved_at).total_seconds()
        # Restore backoff if it hasn't expired yet
        backoff = float(state.get("backoff_until", 0))
        if backoff > time.time():
            global _backoff_until
            _backoff_until = backoff
        # Delay first fetch by however much of REFRESH_SECONDS is still remaining
        delay = max(0.0, REFRESH_SECONDS - age_s)
        return usage, saved_at, delay
    except Exception:
        return None


# ── Render loop ───────────────────────────────────────────────────────────────

_first_draw = True
_last_render_args: tuple | None = None
_last_line_count = 0
_resize_pending = False

def _clear_lines(n: int):
    """Move cursor up n lines and clear each."""
    sys.stdout.write("\r\033[2K")  # clear current line (cursor may be mid-line)
    for _ in range(n - 1):
        sys.stdout.write("\033[1A\033[2K")

def render(usage: dict | None, top_status: str = "", bottom_status: str = ""):
    global _first_draw, _last_render_args, _last_line_count
    _last_render_args = (usage, top_status, bottom_status)

    term_w = shutil.get_terminal_size((80, 24)).columns

    # Fixed overhead: label(2) + space(1) + pct(4) + space(1) + reset(~22 max)
    overhead = 2 + 1 + 4 + 1 + 22  # label + pct + bar-gap + reset
    bar_w = max(10, term_w - overhead)

    lines = []

    if top_status:
        lines.append(top_status)

    if usage is not None:
        fh = usage.get("five_hour", {})
        sd = usage.get("seven_day", {})

        fh_pct = float(fh.get("utilization", 0))
        sd_pct = float(sd.get("utilization", 0))
        fh_reset = _parse_reset(fh.get("resets_at", ""))
        sd_reset = _parse_reset(sd.get("resets_at", ""))

        lines.append(_draw_bar("5h", fh_pct, fh_reset, bar_w, 5 * 3600))
        lines.append(_draw_bar("7d", sd_pct, sd_reset, bar_w, 7 * 86400))
    else:
        lines.append("Fetching…")

    if bottom_status:
        lines.append(bottom_status)

    if not _first_draw:
        _clear_lines(_last_line_count)
    else:
        _first_draw = False

    _last_line_count = len(lines)

    for i, line in enumerate(lines):
        end = "\n" if i < len(lines) - 1 else ""
        print(line, end=end, flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def _interruptible_sleep(seconds: float):
    """Sleep for `seconds`, re-rendering immediately on terminal resize."""
    global _resize_pending
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _resize_pending:
            _resize_pending = False
            if _last_render_args is not None:
                render(*_last_render_args)
        time.sleep(0.1)


def main():
    last_usage: dict | None = None
    last_success_at: datetime | None = None

    def _on_exit(*_):
        _save_state(last_usage, last_success_at)
        print()
        sys.exit(0)

    def _on_resize(*_):
        global _resize_pending
        _resize_pending = True

    signal.signal(signal.SIGINT, _on_exit)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, _on_resize)

    # Restore cached state so we don't hammer the API right after restart
    cached = _load_state()
    if cached is not None:
        last_usage, last_success_at, initial_delay = cached
    else:
        initial_delay = 0.0

    next_fetch_at   = time.time() + initial_delay
    last_headers    = None
    last_header_fetch = 0.0

    while True:
        now = time.time()
        err = None

        if now >= next_fetch_at:
            # Refresh headers every 4 minutes (tokens live ~1h)
            if now - last_header_fetch > 240:
                new_headers, header_err = load_auth_headers()
                if header_err is None:
                    last_headers = new_headers
                    last_header_fetch = now
                else:
                    err = header_err

            if err is None:
                data, fetch_err = fetch_usage(last_headers)
                err = fetch_err
                if data is not None:
                    last_usage = data
                    last_success_at = datetime.now(timezone.utc)

            next_fetch_at = time.time() + REFRESH_SECONDS

        # ── single status line above bars ───────────────────────────────────
        if last_success_at is not None:
            age_seconds = time.time() - last_success_at.timestamp()
            if age_seconds >= 60:
                age_str = _format_relative(age_seconds)
                status = f"{DIM}synced {age_str} ago ({_format_timestamp(last_success_at)}){RESET}"
            else:
                status = ""
            if err:
                status += f"  {RED}{err}{RESET}" if status else f"{RED}{err}{RESET}"
        elif err:
            status = f"{RED}{err}{RESET}"
        else:
            status = f"{DIM}Waiting for first successful fetch…{RESET}"

        render(last_usage, top_status=status)

        sleep_s = min(60, max(0.1, next_fetch_at - time.time()))
        _interruptible_sleep(sleep_s)

if __name__ == "__main__":
    main()
