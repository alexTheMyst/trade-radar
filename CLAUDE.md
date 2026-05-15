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

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Signal System — Rules-Based Investment Alert Engine**

A rules-based, **alert-only** investment signal system for a solo operator with positions across 4 Schwab accounts. Two AI agents (Discovery Agent + News Classifier) emit ranked opportunities and thesis-relevant news deltas; an Alert Router enforces daily delivery budgets. All trade execution is manual — the system's job is to surface the right alerts at the right time, not to act.

**Core Value:** Never miss a material thesis-relevant event on a held position — silent failure is indistinguishable from "no alerts today."

### Constraints

- **Tech Stack**: Python 3.12+, `uv`, SQLite stdlib, Anthropic SDK (current Sonnet) — no ORMs, no frameworks
- **Data**: Finnhub free tier only — 60 calls/min hard limit, validate endpoint availability before coding against it
- **Execution**: Windows Task Scheduler — no Docker, no GitHub Actions, no cron (Linux)
- **DB access**: All SQLite access through `state/repository.py` — no raw SQL outside that module
- **Secrets**: `.env` file only — never commit; Windows Credential Manager as optional override
- **Prompt safety**: Headlines from Finnhub embedded in Claude prompts — strip control characters, cap at ~500 chars/headline, use `<headline>...</headline>` delimiters
- **Wash sale compliance**: `wash_sale` table must have `account` column from day one — retrofitting across 4 accounts is painful
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Existing Stack (Locked — Do Not Change)
| Component | Choice | Status |
|-----------|--------|--------|
| Language | Python 3.12+ | Locked |
| Package manager | uv | Locked |
| State | SQLite (stdlib sqlite3, WAL mode) | Locked |
| LLM | Anthropic Claude API (pinned Sonnet) via `anthropic` SDK | Locked |
| Market data | Finnhub free-tier via `finnhub-python` | Locked |
| Delivery | Gmail SMTP via stdlib `smtplib` | Locked |
| Heartbeat | healthchecks.io | Locked |
| Runner | Windows Task Scheduler | Locked |
| DB access layer | `state/repository.py` — no raw SQL outside | Locked |
## What's Needed for the Next Milestone
### Structured Output — `messages.parse()` (not `instructor`)
- Returns a typed `ParsedMessage` with `.parsed_output` (Pydantic instance)
- Auto-generates JSON schema from the Pydantic model — no manual boilerplate
- This is SDK-native as of the current `anthropic` SDK version; the older tool-use-with-forced-tool pattern is no longer necessary
- **Do NOT use `instructor`** — it wraps the same SDK calls with an extra dependency and is now redundant
### Rate Limiting — stdlib token bucket (not a library)
# ~10 lines — correct for sequential one-shot jobs
- At 500 tickers/day × 1-2 calls/ticker, the Discovery job takes 9–18 minutes wall time — acceptable
- **Do NOT use `pyrate-limiter`** — it solves concurrent-access problems that don't exist in sequential jobs
- **Do NOT use `asyncio`** — Windows event-loop policy quirks; sequential is correct
### Reactive 429 Handling — `tenacity`
### Prompt Caching — thesis.yaml as cached system prompt
- Log `cache_read_input_tokens` and `cache_creation_input_tokens` from `response.usage` to the `llm_calls` table per run
- Token telemetry is free — `ParsedMessage.usage` exposes all four token counts
- Minimum token threshold for cache activation: validate against current Anthropic pricing docs at implementation time (training data says ~1,024 tokens; a real thesis.yaml will likely exceed it)
### Alert Router — Custom domain logic (not a library)
## Dependency Delta
## What NOT to Use
| Rejected | Reason |
|----------|--------|
| `instructor` | SDK now has `messages.parse()` natively; extra dep, no benefit |
| `LangChain`, `CrewAI`, `AutoGen`, `LlamaIndex` | Frameworks own agent lifecycle; conflicts with heartbeat/dispatcher design |
| `asyncio` / `aiohttp` | Windows event-loop policy quirks; sequential jobs don't need concurrency |
| `Celery`, `RQ`, `APScheduler` | Task Scheduler is the orchestrator; no daemons |
| `SQLAlchemy`, `peewee` | Explicitly excluded by CLAUDE.md; `repository.py` is the access layer |
| `pyrate-limiter`, `ratelimit` | Solve concurrent-access problems absent in sequential jobs |
| `Redis` | No network services; SQLite is the shared state |
| `httpx` | `finnhub-python` wraps `requests`; mixing HTTP clients creates confusion |
## New SQLite Tables Needed
| Table | Purpose |
|-------|---------|
| `candidate_signals` | Router staging; cleared after each routing pass |
| `daily_budget` | Tracks slots used per day per severity; query target for router |
| `wash_sale` | Wash sale tracking with `account` column (4 accounts — day one) |
| `llm_calls` | Token telemetry: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` |
| Column | Purpose |
|--------|---------|
| `routing_status` | DELIVERED / MONITORING / SUPPRESSED — router sets this, never severity |
| `model_version` | Pinned model ID string for IC comparability |
| `thesis_version_hash` | SHA256 of thesis.yaml at classification time |
| `signal_price_snapshot` | Price at signal generation (unadjusted) for outcome measurement |
| `weight_version` | Stamp on Discovery signals for IC interpretability after weight changes |
## Open Questions
- Exact minimum token threshold for Anthropic prompt caching (verify against current docs at implementation)
- Which Finnhub free-tier endpoints are available for the 35/30/25/10 Discovery scoring weights — validate empirically before writing scoring code
- Whether `messages.parse()` supports `temperature=0.0` alongside `output_format` (likely yes — verify at implementation; classification calls must be deterministic)
- Does `/stock/candle` support `adjusted=False` on Finnhub free tier? (needed for outcome backfill)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
