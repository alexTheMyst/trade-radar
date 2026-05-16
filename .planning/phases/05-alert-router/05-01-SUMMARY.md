# Phase 05 Summary: Alert Router

**Status:** Complete  
**Completed:** 2026-05-16  
**Commits:** `8718906`, `3f67604`, `1771305`

## What Was Built

### Files Created
- `src/signal_system/router/__init__.py` — package entry point, exports `route_signals`
- `src/signal_system/router/alert_router.py` — pure `route_signals()` function (no DB writes)
- `tests/test_alert_router.py` — 9 tests covering T-AR-01..T-AR-09

### Files Modified
- `src/signal_system/state/repository.py` — `insert_signal()` gains `demoted_from: str | None = None` kwarg; `init_db()` adds `_ensure_column` for `demoted_from TEXT`

## Key Design Decisions

- **Pure function:** `route_signals()` performs one DB read (`count_delivered_today()`), zero writes. Caller (Phase 6 job) handles `insert_signal()` calls.
- **Budget caps:** `_BUDGET_AR = 1`, `_BUDGET_INFO = 3` — module-level constants (not config).
- **Sort order:** Severity-first (AR before INFO), score descending, alphabetical ticker ascending tiebreak. None-safe: `(-(s.score or 0.0), s.ticker or "")`.
- **demoted_from codes:** `"budget_cap_ar"` / `"budget_cap_info"` when cap was already full from prior runs; `"outscored"` when intra-batch competition lost; `None` for DELIVERED.
- **MONITORING guard:** `ValueError` raised immediately if any input signal has `severity == "MONITORING"` — router has zero knowledge of the MONITORING path.
- **ET midnight reset:** Inherited from `count_delivered_today()` — uses `LIKE 'YYYY-MM-DD%'` on ET-formatted timestamps. No new timezone code in router.
- **No eviction:** Budget is enforced as a hard cap. A higher-scored same-day intra-batch signal cannot evict an already-DELIVERED cross-run signal.

## Test Coverage (T-AR-01..T-AR-09)

| Test | Scenario |
|------|----------|
| T-AR-07 | `test_empty_input` — [] → [] |
| T-AR-06 | `test_monitoring_raises` — ValueError on MONITORING severity |
| T-AR-08 | `test_delivered_demoted_from_is_none` — DELIVERED always has dmf=None |
| T-AR-01 | `test_ar_budget_one_winner` — 5 AR → 1 DELIVERED, 4 outscored |
| T-AR-04 | `test_tiebreak_alphabetical` — AAPL beats MSFT at equal score |
| T-AR-02 | `test_mixed_batch_allocation` — 1 AR + 3 INFO delivered, 3 suppressed |
| T-AR-03 | `test_cross_run_ar_full` — prior DELIVERED in DB → budget_cap_ar |
| T-AR-05 | `test_cross_run_info_full` — 3 INFO in DB → budget_cap_info |
| T-AR-09 | `test_et_midnight_reset` — yesterday's DELIVERED doesn't burn today's slot |

**96 total tests, 0 failures.**

## Phase 6 Handoff

`route_signals()` returns `list[tuple[Signal, str, str | None]]` — each tuple is `(signal, routing_status, demoted_from)`. Phase 6 job orchestrators must:
1. Call `route_signals(signals)` to get routing decisions
2. Call `repository.insert_signal(signal, routing_status=rs, demoted_from=dmf)` for each result
3. Send email only for DELIVERED signals; log SUPPRESSED to DB only (already handled by insert_signal)
