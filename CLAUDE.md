# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A rules-based, **alert-only** investment signal system. Two agents (Discovery Agent + News Classifier) emit ranked opportunities and thesis-relevant news deltas. All trade execution is manual on Schwab. No automated execution — ever.

## Tech Stack

- **Python 3.12+**, package manager: `uv`
- **Runner:** Windows Task Scheduler on a Windows machine
- **State:** SQLite (`./state/signals.db`) via stdlib `sqlite3`, WAL mode
- **LLM:** Anthropic Claude API via `anthropic` SDK — use `ANTHROPIC_MODEL` from `.env`
- **Data:** Finnhub free tier via `finnhub-python` SDK (60 calls/min); yfinance for candle history
- **Delivery:** Telegram via custom sender (`delivery/telegram_sender.py`)
- **Heartbeat:** healthchecks.io (silent failure is the main risk)

## Project Layout

```
src/signal_system/
├── __main__.py             # entry: `python -m signal_system <job>`
├── config.py               # loads .env, exposes settings (raises on missing secrets)
├── models.py               # Signal dataclass, Severity type, competition_score()
├── preflight.py            # read-only diagnostic (never scheduled)
├── reconciler.py           # direction reconciliation for news signals
├── redaction.py            # strips API keys from logs / tenacity retry messages
├── jobs/                   # one module per scheduled/operator job
│   ├── common.py           # persist_routed_signals, render_digest, validate_digest_payload
│   ├── daily_close.py      # SPY benchmark close
│   ├── news_morning.py     # News Classifier job
│   ├── discovery.py        # Discovery Agent job
│   ├── sentinel.py         # Exit Sentinel job
│   ├── dispatch.py         # evening cross-agent INFO arbitration
│   ├── telegram_poll.py    # Acted/Passed button taps + reply notes
│   ├── outcome_backfill.py # fills 30d/90d outcome prices
│   ├── review_report.py    # per-agent quality readout
│   ├── weekly_synthesis.py # MONITORING corpus → pillar candidates
│   ├── watchdog.py         # flags stale jobs, old CANDIDATE rows
│   └── backup.py           # WAL-safe DB snapshot
├── classifier/news_classifier.py   # Anthropic messages.parse() + Pydantic
├── discovery/discovery_agent.py    # momentum scoring + signal emission
├── sentinel/exit_sentinel.py       # drawdown / day_drop / below_cost triggers
├── router/alert_router.py          # stage_signals(), dispatch_candidates(), route_signals()
├── state/repository.py             # ALL SQLite access — no raw SQL elsewhere
├── data/                           # finnhub_client, yahoo_client, universe, holdings, thesis_loader
├── scoring/weight_amplifier.py     # position-weight severity adjustment
├── replay/weight_replay.py         # offline scoring-weight analysis
├── review/                         # report, stats
├── synthesis/weekly_synthesis.py
├── evals/classifier_eval.py
└── monitoring/heartbeat.py         # healthchecks.io context manager
```

## Common Commands

```bash
# Install dependencies
uv sync

# Machine health check (read-only, never scheduled)
python -m signal_system preflight

# Scheduled jobs — run manually for testing
python -m signal_system news-morning     # 09:00 ET weekdays
python -m signal_system daily-close      # 16:15 ET weekdays (SPY benchmark)
python -m signal_system discovery        # 16:15 ET weekdays
python -m signal_system sentinel         # 17:00 ET weekdays
python -m signal_system outcome-backfill # 17:30 ET weekdays
python -m signal_system dispatch         # 17:40 ET weekdays — evening INFO arbitration
python -m signal_system watchdog         # daily — flags stale jobs, old CANDIDATE rows
python -m signal_system telegram-poll    # every 15 min, 07:00-23:00 ET (buttons/notes + stuck-PENDING re-send sweep)
python -m signal_system weekly-synthesis # Mondays 08:00 ET
python -m signal_system review-report    # 1st Saturday monthly
python -m signal_system backup           # Sundays 08:00 ET

# Operator commands (never scheduled)
python -m signal_system list-signals [--days N]
python -m signal_system log-action <prefix> --acted|--passed [--note "..."]
python -m signal_system log-action --bulk-close-before YYYY-MM-DD --note "..."
python -m signal_system build-universe [--apply]   # dry-run by default
python -m signal_system backfill-benchmark         # one-time SPY history bootstrap
python -m signal_system replay-weights [--days 180] [--weights 50/30/20] [--horizons 5,20] [--gates on|off]
python -m signal_system eval-classifier            # golden-set readout before any model change

# Run tests
uv run pytest
uv run pytest tests/test_smoke.py -v   # single test file
uv run pytest -k "test_alert_router"   # filter by name

# SQLite inspection
sqlite3 state/signals.db "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 10;"
sqlite3 state/signals.db "SELECT agent, severity, routing_status, count(*) FROM signals WHERE date(timestamp) = date('now') GROUP BY 1,2,3;"
sqlite3 state/signals.db "SELECT * FROM runs ORDER BY started_at DESC LIMIT 5;"
```

## Architecture: Key Design Decisions

**Heartbeat wraps every job, with a per-job check.** The `heartbeat(job=...)`
context manager (in `monitoring/heartbeat.py`) pings healthchecks.io
`/start`, `/success`, and `/fail` on the job's OWN check
(`HEALTHCHECKS_UUID_<JOB>`, falling back to the shared `HEALTHCHECKS_UUID`).
Per-job checks prevent one job's success from masking another's death. This is
non-negotiable — silent failure is indistinguishable from "no alerts today."

**Alert Router enforces per-agent AR lanes and a pooled INFO budget.** All signals from the three agents (Discovery, News Classifier, Exit Sentinel) funnel through the router. Since Plan 12 (`routing_regime='deferred-info-priority-v2'`): `stage_signals()` delivers each agent's top ACTION_REQUIRED against **its own lane** — news 1, sentinel 1, Discovery 0 — so a spent news lane can no longer starve the sentinel. Losing ARs are demoted to INFO with `demoted_from='ACTION_REQUIRED'`; tickerless macro news is demoted with `demoted_from='macro_cap'` and can never be AR. Everything else is staged as `routing_status='CANDIDATE'`. The `dispatch` job (weekdays 5:40 PM ET) pools candidates and calls `dispatch_candidates()`, which (1) reserves one INFO slot for a fresh sentinel candidate (a `trigger_json` containing `drawdown_20d` or `day_drop_1d`; below-cost-only breaches compete normally), ranked by the unclamped severity ratio, then (2) ranks the rest by agent priority (news > sentinel > discovery), with news ordered held → non-held → macro, then `competition_score`. Prior-day candidates expire to `SUPPRESSED` (`suppression_reason='expired'`); the watchdog flags CANDIDATE rows older than 36h and action-button send failures. `route_signals()` (single-batch, severity-first, one AR) is unchanged and still used by tests and replays. Rows with `routing_regime` NULL are legacy-only — never aggregate across regimes.

**MONITORING corpus is the new-idea raw material.** Every off-thesis classification is persisted as a MONITORING signal with a title prefix — the paid-for LLM rationale is never discarded. The `monitoring_reason` column is the source of truth for counting and the corpus; title prefixes are for human readability only. Reason taxonomy: `off_thesis` (no pillar matched), `volume_cap` (over the per-ticker round-robin/global cap, never classified), `parse_failure` (schema parse failed), `below_threshold` (pillar matched, confidence under the routing floor), `reconciled` (lost direction reconciliation, `demoted_from='reconciled'`), `cooldown` (sentinel repeat inside the 5-day window, `[cooldown]` prefix), and `repeat` (cross-day duplicate news story, `[repeat]` prefix, never routed). The **weekly-synthesis** job (Mondays 8:00 AM ET) reads this corpus by `monitoring_reason` to propose new pillar candidates, capped at 200 items, every title re-sanitized before prompt embedding. The digest's Monitoring line is validated against a DB readback (`count_signals_by_routing_status`/`count_monitoring_by_reason`) after persistence — a digest whose counts diverge from the database fails the run instead of sending.

**Exit Sentinel is the Mechanical third signal type.** `jobs/sentinel.py` (weekdays 5:00 PM ET) evaluates held positions from `holdings.csv` against three mechanical triggers — drawdown from the 20-day high, one-day drop, below-cost — and routes one signal per ticker. Severity is rule-determined (AR iff drawdown ≥15% or day-drop ≥8%; the weight amplifier is deliberately unused). `build_signal()` stamps `trigger_json` with the breached magnitudes, which drives the dispatch reserved slot. A 5-trading-day cooldown demotes repeat triggers to `[cooldown]` MONITORING unless the new trigger escalates to AR — and only a prior DELIVERED row starts a cooldown, so an undelivered alert (including one stuck PENDING after a failed send) can never silence the next one. Stuck PENDING rows are re-sent by the `telegram-poll` sweep (see the PENDING → DELIVERED convention). The watchdog flags any row PENDING for more than 3h. **Measurement semantics: a sentinel hit means the price FELL after the alert** — the review report sign-flips excess for `direction="negative"`, exactly like a News Classifier negative call.

**Weekly backup is WAL-safe.** `backup` (Sundays 8:00 AM ET) snapshots `state/signals.db` via `VACUUM INTO` to `state/backups/signals-YYYYMMDD.db` (keeps newest 8) and fails loudly if the snapshot is empty while the live DB has rows. Restore: stop tasks → copy backup over `state/signals.db` → delete stale `-wal`/`-shm` → re-enable.

**No-signal days are explicit.** The daily digest email is always sent, even with zero alerts. "Scanned N tickers, 0 alerts" text is required — silence with confirmation, not silence with ambiguity.

**News Classifier taxonomy is `thesis.yaml`-driven, not hardcoded.** The classifier reads `thesis.yaml` at runtime. Operator updates the thesis; classifier adapts without code changes. If `review_due` date in `thesis.yaml` is past, the classifier should refuse to run (or alert loudly).

**Discovery Agent rotates the universe — and runs in shadow mode.** Core holdings always scanned daily; non-core tickers partition into thirds, scanned on rotation. Scoring weights `50/30/20` (momentum_20d / momentum_5d / range_vs_20d) are initial guesses. `DISCOVERY_MODE=shadow` (default) persists every Discovery signal as `SUPPRESSED`/`suppression_reason='shadow'` for measurement, but never lets it compete or deliver; `compete` lets it compete for INFORMATIONAL slots at last priority. Discovery's ACTION_REQUIRED lane stays 0 in both modes. Re-enabling INFO delivery is a quarterly operator decision.

**SQLite WAL mode** handles any concurrent job writes. All DB access goes through `state/repository.py` — no raw SQL outside that module.

## Secrets

Required environment variables (see `.env.example`):
- `FINNHUB_API_KEY`
- `HEALTHCHECKS_UUID`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY`

On Windows, secrets can also live in Windows Credential Manager; `.env` is the fallback.

## Known Risks to Keep in Mind

- **Finnhub free tier gaps:** `^GSPC`, `^VIX`, insider sentiment, and `/scan/technical-indicator` may be paid-only. Validate before writing code that depends on them. If they're unavailable, adjust the scoring formula — don't silently skip.
- **Timezone:** Always use `zoneinfo.ZoneInfo("America/New_York")` for market-hour comparisons. On Windows, add `tzdata` package if you hit `ZoneInfoNotFoundError`.
- **Prompt injection:** Headlines from Finnhub get embedded in Claude prompts. Strip control characters, cap at ~500 chars per headline, use `<headline>...</headline>` delimiters.
- **Wash-sale tracking is delegated to Schwab** (system of record per account). The in-system `wash_sale` ledger and the Advisor were retired in Plan 12e; the empty table remains only for schema stability. Cross-account (IRA) cases remain a manual check.
- **K-1 exclusion:** Filter USO, UNG, DBC, GSG, and commodity futures ETFs from the discovery universe at the universe-builder level, not at alert time. A hardcoded `K1_DENYLIST` in `universe.py` backstops this regardless of the CSV `k1_etf` flag.

## What This System Explicitly Does NOT Do

- No automated trade execution
- No self-learning / adaptive scoring (operator adapts manually at quarterly review)
- No Earnings Setup or Portfolio Drift agents (covered by Schwab natively)
- No regime classifier (subsumed into news classifier pillar deltas)
- No GitHub Actions runner (Windows Task Scheduler only)

## Signal Log & Measurement

Every signal is logged to `signals` table with `alert_id`, `timestamp`, `agent`, `severity`, `ticker`, `score`, and outcome fields. Each delivered alert gets a Telegram message with **Acted / Passed** buttons; taps are ingested by `telegram-poll` (≤15 min) and fill `acted`/`acted_at`; a reply to that message fills `user_note` with a `tg: ` prefix. Fridays' dispatch re-prompts up to 10 unreviewed signals from the past week. System backfills `outcome_price_30d` and `outcome_price_90d` on weekday afternoons. Measurement is per signal type (Discovery, News Classifier, Mechanical) — never aggregate. See `signal-log-schema.md` for full schema and quarterly review process.

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
- **Wash-sale compliance**: delegated to Schwab; the in-system ledger is retired (empty table retained for schema stability)

## Technology Stack

## Locked — Do Not Change
| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Package manager | uv |
| State | SQLite (stdlib sqlite3, WAL mode) |
| LLM | Anthropic Claude API via `anthropic` SDK — `messages.parse()` + Pydantic structured output |
| Market data | Finnhub free-tier via `finnhub-python`; yfinance for candle/price history |
| Delivery | Telegram via custom sender (`delivery/telegram_sender.py`) |
| Heartbeat | healthchecks.io |
| Runner | Windows Task Scheduler |
| DB access layer | `state/repository.py` — no raw SQL outside |

## What NOT to Use
| Rejected | Reason |
|----------|--------|
| `instructor` | SDK `messages.parse()` is native; extra dep, no benefit |
| `LangChain`, `CrewAI`, `AutoGen`, `LlamaIndex` | Frameworks own agent lifecycle; conflicts with heartbeat/dispatcher design |
| `asyncio` / `aiohttp` | Windows event-loop policy quirks; sequential jobs don't need concurrency |
| `Celery`, `RQ`, `APScheduler` | Task Scheduler is the orchestrator; no daemons |
| `SQLAlchemy`, `peewee` | `repository.py` is the access layer |
| `pyrate-limiter`, `ratelimit` | Solve concurrent-access problems absent in sequential jobs |
| `Redis` | No network services; SQLite is the shared state |
| `httpx` | Only used in `monitoring/heartbeat.py`; do not introduce as a general HTTP client |

## SQLite Tables
| Table | Purpose |
|-------|---------|
| `signals` | Every alert ever emitted with full routing outcome |
| `runs` | Every job invocation (start/end/outcome) |
| `benchmark_prices` | SPY daily closes for excess-return measurement |
| `llm_calls` | Per-call token telemetry (4 token fields including cache hits) |
| `ticker_meta` | Sector/industry cache |
| `synthesis_reports` | Weekly-synthesis run records |
| `telegram_messages` | Button/review messages + send status per alert |
| `app_state` | Small key/value store (Telegram getUpdates offset) |
| `wash_sale` | DEPRECATED — retired Plan 05; empty, retained for schema stability |

## Conventions

**PENDING → DELIVERED two-step delivery.** Jobs persist to-be-delivered signals as `routing_status='PENDING'`, then flip to `'DELIVERED'` after a successful Telegram send (`confirm_delivered_signals()`). A failed send never leaves phantom DELIVERED rows. Sends retry transient failures (429, 5xx, network) up to 3 attempts before failing the job. No job's rerun re-sends its own PENDING rows; `jobs/common.resend_unconfirmed()`, run by `telegram-poll` every 15 min, is the **single owner** of that. Rows under 3 days old (`RESEND_WINDOW`) are re-sent as button messages labelled with when they were raised, skipping any row whose producing job has a run in flight (`running` and under 2h old). Older rows are announced in one notice and then closed out as `SUPPRESSED`/`suppression_reason='send_failed'`. A failed re-send or notice fails the poll run, so the heartbeat pages through healthchecks.io, which does not depend on Telegram. Never add a second re-sender: two of them could double-send the same row.

**validate_digest_payload() before every send.** `jobs/common.py:validate_digest_payload()` reads counts back from the DB after persistence and fails the job if digest text diverges. Always call it — a digest that undercounts is a lie; a job that refuses to send is a page.

**compute_score_deltas() before persist_routed_signals().** The delta function reads the prior scan; persisting first would let it see the current run's own rows.

**MONITORING signals bypass the router entirely.** Pass them directly to `repository.insert_signal()` with `routing_status='MONITORING'`. Never call `stage_signals()` or `route_signals()` on a MONITORING signal — both raise `ValueError`.

**Jobs import config lazily.** `__main__.py` defers all job imports into `_load_jobs()` so that `preflight` can diagnose a broken `.env` without an import traceback. New jobs must be added to both `JOB_NAMES` (import-safe list) and `_load_jobs()`.

**`routing_regime` must be stamped on every insert.** `ROUTING_REGIME` is defined in `models.py` (stdlib-only) and re-exported by `router/__init__.py`; the current value is `deferred-info-priority-v2`. `repository.insert_signal()` defaults to it, so MONITORING and shadow paths are stamped without extra plumbing; pass `routing_regime=None` only for a deliberate legacy replay. NULL rows are legacy-only and must never be aggregated with stamped rows.

**No raw SQL outside `state/repository.py`.** Schema migrations use `_ensure_column()` (idempotent `ALTER TABLE`) and `CREATE TABLE IF NOT EXISTS`. New tables go in `init_db()`.

**Scores are agent-native; competition uses `competition_score()`.** News Classifier scores are [0.0, 1.0] (confidence); Discovery scores are 0–100. `competition_score()` in `models.py` converts news to 0–100 for cross-agent comparisons. Display rendering (`format_score()`) uses the native scale.

## Architecture

Full detail in `architecture.md`. Key flows:

**Signal lifecycle:** agent job → `stage_signals()` (router) → `persist_routed_signals()` (common.py) → Telegram send → `confirm_delivered_signals()`. AR signals are delivered immediately; INFO signals are staged as CANDIDATE for the 17:40 `dispatch` job.

**Dispatch ranking** (v2): one reserved INFO slot for a fresh sentinel drawdown/day-drop trigger (ranked by unclamped severity ratio), then agent priority `news > sentinel > discovery`, with news ordered held → non-held → macro, then `competition_score`. Discovery does not compete in shadow mode. `route_signals()` (severity-first, single-batch, one AR) is legacy — used only by tests and replays.

**Job schedule:**
| Job | Cadence | Time (ET) |
|-----|---------|-----------|
| `news-morning` | Weekdays | 09:00 |
| `daily-close` | Weekdays | 16:15 |
| `discovery` | Weekdays | 16:15 |
| `sentinel` | Weekdays | 17:00 |
| `outcome-backfill` | Weekdays | 17:30 |
| `dispatch` | Weekdays | 17:40 |
| `watchdog` | Daily | — |
| `review-report` | 1st Saturday monthly | 09:00 |
| `weekly-synthesis` | Mondays | 08:00 |
| `backup` | Sundays | 08:00 |
| `telegram-poll` | Daily | every 15 min, 07:00-23:00 |
