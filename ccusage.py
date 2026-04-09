#!/usr/bin/env python3
"""Claude Code usage monitor — shows 5h and 7d rate limit bars."""

import argparse
import json
import locale
import os
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

CREDENTIALS_PATH = Path(
    os.getenv("CCUSAGE_CREDENTIALS_PATH", str(Path.home() / ".claude" / ".credentials.json"))
)
STATE_PATH = Path(
    os.getenv("CCUSAGE_STATE_PATH", str(Path.home() / ".ccusage_cache.json"))
)
API_BASE = os.getenv("CCUSAGE_API_BASE", "https://api.anthropic.com")
USAGE_ENDPOINT = os.getenv("CCUSAGE_USAGE_ENDPOINT", "/api/oauth/usage")
EVENT_LOG_PATH = os.getenv("CCUSAGE_EVENT_LOG_PATH", str(Path.home() / ".ccusage_events.jsonl"))
REFRESH_SECONDS = 120

def _detect_12h() -> bool:
    try:
        return "%I" in locale.nl_langinfo(locale.T_FMT) or \
               "%l" in locale.nl_langinfo(locale.T_FMT)
    except Exception:
        return False

_USE_12H = _detect_12h()

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
    """Color by burn rate (actual/expected) when available, else raw utilisation.
    At extreme utilisation (>=95%) always returns RED — you're effectively blocked."""
    if pct >= 95:
        return RED
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
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "1m"

def _dim_separators(text: str, active_color: str = "") -> str:
    """Replace spaces between time units with a dim underscore separator."""
    if " " not in text:
        return text
    sep = f"{DIM}_{RESET}{active_color}"
    return text.replace(" ", sep)

def _fmt_time(dt: datetime) -> str:
    """Format time component respecting the system 12/24h preference."""
    if _USE_12H:
        return dt.strftime("%-I:%M %p")   # "1:30 PM"  (no leading zero)
    return dt.strftime("%H:%M")            # "13:30"

def _format_absolute(reset_dt: datetime) -> str:
    """Return absolute time: HH:MM if today, else 'Mon HH:MM'."""
    now = datetime.now(timezone.utc)
    local_reset = reset_dt.astimezone()
    local_now = now.astimezone()
    t = _fmt_time(local_reset)
    if local_reset.date() == local_now.date():
        return t
    return f"{local_reset.strftime('%a')} {t}"

def _parse_reset(resets_at: str) -> datetime | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at)
    except ValueError:
        return None

def _format_timestamp(dt: datetime) -> str:
    """Return a local wall-clock timestamp for status updates."""
    return _fmt_time(dt.astimezone())

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


def _build_reset_str(reset_dt: datetime | None, secs_left: float | None = None) -> str:
    """Build the reset countdown suffix shown after a bar."""
    if reset_dt is None:
        return ""
    if secs_left is None:
        secs_left = max(0.0, (reset_dt - datetime.now(timezone.utc)).total_seconds())
    rel = _dim_separators(_format_relative(secs_left), TIME_COLOR)
    abso = _format_absolute(reset_dt)
    return f"  {TIME_COLOR}in {rel}{RESET} {DIM}({abso}){RESET}"


def _draw_bar(
    label: str,
    pct: float,
    reset_dt: datetime | None,
    bar_width: int,
    window_s: int,
    now_utc: datetime | None = None,
    reset_str: str | None = None,
) -> str:
    """Return a single colored bar line (no trailing newline)."""
    pct = max(0.0, min(pct, 100.0))
    now_utc = now_utc or datetime.now(timezone.utc)

    burn_ratio = None
    elapsed_frac = None
    secs_left = None
    if reset_dt:
        secs_left = max(0.0, (reset_dt - now_utc).total_seconds())
        elapsed_frac = max(0.0, (window_s - secs_left) / window_s)
        if elapsed_frac >= 0.02:
            burn_ratio = pct / (elapsed_frac * 100.0)

    filled   = round(bar_width * pct / 100)
    time_pos = round(bar_width * elapsed_frac) if elapsed_frac is not None else None
    color    = usage_color(pct, burn_ratio)
    bar      = _build_bar_str(bar_width, filled, color, time_pos)

    pct_str = f"{round(pct):3d}%"

    if reset_str is None:
        reset_str = _build_reset_str(reset_dt, secs_left)

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


def _load_state(refresh_seconds: int = REFRESH_SECONDS) -> tuple | None:
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
        # Delay first fetch by however much of refresh_seconds is still remaining
        delay = max(0.0, refresh_seconds - age_s)
        return usage, saved_at, delay
    except Exception:
        return None


# ── Render loop ───────────────────────────────────────────────────────────────

_last_render_args: tuple | None = None
_last_render_start_row: int | None = None
_resize_pending = False


def _json_safe(value):
    """Convert values to JSON-serializable shapes for event logging."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class EventLogger:
    """Append runtime events to a JSONL file for later replay."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def log(self, event_type: str, **data) -> None:
        if self.path is None:
            return
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
        }
        event.update({k: _json_safe(v) for k, v in data.items()})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=True) + "\n")
        except Exception:
            pass


def replay_event_log(path: str | Path, speedup: float = 0.0) -> None:
    """Replay recorded render events from a JSONL event log."""
    prev_ts = None
    with Path(path).open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            event = json.loads(line)
            if speedup > 0 and prev_ts and event.get("ts"):
                cur_ts = datetime.fromisoformat(event["ts"])
                delay = (cur_ts - prev_ts).total_seconds() / speedup
                if delay > 0:
                    time.sleep(delay)
                prev_ts = cur_ts
            elif event.get("ts"):
                prev_ts = datetime.fromisoformat(event["ts"])
            if event.get("type") != "render":
                continue
            render(
                event.get("usage"),
                top_status=event.get("top_status", ""),
                bottom_status=event.get("bottom_status", ""),
            )

def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences to get visible text width."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)

def _visual_rows(line: str, term_w: int) -> int:
    """Number of visual rows a line occupies (>=1), accounting for wrapping."""
    w = len(_strip_ansi(line))
    if w == 0:
        return 1
    return max(1, (w + term_w - 1) // term_w)

def render(usage: dict | None, top_status: str = "", bottom_status: str = ""):
    global _last_render_args, _last_render_start_row
    _last_render_args = (usage, top_status, bottom_status)

    term_size = shutil.get_terminal_size((80, 24))
    term_w = term_size.columns
    term_h = term_size.lines

    lines = []

    lines.append(top_status)  # always reserve the line; may be empty

    if usage is not None:
        fh = usage.get("five_hour", {})
        sd = usage.get("seven_day", {})

        fh_pct = float(fh.get("utilization", 0))
        sd_pct = float(sd.get("utilization", 0))
        fh_reset = _parse_reset(fh.get("resets_at", ""))
        sd_reset = _parse_reset(sd.get("resets_at", ""))
        now_utc = datetime.now(timezone.utc)

        fh_secs_left = max(0.0, (fh_reset - now_utc).total_seconds()) if fh_reset else None
        sd_secs_left = max(0.0, (sd_reset - now_utc).total_seconds()) if sd_reset else None
        fh_reset_str = _build_reset_str(fh_reset, fh_secs_left)
        sd_reset_str = _build_reset_str(sd_reset, sd_secs_left)

        # Size the shared bar from the longest reset suffix actually present in
        # this frame. Staying below the last terminal column avoids auto-wrap.
        prefix_w = len("7d 100% ")
        reset_w = max(len(_strip_ansi(fh_reset_str)), len(_strip_ansi(sd_reset_str)))
        bar_w = max(1, term_w - prefix_w - reset_w - 1)

        lines.append(_draw_bar("5h", fh_pct, fh_reset, bar_w, 5 * 3600, now_utc, fh_reset_str))
        lines.append(_draw_bar("7d", sd_pct, sd_reset, bar_w, 7 * 86400, now_utc, sd_reset_str))
    else:
        lines.append("Fetching…")

    if bottom_status:
        lines.append(bottom_status)

    total_visual = sum(_visual_rows(line, term_w) for line in lines)
    start_row = max(1, term_h - total_visual + 1)
    clear_from = min(start_row, _last_render_start_row) if _last_render_start_row is not None else start_row
    # Clear from the earliest row occupied by either frame, then move to the
    # current frame's start row before painting.
    sys.stdout.write(f"\033[{clear_from};1H\033[J\033[{start_row};1H")

    for i, line in enumerate(lines):
        end = "\n" if i < len(lines) - 1 else ""
        print(line, end=end, flush=True)

    _last_render_start_row = start_row

# ── Main ──────────────────────────────────────────────────────────────────────

def _interruptible_sleep(seconds: float):
    """Sleep for `seconds`, waking early if a signal interrupts."""
    global _resize_pending
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _resize_pending:
            _resize_pending = False
            return
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description="Claude Code rate-limit monitor")
    parser.add_argument("--version", action="version", version="%(prog)s 1.6.1")
    parser.add_argument(
        "-i", "--interval",
        type=int,
        default=REFRESH_SECONDS,
        metavar="SECONDS",
        help=f"Poll interval in seconds (default: {REFRESH_SECONDS})",
    )
    parser.add_argument(
        "--event-log",
        default=None,
        metavar="PATH",
        const=EVENT_LOG_PATH,
        nargs="?",
        help=f"Write JSONL runtime events to PATH (default path when omitted: {EVENT_LOG_PATH})",
    )
    parser.add_argument(
        "--replay-log",
        metavar="PATH",
        help="Replay render events from a JSONL log instead of running live",
    )
    parser.add_argument(
        "--replay-speed",
        type=float,
        default=0.0,
        metavar="FACTOR",
        help="Replay timing speedup factor; 0 replays immediately",
    )
    args = parser.parse_args()

    if args.replay_log:
        replay_event_log(args.replay_log, speedup=args.replay_speed)
        return

    refresh_seconds = args.interval
    event_logger = EventLogger(args.event_log)
    event_logger.log(
        "startup",
        interval=refresh_seconds,
        event_log=args.event_log,
        pid=os.getpid(),
    )

    last_usage: dict | None = None
    last_success_at: datetime | None = None

    def _on_exit(*_):
        event_logger.log("exit", usage=last_usage, last_success_at=last_success_at)
        _save_state(last_usage, last_success_at)
        print()
        sys.exit(0)

    def _on_resize(*_):
        global _resize_pending
        _resize_pending = True
        event_logger.log("resize")

    signal.signal(signal.SIGINT, _on_exit)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, _on_resize)

    # Restore cached state so we don't hammer the API right after restart
    cached = _load_state(refresh_seconds)
    if cached is not None:
        last_usage, last_success_at, initial_delay = cached
        event_logger.log(
            "state_loaded",
            usage=last_usage,
            last_success_at=last_success_at,
            initial_delay=initial_delay,
            backoff_until=_backoff_until,
        )
    else:
        initial_delay = 0.0
        event_logger.log("state_missing")

    next_fetch_at   = time.time() + initial_delay
    last_headers    = None
    last_header_fetch = 0.0

    while True:
        now = time.time()
        err = None

        if now >= next_fetch_at:
            event_logger.log("fetch_window_open", now=now)
            # Refresh headers every 4 minutes (tokens live ~1h)
            if now - last_header_fetch > 240:
                new_headers, header_err = load_auth_headers()
                if header_err is None:
                    last_headers = new_headers
                    last_header_fetch = now
                    event_logger.log("auth_headers_loaded")
                else:
                    err = header_err
                    event_logger.log("auth_headers_failed", error=header_err)

            if err is None:
                event_logger.log("fetch_start")
                data, fetch_err = fetch_usage(last_headers)
                err = fetch_err
                event_logger.log("fetch_result", usage=data, error=fetch_err, backoff_until=_backoff_until)
                if data is not None:
                    last_usage = data
                    last_success_at = datetime.now(timezone.utc)
                    event_logger.log("usage_updated", usage=last_usage, last_success_at=last_success_at)

            next_fetch_at = time.time() + refresh_seconds
            event_logger.log("next_fetch_scheduled", next_fetch_at=next_fetch_at)

        # ── single status line above bars ───────────────────────────────────
        if last_success_at is not None:
            age_seconds = time.time() - last_success_at.timestamp()
            if age_seconds >= 60:
                age_str = _dim_separators(_format_relative(age_seconds), DIM)
                status = f"{DIM}synced {age_str} ago ({_format_timestamp(last_success_at)}){RESET}"
            else:
                status = ""
            if err:
                status += f"  {RED}{err}{RESET}" if status else f"{RED}{err}{RESET}"
        elif err:
            status = f"{RED}{err}{RESET}"
        else:
            status = f"{DIM}Waiting for first successful fetch…{RESET}"

        event_logger.log(
            "render",
            usage=last_usage,
            top_status=status,
            bottom_status="",
            err=err,
        )
        render(last_usage, top_status=status)

        sleep_s = min(60, max(0.1, next_fetch_at - time.time()))
        event_logger.log("sleep", seconds=sleep_s)
        _interruptible_sleep(sleep_s)

if __name__ == "__main__":
    main()
