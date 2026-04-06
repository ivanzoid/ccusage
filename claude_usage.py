#!/usr/bin/env python3
"""Claude Code usage monitor — shows 5h and 7d rate limit bars."""

import json
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Configuration ────────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
API_BASE = "https://api.anthropic.com"
USAGE_ENDPOINT = "/api/oauth/usage"
REFRESH_SECONDS = 30

# ── ANSI helpers ─────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"

def _color(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"

GREEN  = _color(80, 200, 80)
YELLOW = _color(220, 200, 40)
ORANGE = _color(255, 140, 0)
RED    = _color(220, 50, 50)
DIM    = "\033[2m"

def usage_color(pct: float) -> str:
    if pct >= 95:
        return RED
    if pct >= 81:
        return ORANGE
    if pct >= 50:
        return YELLOW
    return GREEN

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

def fetch_usage(headers: dict) -> tuple:
    """Return (usage_dict, error_str)."""
    global _consecutive_429s, _backoff_until

    if time.time() < _backoff_until:
        remaining = int(_backoff_until - time.time())
        return None, f"Rate-limited — retry in {remaining}s"

    url = f"{API_BASE}{USAGE_ENDPOINT}"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                _consecutive_429s = 0
                _backoff_until = 0.0
                return resp.json(), None
            elif resp.status_code == 429:
                if attempt < 2:
                    time.sleep(4 * (2 ** attempt) + random.uniform(0, 2))
                    continue
                duration = min(300 * (2 ** _consecutive_429s), 3600)
                _backoff_until = time.time() + duration
                _consecutive_429s += 1
                return None, f"Rate-limited (429) — retry in {duration}s"
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
    s = int(seconds)
    if s <= 0:
        return "now"
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes = s // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"

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

# ── Bar drawing ───────────────────────────────────────────────────────────────

FILLED = "█"
EMPTY  = "░"

def _draw_bar(label: str, pct: float, reset_dt: datetime | None, bar_width: int) -> str:
    """Return a single colored bar line (no trailing newline)."""
    pct = max(0.0, min(pct, 100.0))
    filled = round(bar_width * pct / 100)
    empty  = bar_width - filled

    color = usage_color(pct)
    bar = color + FILLED * filled + DIM + EMPTY * empty + RESET

    pct_str = f"{pct:5.1f}%"

    if reset_dt:
        secs = (reset_dt - datetime.now(timezone.utc)).total_seconds()
        rel  = _format_relative(secs)
        abso = _format_absolute(reset_dt)
        reset_str = f"  in {rel} ({abso})"
    else:
        reset_str = ""

    return f"{BOLD}{label}{RESET} {bar}  {color}{BOLD}{pct_str}{RESET}{reset_str}"

# ── Render loop ───────────────────────────────────────────────────────────────

_first_draw = True

def _clear_lines(n: int):
    """Move cursor up n lines and clear each."""
    for _ in range(n):
        sys.stdout.write("\033[1A\033[2K")

def render(usage: dict | None, error: str | None):
    global _first_draw

    term_w = shutil.get_terminal_size((80, 24)).columns

    # Fixed overhead: label(2) + space(1) + 2 spaces before pct + pct(7) + reset(~22 max)
    # Keep it generous: reserve 35 chars for right side, 3 for label+space
    overhead = 3 + 2 + 7 + 22  # label + bar-gap + pct + reset
    bar_w = max(10, term_w - overhead)

    lines = []

    if error:
        msg = f"\033[31m{error}{RESET}"
        lines.append(msg)
        lines.append("")  # placeholder for 7d
    elif usage:
        fh = usage.get("five_hour", {})
        sd = usage.get("seven_day", {})

        fh_pct = float(fh.get("utilization", 0))
        sd_pct = float(sd.get("utilization", 0))
        fh_reset = _parse_reset(fh.get("resets_at", ""))
        sd_reset = _parse_reset(sd.get("resets_at", ""))

        lines.append(_draw_bar("5h", fh_pct, fh_reset, bar_w))
        lines.append(_draw_bar("7d", sd_pct, sd_reset, bar_w))
    else:
        lines.append("Fetching…")
        lines.append("")

    if not _first_draw:
        _clear_lines(2)
    else:
        _first_draw = False

    for line in lines:
        print(line)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT, lambda *_: (print(), sys.exit(0)))

    print()  # blank line so clearing doesn't scroll
    _clear_lines(1)

    last_usage = None
    last_headers = None
    last_header_fetch = 0.0

    while True:
        now = time.time()

        # Refresh headers every 4 minutes (tokens live ~1h)
        if now - last_header_fetch > 240:
            headers, err = load_auth_headers()
            if err:
                render(None, err)
                time.sleep(REFRESH_SECONDS)
                continue
            last_headers = headers
            last_header_fetch = now

        data, err = fetch_usage(last_headers)
        if data:
            last_usage = data
        render(last_usage if not err else None, err if not last_usage else None)
        time.sleep(REFRESH_SECONDS)

if __name__ == "__main__":
    main()
