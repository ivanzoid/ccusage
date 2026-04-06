# ccusage

Minimal terminal monitor for Claude Code rate-limit usage.

## What it does

Draws two live-updating progress bars:
- **5h** — 5-hour rate-limit utilization
- **7d** — 7-day rate-limit utilization

Each bar shows:
- Percentage left of the bar
- Fill-style bar (`█` filled, `░` empty) colored by utilization:
  - Cyan   0–40 %   (idle)
  - Green  40–65 %  (healthy)
  - Yellow 65–80 %  (moderate)
  - Orange 80–92 %  (high)
  - Red    92 %+    (critical)
- Time to reset: relative (`in 2h 30m`) and absolute (`14:30` or `Mon 14:30`)

Bars resize to full terminal width. Refreshes every 30 seconds.

## Structure

```
ccusage/
  ccusage.py   # everything: auth, API, rendering
  CLAUDE.md
```

No package subdirectory — the entire tool is a single script.

## Dependencies

- `requests` — HTTP calls to the Claude API

No other external libraries. Drawing uses raw ANSI escape codes and Unicode block characters.

## Authentication

Reads `~/.claude/.credentials.json` (written by `claude` CLI after login).  
On macOS falls back to the system Keychain if the file is missing or the token is expired.

## Running

```bash
python ccusage.py
# or
chmod +x ccusage.py && ./ccusage.py
```

## API endpoint

`GET https://api.anthropic.com/api/oauth/usage`

Response fields used:
- `five_hour.utilization` — percentage (0–100+)
- `five_hour.resets_at`   — ISO-8601 timestamp
- `seven_day.utilization`
- `seven_day.resets_at`

## Removed from original

- Console mode (org admin API key)
- Pace Maker integration
- Governance event feed
- Blockage statistics
- Profile / account badges
- Model-specific (Sonnet/Opus) sub-limits
- SQLite usage history
- Rich library dependency
- All other files (tests, docs, tools, pyproject.toml)
