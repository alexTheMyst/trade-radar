# Roadmap: Signal System — Rules-Based Investment Alert Engine

## Overview

Building on the completed MVP Week 1 infrastructure (DB, heartbeat, email, config, dispatcher), this milestone delivers the two AI agents, the alert router, and the job orchestrators that form the complete signal system. Work proceeds bottom-up by dependency: shared types and foundation first, data-layer extensions second, agents third (News Classifier and Discovery Agent can be built in parallel), router fourth, and thin job orchestrators last. The system goes live when operators can run `news-morning` and `discovery` jobs that emit heartbeat-monitored, budget-capped alerts to their inbox.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Foundation** - Shared Signal type, thesis taxonomy, ticker universe, and schema extensions
- [ ] **Phase 2: Data Layer** - Finnhub client extensions with rate limiting and news fetch
- [ ] **Phase 3: News Classifier** - Claude API agent, thesis-driven classification, prompt caching
- [x] **Phase 4: Discovery Agent** - Ticker scoring, Phase A logs-only mode, per-factor sub-scores (completed 2026-05-16)
- [x] **Phase 5: Alert Router** - Daily budget enforcement, slot competition, deterministic tiebreak (completed 2026-05-16)
- [ ] **Phase 6: Job Orchestration** - news-morning + discovery jobs, digest email, Windows Task Scheduler docs

## Phase Details

### Phase 1: Foundation
**Goal**: Establish the shared Signal contract, operator-maintained thesis taxonomy, deterministic ticker universe, and all schema extensions that every subsequent component depends on.
**Mode:** mvp
**Depends on**: Nothing (MVP Week 1 complete)
**Requirements**: TYPE-01, TYPE-02, TAX-01, TAX-02, TAX-03, TAX-04, UNIV-01, UNIV-02, UNIV-03, UNIV-04, SCHEMA-01, SCHEMA-02, SCHEMA-03, SCHEMA-04, SCHEMA-05, SCHEMA-06
**Success Criteria** (what must be TRUE):
  1. A `Signal` dataclass in `models.py` is importable by any module, and its `alert_id` is the SHA-256 of `ticker + date + rule + agent` — inserting the same signal twice produces one DB row, not two
  2. Operator can edit `thesis.yaml` pillars; running the news classifier with a `review_due` date in the past aborts the job and trips the healthchecks.io `/fail` ping before any classification occurs
  3. The ticker universe loads ~1,500 tickers; running `universe.py` for the same ticker on the same day always places it in the same rotation partition (deterministic `hashlib.md5`); core holdings appear in every daily subset regardless of partition
  4. K-1 ETFs (USO, UNG, DBC, GSG) are not present in any scanned subset — exclusion happens at universe-builder level, not downstream
  5. `sqlite3 state/signals.db ".schema"` shows `routing_status`, `signal_price_snapshot`, `model_version`, `wash_sale` table with `account` column, `llm_calls` table, and `repository.py` exposes `count_delivered_today()`
**Plans**: 1 plan
  - [x] 01-01-PLAN.md — Foundation: Signal dataclass, schema migration, thesis loader, universe partitioning
### Phase 2: Data Layer
**Goal**: Extend the Finnhub client with bulk quote fetch, news headline fetch, rate-limit token bucket, retry logic, and graceful paid-tier endpoint detection.
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: DATA-01, DATA-02, DATA-03, DATA-04
**Success Criteria** (what must be TRUE):
  1. Passing a list of 100 tickers to the bulk quote fetch completes without exceeding 55 Finnhub calls/min — verified by logging call timestamps
  2. When Finnhub returns a 429, the client retries up to 5 times with exponential backoff (via `tenacity`) and succeeds on recovery; the retry count is logged
  3. When a paid-tier endpoint returns 403 or 404, the client logs a warning and returns `None` — the calling code skips scoring that ticker rather than raising an exception or producing a zero score
  4. The client fetches company news headlines for a given ticker and date range; returned items include at minimum the headline text and source
**Plans**: 1 plan
  - [x] 02-PLAN.md — Data Layer: token bucket, tenacity retry, fetch_quotes, fetch_company_news

### Phase 3: News Classifier
**Goal**: Deliver a working News Classifier agent that fetches, sanitizes, and classifies headlines against thesis pillars via the Anthropic tool-use API, emitting typed Signal objects with deduplication and parse-failure safety.
**Mode:** mvp
**Depends on**: Phase 1, Phase 2
**Requirements**: CLFY-01, CLFY-02, CLFY-03, CLFY-04, CLFY-05, CLFY-06
**Success Criteria** (what must be TRUE):
  1. Running the classifier against a ticker with recent news produces `Signal` objects with per-pillar confidence scores — no email is sent directly; signals are returned to the caller
  2. The Anthropic call uses `temperature=0`, the pinned model ID from config, and the `thesis.yaml` system prompt with `cache_control: {type: "ephemeral"}` — verified by inspecting `llm_calls` rows for `cache_read_input_tokens > 0` on repeated runs
  3. Injecting a malformed headline (control characters, 800-char string) results in a sanitized, `<headline>`-delimited string reaching the API — the raw content never appears in the prompt
  4. When the API returns unparseable JSON, a MONITORING-severity signal row is inserted with `raw_response` captured — no classification attempt is silently dropped
  5. Running the classifier twice on the same ticker and trading day produces the same set of `alert_id` values; duplicate headlines are not re-classified
**Plans**: 1 plan
  - [ ] 03-PLAN.md — News Classifier: sanitization, messages.parse() with cached thesis, tenacity-retried parse-failure recovery, two-layer dedup, Signal extended with model_version + thesis_version_hash, insert_llm_call helper

### Phase 4: Discovery Agent
**Goal**: Deliver a working Discovery Agent that scores tickers from the rotation universe across 4 weighted factors, enforces a score-floor guard for missing data, and operates in Phase A logs-only mode controlled entirely by config.
**Mode:** mvp
**Depends on**: Phase 1, Phase 2 (can run parallel to Phase 3)
**Requirements**: DISC-01, DISC-02, DISC-03, DISC-04, DISC-05
**Success Criteria** (what must be TRUE):
  1. Running the agent against the day's rotation partition produces `Signal` objects with per-factor sub-scores (35/30/25/10 weights) retained on each signal — no email is sent directly
  2. A ticker missing one or more required Finnhub data fields produces no signal at all — it is not scored with a partial or zero score
  3. Setting `DISCOVERY_PHASE=A` in `.env` causes all agent output to be written to SQLite as MONITORING rows; changing to `DISCOVERY_PHASE=B` enables live routing — no code change required in either direction
  4. After each run, the `runs` table row for that job contains the list of tickers that were scanned in that execution
**Plans**: 4 plans
  - [x] 06-01-PLAN.md — Shared orchestration helpers + `news-morning`
  - [x] 06-02-PLAN.md — `discovery` job wiring and digest guardrails
  - [ ] 06-03-PLAN.md — deferred outcome backfill + ops artifacts
  - [ ] 06-04-PLAN.md — verification + audit closeout

### Phase 5: Alert Router
**Goal**: Deliver the Alert Router that enforces daily delivery budgets, runs slot competition with deterministic tiebreaking, and writes suppressed signals to SQLite with full audit trail — always reading budget state from the DB, never from memory.
**Mode:** mvp
**Depends on**: Phase 1, Phase 3, Phase 4
**Requirements**: ROUT-01, ROUT-02, ROUT-03, ROUT-04, ROUT-05
**Success Criteria** (what must be TRUE):
  1. Submitting 5 ACTION_REQUIRED signals to the router in a single day results in exactly 1 DELIVERED row and 4 SUPPRESSED rows — the delivered signal has the highest score; the 4 suppressed rows each have a `demoted_from` reason code and unmodified `severity`
  2. Running the router a second time on the same day (simulating news-morning + discovery both running) reads the current DELIVERED count from the DB and correctly refuses to exceed the budget cap
  3. Two equal-scored ACTION_REQUIRED signals always resolve to the same winner across reruns — alphabetical ticker order is the tiebreak; the loser is SUPPRESSED, not dropped
  4. The daily budget counter resets at `America/New_York` midnight — a signal submitted at 11:59 PM ET is constrained by today's budget; a signal submitted at 12:01 AM ET sees a fresh zero count
**Plans**: 2 plans
  - [x] 05-01-PLAN.md — Alert Router: pure route_signals(), demoted_from reason codes, cross-run budget read, ET midnight reset
**Goal**: Wire the agents, router, and delivery layer into runnable `news-morning` and `discovery` jobs, guarantee zero-alert-day digests, document Windows Task Scheduler setup, and schedule the deferred outcome backfill.
**Mode:** mvp
**Depends on**: Phase 3, Phase 4, Phase 5
**Requirements**: JOBS-01, JOBS-02, JOBS-03, JOBS-04, MEAS-01, MEAS-02, OPS-01, OPS-02
**Success Criteria** (what must be TRUE):
  1. `python -m signal_system news-morning` completes end-to-end: fetches headlines, classifies, routes, and sends a digest email — all inside a heartbeat context manager that trips `/fail` on any unhandled exception
  2. `python -m signal_system discovery` completes end-to-end: loads universe, scores the day's rotation partition, and logs results — routed in Phase B, MONITORING-only in Phase A — inside a heartbeat context manager
  3. On a day with zero alerts, `news-morning` still sends a digest email containing "Scanned N tickers, 0 alerts" — silence with confirmation, never ambiguity
  4. Running `news-morning` against a feed with 100+ headlines stops processing at 50; excess headlines are written as MONITORING rows with "volume cap reached" in the note field
  5. The operator `signals` table has `acted`, `acted_at`, and `user_note` columns; the Windows Task Scheduler setup guide includes an exported `.xml` task file covering absolute paths, Eastern Time trigger, and single-instance enforcement; the outcome backfill job (`MEAS-02`) exists as code but is documented as "activate ~30 days post go-live"
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order. Phase 3 and Phase 4 can be built in parallel (both depend on Phases 1 and 2, neither depends on the other).

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 1/1 | Complete | 2026-05-15 |
| 2. Data Layer | 1/1 | Complete | 2026-05-15 |
| 3. News Classifier | 0/TBD | Not started | - |
| 4. Discovery Agent | 1/1 | Complete   | 2026-05-16 |
| 5. Alert Router | 2/2 | Complete | 2026-05-16 |
| 6. Job Orchestration | 2/4 | In Progress | - |
