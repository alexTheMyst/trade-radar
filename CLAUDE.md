# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A rules-based, **alert-only** investment signal system. Two agents (Discovery Agent + News Classifier) emit ranked opportunities and thesis-relevant news deltas. All trade execution is manual on Schwab. No automated execution — ever.

## Tech Stack

- **Python 3.12+**, package manager: `uv`
- **Runner:** Windows Task Scheduler on a Windows machine
- **State:** SQLite (`./state/signals.db`) via stdlib `sqlite3`, WAL mode
- **LLM:** Anthropic Claude API via `anthropic` SDK — use current Sonnet model
- **Data:** Finnhub free tier via `finnhub-python` SDK (60 calls/min limit)
- **Delivery:** Gmail SMTP via stdlib `smtplib`
- **Heartbeat:** healthchecks.io (silent failure is the main risk)

## Project Layout

```
signal-system/
├── pyproject.toml
├── .env                        # secrets — NEVER commit
├── .env.example                # committed template
├── state/signals.db            # gitignored
├── src/signal_system/
│   ├── __main__.py             # entry: `python -m signal_system <job>`
│   ├── config.py               # loads .env, exposes settings
│   ├── jobs/daily_close.py     # MVP job; news + discovery jobs come later
│   ├── data/finnhub_client.py
│   ├── state/repository.py     # all SQLite access
│   ├── delivery/email_sender.py
│   └── monitoring/heartbeat.py # healthchecks.io context manager
└── tests/
```

## Common Commands

```bash
# Install dependencies
uv sync

# Run a job manually (main entry point)
python -m signal_system daily-close
python -m signal_system news-morning

# Run tests
uv run pytest
uv run pytest tests/test_smoke.py  # single test file

# SQLite inspection
sqlite3 state/signals.db "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 10;"
sqlite3 state/signals.db "SELECT * FROM runs ORDER BY started_at DESC LIMIT 5;"
```

## Architecture: Key Design Decisions

**Heartbeat wraps every job.** The `heartbeat()` context manager (in `monitoring/heartbeat.py`) pings healthchecks.io `/start`, `/success`, and `/fail`. This is non-negotiable — silent failure is indistinguishable from "no alerts today."

**Alert Router enforces a daily budget.** All signals from both agents funnel through the router before delivery. Hard caps: 1 `ACTION_REQUIRED`, 3 `INFORMATIONAL` per day. Slot competition: if two agents compete for `ACTION_REQUIRED`, higher-scored wins, others demote. The router writes suppressed signals to SQLite tagged `MONITORING`.

**No-signal days are explicit.** The daily digest email is always sent, even with zero alerts. "Scanned N tickers, 0 alerts" text is required — silence with confirmation, not silence with ambiguity.

**News Classifier taxonomy is `thesis.yaml`-driven, not hardcoded.** The classifier reads `thesis.yaml` at runtime. Operator updates the thesis; classifier adapts without code changes. If `review_due` date in `thesis.yaml` is past, the classifier should refuse to run (or alert loudly).

**Discovery Agent rotates the universe.** ~1,500 tickers, free tier rate limit means 1/3 scanned per day (full coverage every 3 days). Core holdings always scanned daily. Scoring weights `35/30/25/10` are initial guesses — Phase A logs only (no alerts) for two weeks to build intuition before connecting to the router.

**SQLite WAL mode** handles any concurrent job writes. All DB access goes through `state/repository.py` — no raw SQL outside that module.

## Secrets

Required environment variables (see `.env.example`):
- `FINNHUB_API_KEY`
- `HEALTHCHECKS_UUID`
- `GMAIL_USERNAME` / `GMAIL_APP_PASSWORD`
- `ALERT_RECIPIENT_EMAIL`
- `ANTHROPIC_API_KEY`

On Windows, secrets can also live in Windows Credential Manager; `.env` is the fallback.

## Known Risks to Keep in Mind

- **Finnhub free tier gaps:** `^GSPC`, `^VIX`, insider sentiment, and `/scan/technical-indicator` may be paid-only. Validate before writing code that depends on them. If they're unavailable, adjust the scoring formula — don't silently skip.
- **Timezone:** Always use `zoneinfo.ZoneInfo("America/New_York")` for market-hour comparisons. On Windows, add `tzdata` package if you hit `ZoneInfoNotFoundError`.
- **Prompt injection:** Headlines from Finnhub get embedded in Claude prompts. Strip control characters, cap at ~500 chars per headline, use `<headline>...</headline>` delimiters.
- **Wash sale tracking:** The `wash_sale` table must include an `account` column from day one — wash sale rules apply across 4 accounts (main Schwab, secondary Schwab, Roth IRA, HSA). Retrofitting this is painful.
- **K-1 exclusion:** Filter USO, UNG, DBC, GSG, and commodity futures ETFs from the discovery universe at the universe-builder level, not at alert time.

## What This System Explicitly Does NOT Do

- No automated trade execution
- No self-learning / adaptive scoring (operator adapts manually at quarterly review)
- No Earnings Setup or Portfolio Drift agents (covered by Schwab natively)
- No regime classifier (subsumed into news classifier pillar deltas)
- No GitHub Actions runner (Windows Task Scheduler only)

## Signal Log & Measurement

Every signal is logged to `signals` table with `alert_id`, `timestamp`, `agent`, `severity`, `ticker`, `score`, and outcome fields. Operator fills `acted`/`acted_at`/`user_note` within 7 days. System backfills `outcome_price_30d` and `outcome_price_90d` via cron. Measurement is per signal type (Discovery, News Classifier, Mechanical) — never aggregate. See `signal-log-schema.md` for full schema and quarterly review process.
