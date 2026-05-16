---
phase: 04-discovery-agent
plan: 01
subsystem: discovery
tags: [discovery-agent, scoring, cross-sectional-ranking, sqlite, phase-a-b]
dependency_graph:
  requires:
    - 01-foundation (Signal dataclass, repository)
    - 02-data-pipeline (finnhub_client fetch_quotes, fetch_company_news)
    - 03-classifier (news_classifier structural template)
  provides:
    - score_universe() public function
    - fetch_quote() validated single-quote accessor
    - Signal.signal_price_snapshot field
    - repository.update_run_counts()
    - repository.insert_signal(routing_status kwarg)
  affects:
    - src/signal_system/models.py (new field)
    - src/signal_system/state/repository.py (new kwarg, new function, new DB columns)
    - src/signal_system/data/finnhub_client.py (new public function)
tech_stack:
  added:
    - signal_system.discovery package (new)
  patterns:
    - 3-pass cross-sectional factor ranking (fetch → rank → emit)
    - Phase A/B config-driven routing (no code changes needed to switch)
    - Parameterised SQLite UPDATE for tickers_scanned/tickers_signaled
key_files:
  created:
    - src/signal_system/discovery/__init__.py
    - src/signal_system/discovery/discovery_agent.py
    - tests/test_discovery_agent.py
  modified:
    - src/signal_system/models.py
    - src/signal_system/state/repository.py
    - src/signal_system/data/finnhub_client.py
decisions:
  - "score_universe reads config.DISCOVERY_PHASE inside function body (not module-level) to allow monkeypatching in tests"
  - "signals_emitted tracks all >=60 signals regardless of phase so tickers_signaled count is accurate"
  - "fetch_quote rejects l<=0 and h<l as invalid data, but h==l (flat day) is valid — range_position=0.0 computed by caller"
  - "update_run_counts called before every return path (including empty list early returns)"
  - "Phase B uses two-call approach for alert_id determinism test (avoids DB uniqueness constraint interference)"
metrics:
  duration: "~10 minutes"
  completed: "2026-05-16"
  tasks_completed: 4
  files_created: 3
  files_modified: 3
---

# Phase 4 Plan 1: Discovery Agent Summary

## One-liner

Cross-sectional 4-factor scoring agent (momentum/volume/range/news, weights 35/30/25/10) with Phase A/B config switch using free-tier Finnhub endpoints.

## What Was Built

### Wave 0 — Prerequisites

**`src/signal_system/data/finnhub_client.py`** — Added `fetch_quote(ticker)` public function:
- Calls `_fetch_single_quote()` internally
- Score-floor guard: rejects quotes with `dp=None`, `v=None`, `h < l`, or `l <= 0`
- `h == l` (flat day) is VALID — range_position=0.0 is computed by the caller
- Returns full quote dict or None

**`src/signal_system/models.py`** — Added `signal_price_snapshot: float | None = None` field to `Signal` dataclass with default None for backward compatibility.

**`src/signal_system/state/repository.py`** — Three changes:
1. `insert_signal()` now accepts `routing_status: str | None = None` kwarg (back-compat)
2. INSERT VALUES wired to `routing_status` and `signal.signal_price_snapshot` (previously hardcoded None)
3. `init_db()` adds `tickers_scanned` and `tickers_signaled` columns to `runs` table (idempotent)
4. New `update_run_counts(run_id, tickers_scanned, tickers_signaled)` function

### Wave 1 — Discovery Package

**`src/signal_system/discovery/__init__.py`** — Package entry point exporting `score_universe`.

**`src/signal_system/discovery/discovery_agent.py`** — 3-pass scoring engine:
- **Pass 1 — Fetch**: calls `fetch_quote()` (score-floor guard) and `fetch_company_news()` for each ticker
- **Pass 2 — Cross-sectional ranking**: `_rank_values()` helper ranks all tickers 1.0→0.0 with alphabetical tiebreak; computes momentum/volume/range/news ranks simultaneously
- **Pass 3 — Score and emit**: composite = 35·m + 30·v + 25·r + 10·n; emits Signal if ≥60, severity ACTION_REQUIRED if ≥80
- Phase A: inserts directly to DB with `routing_status='MONITORING'`, returns []
- Phase B: returns list[Signal], no DB write
- Always calls `update_run_counts()` before returning

### Wave 2 — Test Suite

**`tests/test_discovery_agent.py`** — 18 tests covering all DISC requirements:
- T-01..T-06: scoring accuracy, score-floor guard, flat-day handling, news edge cases
- T-07..T-08: Phase A/B routing (insert_signal called/not-called with correct args)
- T-09..T-11: threshold suppression and severity assignment
- T-12..T-14: ranking ties, single-ticker edge case, empty universe
- T-15..T-18: DB counts, alert_id determinism, price snapshot, sub_scores structure

### Wave 3 — Integration Smoke

Full suite: **87 passed** (69 pre-existing + 18 new), 0 failures, 0 errors.

## Commits

| Wave | Hash | Message |
|------|------|---------|
| 0 | b796565 | Wave 0 — fetch_quote, Signal.signal_price_snapshot, repository extensions |
| 1 | 79602a5 | Wave 1 — score_universe discovery agent (3-pass, Phase A/B) |
| 2 | f67f0c0 | Wave 2 — test_discovery_agent.py (18 tests, all passing) |
| 3 | 10ae1dd | Wave 3 — integration smoke verified |

## Deviations from Plan

None — plan executed exactly as written. All critical implementation notes from the checker were applied as specified.

## Known Stubs

None — all data flows are wired. `score_universe()` calls real (mockable) fetch functions; `signal_price_snapshot` is populated from actual quote data; `update_run_counts` writes to real DB rows.

## Threat Flags

No new threat surface beyond what was declared in the plan's threat model. All mitigations applied:
- `routing_status` hardcoded as `"MONITORING"` literal (not from input)
- `update_run_counts` uses parameterised `?` placeholder
- `h == l` division guard applied before ranking
- `_rank_values` n==1 early return prevents division by zero

## Self-Check: PASSED

Files created:
- ✅ src/signal_system/discovery/__init__.py
- ✅ src/signal_system/discovery/discovery_agent.py
- ✅ tests/test_discovery_agent.py

Commits verified:
- ✅ b796565 (Wave 0)
- ✅ 79602a5 (Wave 1)
- ✅ f67f0c0 (Wave 2)
- ✅ 10ae1dd (Wave 3)

Test count: 87 passed, 0 failed.
