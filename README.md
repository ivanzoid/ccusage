# ccusage

Minimal Claude Code rate-limit monitor for the terminal.

Draws two live-updating bars — 5-hour and 7-day usage — that fill your terminal width and warm in color as consumption grows.

```
5h   3% ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  in 4h51m (14:30)
7d  18% ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  in 5d12h (Thu 14:30)
```

## Color scheme

Color is based on **burn rate** — how fast you're consuming the limit relative to the time elapsed in the window. This catches "you're already at 50% with 4 days left in the week" situations that raw utilization misses.

| Burn ratio (actual / expected) | Color  |
|-------------------------------|--------|
| < 0.5×                        | Cyan   |
| 0.5 – 1.0×                    | Green  |
| 1.0 – 1.5×                    | Yellow |
| 1.5 – 2.0×                    | Orange |
| 2.0×+                         | Red    |

Falls back to raw utilization thresholds during the first 10% of a window (too early to judge pace).

## Requirements

- Python 3.10+
- [`requests`](https://pypi.org/project/requests/)
- Claude Code installed and authenticated (`claude` CLI)

## Install

```bash
pipx install git+https://github.com/ivanzoid/ccusage.git
```

Or without pipx:

```bash
pip install git+https://github.com/ivanzoid/ccusage.git
```

## Run

```bash
ccusage
```

Press `Ctrl+C` to exit.

## How it works

Reads OAuth credentials from `~/.claude/.credentials.json` (written by the `claude` CLI).  
On macOS, falls back to the system Keychain if the file is missing or the token is expired.

Polls `GET https://api.anthropic.com/api/oauth/usage` every 30 seconds and redraws the two lines in-place. No databases, no config files, no background processes.

When rate-limited, shows a live countdown (`Rate-limited — retry in 4m`) that updates every second using exponential backoff.

## Origin

Derived from [LightspeedDMS/claude-usage](https://github.com/LightspeedDMS/claude-usage) — stripped down to the two usage bars and rewritten to use only ANSI escape codes (no `rich` dependency).
