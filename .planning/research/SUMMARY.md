# Research Summary — Rules-Based Investment Signal System

**Synthesized:** 2026-05-14
**Research files:** STACK.md, FEATURES.md, ARCHITECTURE.md, PITFALLS.md, PROJECT.md

---

## Executive Summary

This is a rules-based, alert-only investment signal system for a single operator across 4 Schwab accounts. The system never executes trades — its entire value is avoiding silent failure on material thesis-relevant events. Two AI agents (News Classifier + Discovery Agent) produce scored signals independently; a central Alert Router enforces daily delivery budgets before anything reaches email. MVP Week 1 is complete: the infrastructure layer (DB, heartbeat, email, config, job dispatcher) is proven and committed. The next milestone builds the two agents, the router, and the supporting data/taxonomy layer on top of that foundation.

The recommended approach is a bottom-up build ordered by dependency: shared `Signal` type first, then data-layer extensions, then agents in parallel, then the router, then thin job orchestrators on top. The architecture pattern — agents return signals, never send; router enforces policy, never classifies; jobs orchestrate, never contain business logic — is the load-bearing design decision.

The two highest-consequence risks are silent failures that appear healthy: (1) a JSON parse error in the News Classifier that drops signals while heartbeat pings green, and (2) a universe rotation implementation using Python's `hash()` (non-deterministic across processes) rather than `hashlib.md5`. The Discovery Agent Phase A logs-only flag is a critical safety rail — the 35/30/25/10 scoring weights are initial guesses, not calibrated values.

---

## Stack

- **Python 3.12+ / uv / SQLite stdlib** — locked, non-negotiable; no ORMs, no frameworks
- **Anthropic SDK `messages.parse()`** — use for structured output; do NOT use `instructor` (redundant) or LangChain/CrewAI/AutoGen (conflicts with heartbeat/dispatcher design)
- **`tenacity>=8.0`** — only new dependency to add; for Finnhub 429 retries and Claude transient errors
- **Rate limiting: `time.sleep()` token bucket in `finnhub_client.py`** — 55 calls/min (headroom below 60); do NOT use `asyncio`, `pyrate-limiter`, or `aiohttp`
- **Prompt caching: `cache_control: {type: "ephemeral"}`** on thesis.yaml system prompt prefix — reduces per-headline Claude costs on news-morning runs
- **Alert Router: custom staging-table pattern** — no library exists for this; content-hash `alert_id` + `BEGIN EXCLUSIVE` transaction
- **Avoid:** `asyncio`, `Celery/RQ/APScheduler`, `SQLAlchemy`, `Redis`, `httpx`, any AI agent framework

---

## Table Stakes

Features without which the system fails its core promise of never missing a material event:

1. **`models.py` canonical `Signal` dataclass** — shared contract between agents and router; every field added here, nowhere else
2. **thesis.yaml `review_due` gate** — hard abort (not warning) that propagates through heartbeat to trip `/fail`; load once at job start, never per-headline
3. **Headline sanitization** — `<headline>` delimiters, control-char strip, 500-char cap before any Claude call
4. **Structured output via tool-use API** — parse failures must INSERT a MONITORING row with `raw_response` captured, never silently drop
5. **Alert Router daily budget from DB query** — `count_delivered_today()` in `repository.py`, not in-memory; safe when news-morning and discovery both run same day
6. **`routing_status` column (DELIVERED / MONITORING / SUPPRESSED)** — router never mutates `severity`; measurement integrity depends on preserving original severity on suppressed signals
7. **`alert_id` as content-hash + UNIQUE constraint + `INSERT OR IGNORE`** — deduplication on job retry; `sha256(ticker + date + rule_id + agent)`
8. **Discovery Agent Phase A config flag** — `DISCOVERY_PHASE=A` in `.env` writes to MONITORING only; Phase B promotion is a config change, not a code change
9. **K-1 ETF exclusion at universe-builder level** — USO, UNG, DBC, GSG filtered in `universe.py`, never at alert time
10. **`wash_sale` table with `account` column from day one** — 4 accounts; retrofitting is painful

---

## Build Order

Ordered by hard dependencies. Steps 6a/6b and 8a/8b can be built in parallel.

1. **`models.py`** — canonical `Signal` dataclass; unblocks everything; zero dependencies
2. **`thesis.yaml` + `data/thesis_loader.py`** — operator taxonomy with `review_due` gate; zero dependencies
3. **`data/universe.py`** — ~1,500 ticker list, `core_holding` flag, K-1 exclusions, deterministic `hashlib.md5` rotation; zero dependencies
4. **`data/finnhub_client.py` extensions** — bulk quote fetch, company news fetch, rate-limit token bucket; depends on `config.py` (done)
5. **`state/repository.py` extensions** — `routing_status` column on `signals`, `count_delivered_today()`, `wash_sale` table, `llm_calls` table; depends on `models.py`
6a. **`agents/news_classifier.py`** — thesis-driven Claude classification, `temperature=0`, pinned model ID, prompt caching; depends on steps 1, 2, 4
6b. **`agents/discovery_agent.py`** — 35/30/25/10 scoring, score-floor guard for missing data, per-factor sub-scores; depends on steps 1, 3, 4
7. **`routing/alert_router.py`** — budget enforcement, slot competition, demotion audit trail, deterministic tiebreak; depends on steps 1, 5
8a. **`jobs/news_morning.py`** — thin orchestrator: heartbeat + classifier + router + delivery; depends on steps 6a, 7
8b. **`jobs/discovery.py`** — thin orchestrator: heartbeat + agent + Phase A/B flag; depends on steps 6b, 7
9. **`__main__.py` extension** — add `news-morning` and `discovery` to JOBS dispatch dict
10. **Signal outcome backfill cron** — idempotent; defer until ~30 days post go-live when signal rows with `acted` field exist

---

## Watch Out For

The 5 most consequential pitfalls:

1. **Structured output parse failure drops signals silently** — Claude returns prose or refuses; heartbeat pings green; material events are lost. Mitigation: use tool-use API for schema-validated responses; on any parse failure INSERT a MONITORING row with `raw_response` captured.

2. **LLM non-determinism corrupts measurement** — default temperature and floating model aliases make same headlines return different severity across runs. Mitigation: `temperature=0` on every classifier call; pin exact model ID string (e.g., `claude-sonnet-4-6`) in `config.py`; log `model_version` and `thesis_version_hash` on every signal row.

3. **Universe rotation using `hash()` instead of `hashlib.md5`** — Python's built-in `hash()` randomizes per process; coverage gaps are invisible. Mitigation: always use `hashlib.md5(symbol.encode()).hexdigest()` for bucket assignment; never use `hash()`.

4. **Alert Router budget maintained in-memory** — if news-morning and discovery both invoke the router same day, the second invocation sees a stale zero-count. Mitigation: router always queries `repository.count_delivered_today()` from DB; never accumulate counts in a module-level variable.

5. **Windows Task Scheduler silent non-start** — process never starts, healthchecks.io gets no ping at all. Mitigation: "Run whether user is logged on or not"; absolute paths in all task fields; "run as soon as possible after missed start"; produce a tested `.xml` task export as reference config.

---

## Open Questions

Items requiring empirical validation before writing the dependent code:

1. **Which Finnhub free-tier endpoints actually return data for the 35/30/25/10 Discovery scoring weights?** — Validate each endpoint with a live API call before writing scoring code; adjust formula if any are unavailable.

2. **Does `messages.parse()` support `temperature=0.0` alongside `output_format`?** — Verify at implementation; if not, fall back to tool-use API with `tool_choice={"type": "tool"}`.

3. **What is the exact minimum token threshold for Anthropic prompt caching?** — Verify against current pricing docs before relying on cache savings.

4. **Does Finnhub `/stock/candle` support `adjusted=False` on free tier?** — Needed for unadjusted `signal_price_snapshot` at outcome backfill time.

5. **What is the realistic news headline volume on a high-news day per free-tier Finnhub?** — The hard headline cap (recommended: 50/run) must be set before news-morning goes live.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack choices | HIGH | Locked constraints from CLAUDE.md; SDK-native `messages.parse()` confirmed |
| Architecture patterns | HIGH | Derived directly from existing codebase; Produce → Route → Deliver already implicit in MVP |
| Feature scope | HIGH | Table stakes list is conservative and grounded in core-value statement |
| Pitfall mitigations | HIGH | Each mitigation is concrete and implementable |
| Finnhub endpoint availability | LOW | Free-tier gaps are the single largest empirical unknown; scoring formula depends on it |
| Discovery scoring weights | LOW | 35/30/25/10 are explicitly labeled initial guesses; Phase A calibration is the answer |
| Prompt caching token threshold | MEDIUM | Likely fine for a real thesis.yaml; verify at implementation |

**Overall: HIGH confidence on architecture and build order; LOW confidence on Discovery Agent scoring inputs until Finnhub endpoints are validated empirically.**
