# ccusage

Minimal Claude Code rate-limit monitor for the terminal.

Draws two live-updating bars — 5-hour and 7-day usage — that fill your terminal width and warm in color as consumption grows.

```
5h   3.2% ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  in 4h 51m (14:30)
7d  18.7% ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  in 5d 12h (Thu 14:30)
```

## Color scheme

| Utilization | Color  |
|-------------|--------|
| 0 – 40 %    | Cyan   |
| 40 – 65 %   | Green  |
| 65 – 80 %   | Yellow |
| 80 – 92 %   | Orange |
| 92 %+       | Red    |

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

## Origin

Derived from [LightspeedDMS/claude-usage](https://github.com/LightspeedDMS/claude-usage) — stripped down to the two usage bars and rewritten to use only ANSI escape codes (no `rich` dependency).
