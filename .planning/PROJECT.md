# Signal System — Rules-Based Investment Alert Engine

## What This Is

A rules-based, **alert-only** investment signal system for a solo operator with positions across 4 Schwab accounts. Two AI agents (Discovery Agent + News Classifier) emit ranked opportunities and thesis-relevant news deltas; an Alert Router enforces daily delivery budgets. All trade execution is manual — the system's job is to surface the right alerts at the right time, not to act.

## Core Value

Never miss a material thesis-relevant event on a held position — silent failure is indistinguishable from "no alerts today."

## Current State

**v1.0 shipped 2026-05-17** — implementation complete, 44/44 requirements satisfied, 120 tests passing, 6 phases complete with full VERIFICATION.md artifacts.

Four manual go-live evidence items pending before production use:
- OPS-01: Windows Task Scheduler import and validation on runner machine
- OPS-02: Gmail SMTP + healthchecks.io live credential verification
- JOBS-01: End-to-end credentialed `news-morning` run against live Finnhub + Anthropic APIs
- MEAS-01: 7-day acted/user_note feedback workflow confirmed

## Requirements

### Validated — v1.0

- ✓ Daily close job with Finnhub market data fetch — MVP Week 1
- ✓ SQLite persistence layer (WAL mode, `signals` + `runs` tables) via `state/repository.py` — MVP Week 1
- ✓ Heartbeat monitoring via healthchecks.io context manager (start/success/fail pings) — MVP Week 1
- ✓ Gmail SMTP email delivery via `delivery/email_sender.py` — MVP Week 1
- ✓ Environment-based config loading with required-var validation via `config.py` — MVP Week 1
- ✓ Job dispatcher entry point (`python -m signal_system <job>`) — MVP Week 1
- ✓ Smoke test suite covering DB init, signal insert, heartbeat, daily-close paths — MVP Week 1
- ✓ **News Classifier agent** — Claude API, thesis.yaml-driven taxonomy, pillar delta scoring, `<headline>` delimiters — v1.0
- ✓ **Discovery Agent** — ~1,500 ticker universe, 1/3 daily rotation, core holdings always scanned, 35/30/25/10 scoring weights, Phase A logs-only — v1.0
- ✓ **Alert Router** — daily budget enforcement (1 ACTION_REQUIRED, 3 INFORMATIONAL), slot competition (higher score wins), suppressed signals tagged MONITORING in SQLite — v1.0
- ✓ **news-morning job** — wraps News Classifier, heartbeat-monitored, sends daily digest even on zero-alert days — v1.0
- ✓ **discovery job** — wraps Discovery Agent, heartbeat-monitored, logs to SQLite — v1.0
- ✓ **Ticker universe builder** — static universe list (~1,500 tickers), K-1 ETF exclusions (USO, UNG, DBC, GSG), core-holdings flag for daily scan priority — v1.0
- ✓ **thesis.yaml** — operator-maintained taxonomy defining news pillars, review_due gate, classifier reads at runtime — v1.0
- ✓ **Wash sale tracking table** — `wash_sale` table with `account` column (4 accounts) — v1.0
- ✓ **Signal outcome backfill** — `outcome_backfill` job coded, activate ~30 days post go-live — v1.0
- ✓ **No-signal-day digest** — explicit "Scanned N tickers, 0 alerts" email — v1.0
- ✓ **Windows Task Scheduler integration docs** — setup guide with `.xml` task files — v1.0

### Out of Scope

- Automated trade execution — system is alert-only by design; automated execution would change risk profile entirely
- Self-learning / adaptive scoring — operator adapts weights manually at quarterly review
- Earnings Setup agent — covered natively by Schwab
- Portfolio Drift agent — covered natively by Schwab
- Regime classifier — subsumed into news classifier pillar deltas
- GitHub Actions runner — Windows Task Scheduler only (runner is a Windows machine)
- `^GSPC`, `^VIX`, insider sentiment, `/scan/technical-indicator` — may be Finnhub paid-only; validate before adding, adjust scoring formula if unavailable

## Context

**Existing codebase (MVP Week 1 complete — 15 commits):**
```
src/signal_system/
├── __main__.py             # job dispatcher
├── config.py               # env validation
├── jobs/daily_close.py     # MVP job
├── data/finnhub_client.py  # Finnhub free-tier client
├── state/repository.py     # all SQLite access (DB_PATH from config, not relative)
├── delivery/email_sender.py
└── monitoring/heartbeat.py # healthchecks.io context manager
```

**Runner environment:** Windows Task Scheduler on a Windows machine. Secrets in `.env` (fallback from Windows Credential Manager). Always use `zoneinfo.ZoneInfo("America/New_York")` for market-hour logic; add `tzdata` on Windows.

**Finnhub free-tier constraints:** 60 calls/min. Rate limiting must gate the Discovery Agent's universe scan. Paid-only endpoints must be validated before use.

**Signal measurement:** Every signal logged with `alert_id`, `timestamp`, `agent`, `severity`, `ticker`, `score`. Operator fills `acted`/`acted_at`/`user_note` within 7 days. Measured per signal type — never aggregate.

## Constraints

- **Tech Stack**: Python 3.12+, `uv`, SQLite stdlib, Anthropic SDK (current Sonnet) — no ORMs, no frameworks
- **Data**: Finnhub free tier only — 60 calls/min hard limit, validate endpoint availability before coding against it
- **Execution**: Windows Task Scheduler — no Docker, no GitHub Actions, no cron (Linux)
- **DB access**: All SQLite access through `state/repository.py` — no raw SQL outside that module
- **Secrets**: `.env` file only — never commit; Windows Credential Manager as optional override
- **Prompt safety**: Headlines from Finnhub embedded in Claude prompts — strip control characters, cap at ~500 chars/headline, use `<headline>...</headline>` delimiters
- **Wash sale compliance**: `wash_sale` table must have `account` column from day one — retrofitting across 4 accounts is painful

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Alert-only, no execution | Risk management — operator retains full discretion on all trades | — Pending |
| Heartbeat wraps every job | Silent failure is the primary risk; healthchecks.io /fail ping is the safety net | ✓ Good |
| SQLite over Postgres | Single-machine runner, no concurrent external clients, stdlib only | ✓ Good |
| thesis.yaml drives classifier taxonomy | Operator can update thesis without code changes; `review_due` gate forces periodic refresh | — Pending |
| Discovery Agent Phase A = logs only | 2-week calibration period before live alerts prevents noisy signal flood | — Pending |
| Heartbeat success must be inside context manager boundary | DB failure must trip /fail ping — success call moved inside `with heartbeat()` block | ✓ Good |
| DB_PATH from config (absolute) | Relative paths break when Task Scheduler runs from system CWD | ✓ Good |
| 1/3 universe rotation per day | Finnhub rate limit makes full daily scan infeasible; core holdings always in daily subset | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-17 after v1.0 milestone close*
