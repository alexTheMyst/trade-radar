# Requirements: Signal System

**Defined:** 2026-05-14
**Core Value:** Never miss a material thesis-relevant event on a held position — silent failure is indistinguishable from "no alerts today."

---

## Already Validated (MVP Week 1)

The following requirements are complete and committed. They form the foundation for all v1 work.

- ✓ **MVP-01**: Operator can run `python -m signal_system daily-close` and have the job execute end-to-end — existing
- ✓ **MVP-02**: System pings healthchecks.io /start, /success, and /fail around every job — existing
- ✓ **MVP-03**: System stores signal and run records in SQLite with WAL mode — existing
- ✓ **MVP-04**: System sends email alerts via Gmail SMTP — existing
- ✓ **MVP-05**: System loads and validates all required environment variables at startup — existing
- ✓ **MVP-06**: System fetches daily market data via Finnhub free-tier API — existing
- ✓ **MVP-07**: Test suite covers DB init, signal insert, heartbeat, and job error paths — existing

---

## v1 Requirements

Requirements for the current milestone. Each maps to roadmap phases.

### Shared Types

- [x] **TYPE-01**: System has a canonical `Signal` dataclass in `models.py` that serves as the contract between agents and the router (ticker, score, severity, agent, timestamp, alert_id)
- [x] **TYPE-02**: `alert_id` is a deterministic content-hash (SHA-256 of ticker + date + rule + agent), not a UUID — enables idempotent reruns

### Taxonomy

- [x] **TAX-01**: Operator can maintain investment thesis pillars in `thesis.yaml` without code changes
- [x] **TAX-02**: System refuses to run the News Classifier when `thesis.yaml`'s `review_due` date is past — hard abort, not a warning, trips /fail ping
- [x] **TAX-03**: thesis.yaml is loaded once per job start and validated against a Pydantic schema before any classification occurs
- [x] **TAX-04**: System stores `thesis_version_hash` (SHA-256 of file contents) on every classified signal row for IC comparability across thesis versions

### Universe

- [x] **UNIV-01**: System maintains a static ticker universe of ~1,500 tickers with a `core_holding` flag for positions the operator holds
- [x] **UNIV-02**: System partitions the universe into thirds for daily rotation using deterministic `hashlib.md5(ticker)` — not Python's `hash()` — so the same ticker lands in the same partition every day
- [x] **UNIV-03**: Core holdings are scanned every day regardless of rotation partition
- [x] **UNIV-04**: K-1 ETFs (USO, UNG, DBC, GSG) are excluded at the universe-builder level, not at alert time

### Data Layer

- [x] **DATA-01**: Finnhub client supports bulk quote fetch for a list of tickers with a preemptive rate-limit token bucket (≤55 calls/min)
- [x] **DATA-02**: System retries Finnhub 429 responses with exponential backoff via `tenacity` (up to 5 attempts)
- [x] **DATA-03**: System detects paid-tier Finnhub endpoints (403/404 for free-tier accounts) and skips gracefully with a logged warning — does not score tickers with missing data
- [x] **DATA-04**: Finnhub client fetches company news headlines for a ticker within a date range

### Schema

- [x] **SCHEMA-01**: `signals` table has a `routing_status` column (DELIVERED / MONITORING / SUPPRESSED) — router sets this, never modifies `severity`
- [x] **SCHEMA-02**: `signals` table has a `signal_price_snapshot` column capturing unadjusted price at signal generation time (for outcome measurement)
- [x] **SCHEMA-03**: `signals` table has a `model_version` column storing the pinned Claude model ID used for classification
- [x] **SCHEMA-04**: System has a `wash_sale` table with an `account` column from day one (4 accounts: schwab_main, schwab_secondary, roth_ira, hsa)
- [x] **SCHEMA-05**: System has an `llm_calls` table logging token counts per classifier invocation (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, job name, timestamp)
- [x] **SCHEMA-06**: `repository.py` has a `count_delivered_today()` function that queries today's DELIVERED signal count by severity — used by router, no in-memory alternatives

### News Classifier

- [x] **CLFY-01**: News Classifier fetches company news headlines from Finnhub, sanitizes them (strip control chars, cap at 500 chars, wrap in `<headline>` delimiters), and classifies each against thesis pillars
- [x] **CLFY-02**: Classification uses the Anthropic tool-use API with a typed schema — not free-text parsing; `temperature=0`; pinned model ID from config
- [x] **CLFY-03**: thesis.yaml system prompt is passed with `cache_control: {type: "ephemeral"}` to enable prompt caching across headlines within a single job run
- [x] **CLFY-04**: On structured output parse failure (second retry), system inserts a MONITORING-severity signal with `raw_response` captured — never silently drops a classification attempt
- [x] **CLFY-05**: News Classifier emits `Signal` objects with per-pillar confidence scores; it never sends email directly
- [x] **CLFY-06**: Headline deduplication within a trading day prevents re-classifying the same story from multiple sources

### Discovery Agent

- [x] **DISC-01**: Discovery Agent scores tickers across 4 factors (configurable weights defaulting to 35/30/25/10) using available Finnhub free-tier endpoints
- [x] **DISC-02**: Tickers with missing required data fields receive no score (score-floor guard) — not an artificially low score
- [x] **DISC-03**: Discovery Agent operates in Phase A (logs-only) mode when `DISCOVERY_PHASE=A` is set in config; Phase B (live routing) is activated by changing the config value — no code change required
- [x] **DISC-04**: Discovery Agent emits `Signal` objects with per-factor sub-scores retained; it never sends email directly
- [x] **DISC-05**: Discovery Agent logs the set of tickers scanned in each run to the `runs` table for auditability

### Alert Router

- [x] **ROUT-01**: Alert Router enforces daily hard caps: 1 ACTION_REQUIRED and 3 INFORMATIONAL signals delivered per day, regardless of how many agents run
- [x] **ROUT-02**: When competing signals exceed the budget cap, the higher-scoring signal wins the slot; the loser is written to SQLite with `routing_status=SUPPRESSED` and a `demoted_from` reason code — severity is never mutated
- [x] **ROUT-03**: Router reads today's delivered signal count from the DB (`count_delivered_today()`), not from in-memory state — safe when multiple jobs run same day
- [x] **ROUT-04**: Budget reset uses `America/New_York` midnight — never UTC or timezone-naive `datetime.now()`
- [x] **ROUT-05**: Tiebreaking between equal-scored signals is deterministic (alphabetical by ticker as secondary sort) — reruns produce identical routing decisions

### Job Orchestration

- [x] **JOBS-01**: Operator can run `python -m signal_system news-morning` to execute: fetch headlines → classify → route → send digest email — all wrapped in heartbeat context manager
- [x] **JOBS-02**: Operator can run `python -m signal_system discovery` to execute: load universe → score rotation partition → route (Phase A: skip router) → log — all wrapped in heartbeat context manager
- [x] **JOBS-03**: Daily digest email is always sent, even on zero-alert days — "Scanned N tickers, 0 alerts" with explicit confirmation, never silence
- [x] **JOBS-04**: Job run hard-caps: news-morning processes maximum 50 headlines per run; excess headlines are written as MONITORING rows with "volume cap reached" note

### Measurement

- [x] **MEAS-01**: `signals` table has operator feedback fields (`acted`, `acted_at`, `user_note`) filled manually within 7 days of alert
- [x] **MEAS-02**: Signal outcome backfill job idempotently fills `outcome_price_30d` and `outcome_price_90d` via Finnhub for rows where `acted IS NOT NULL` — deferred until ~30 days post go-live

### Operations

- [x] **OPS-01**: Windows Task Scheduler configuration is documented with an exported `.xml` task file as reference — not just prose — covering: "run whether logged on or not," absolute paths, missed-run policy, Eastern Time trigger, single-instance enforcement
- [x] **OPS-02**: Setup guide documents the Gmail filter (`from:GMAIL_USERNAME → never send to spam`) and instructs operator to configure healthchecks.io SMS/push (not email) as the canonical "job ran" signal

---

## v2 Requirements

Deferred to future. Not in current roadmap.

### Measurement (Long Horizon)

- **MEAS-V2-01**: Information Coefficient (Spearman rank correlation between score and 30d outcome) tracked per agent type — deferred until ~30 days of live signal data exists
- **MEAS-V2-02**: Hit-rate vs base-rate comparison per signal type — deferred until sufficient sample size
- **MEAS-V2-03**: Score normalization to percentile rank within scanned universe — deferred until Phase A calibration data exists
- **MEAS-V2-04**: Weight version stamp on Discovery signals for IC interpretability after weight changes

### Operational

- **OPS-V2-01**: SQLite WAL checkpoint + weekly VACUUM job + rolling weekly backup script — deferred to post-go-live hardening
- **OPS-V2-02**: `ticker_delisted_at` column in universe table with outcome backfill as of delisting date
- **OPS-V2-03**: Quarterly review tooling: query for unannotated signals older than 7 days, prompt annotation or "expired" mark

### Signal Quality

- **QUAL-V2-01**: Source quality tier/whitelist for Finnhub news sources — deferred until baseline quality is measured
- **QUAL-V2-02**: Pillar delta vs absolute-level distinction in News Classifier (alert when trend changes direction, not just when it's high)

---

## Out of Scope

| Feature | Reason |
|---------|--------|
| Automated trade execution | System is alert-only by design; execution changes risk profile entirely |
| Self-learning / adaptive scoring | Operator adapts weights manually at quarterly review |
| Earnings Setup agent | Covered natively by Schwab |
| Portfolio Drift agent | Covered natively by Schwab |
| Regime classifier | Subsumed into news classifier pillar deltas |
| GitHub Actions runner | Windows Task Scheduler only |
| `^GSPC`, `^VIX`, insider sentiment, `/scan/technical-indicator` | May be Finnhub paid-only; validate before use, adjust scoring formula if unavailable |
| Real-time intraday tick streaming | Finnhub free tier + Task Scheduler is a daily-batch system |
| Multi-user / multi-tenant | Solo-operator system |
| UI dashboard | Email delivery is the interface |
| `asyncio` / `aiohttp` | Windows event-loop quirks; sequential jobs are correct |
| ORM (SQLAlchemy, peewee) | `repository.py` is the access layer; no ORMs |
| LLM frameworks (LangChain, CrewAI, AutoGen) | Conflict with heartbeat/dispatcher design |
| Aggregate IC across signal types | Must always be per-agent-type; aggregation is misleading |

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| TYPE-01 | Phase 1 | Complete |
| TYPE-02 | Phase 1 | Complete |
| TAX-01 | Phase 1 | Complete |
| TAX-02 | Phase 1 | Complete |
| TAX-03 | Phase 1 | Complete |
| TAX-04 | Phase 1 | Complete |
| UNIV-01 | Phase 1 | Complete |
| UNIV-02 | Phase 1 | Complete |
| UNIV-03 | Phase 1 | Complete |
| UNIV-04 | Phase 1 | Complete |
| SCHEMA-01 | Phase 1 | Complete |
| SCHEMA-02 | Phase 1 | Complete |
| SCHEMA-03 | Phase 1 | Complete |
| SCHEMA-04 | Phase 1 | Complete |
| SCHEMA-05 | Phase 1 | Complete |
| SCHEMA-06 | Phase 1 | Complete |
| DATA-01 | Phase 2 | Complete |
| DATA-02 | Phase 2 | Complete |
| DATA-03 | Phase 2 | Complete |
| DATA-04 | Phase 2 | Complete |
| CLFY-01 | Phase 3 | Complete |
| CLFY-02 | Phase 3 | Complete |
| CLFY-03 | Phase 3 | Complete |
| CLFY-04 | Phase 3 | Complete |
| CLFY-05 | Phase 3 | Complete |
| CLFY-06 | Phase 3 | Complete |
| DISC-01 | Phase 4 | Complete |
| DISC-02 | Phase 4 | Complete |
| DISC-03 | Phase 4 | Complete |
| DISC-04 | Phase 4 | Complete |
| DISC-05 | Phase 4 | Complete |
| ROUT-01 | Phase 5 | Complete |
| ROUT-02 | Phase 5 | Complete |
| ROUT-03 | Phase 5 | Complete |
| ROUT-04 | Phase 5 | Complete |
| ROUT-05 | Phase 5 | Complete |
| JOBS-01 | Phase 6 | Complete |
| JOBS-02 | Phase 6 | Complete |
| JOBS-03 | Phase 6 | Complete |
| JOBS-04 | Phase 6 | Complete |
| MEAS-01 | Phase 6 | Complete |
| MEAS-02 | Phase 6 | Complete |
| OPS-01 | Phase 6 | Complete |
| OPS-02 | Phase 6 | Complete |

**Coverage:**
- v1 requirements: 44 total
- Mapped to phases: 44
- Unmapped: 0 ✓

---
*Requirements defined: 2026-05-14*
*Last updated: 2026-05-17 after Phase 6 closeout and milestone audit refresh*
