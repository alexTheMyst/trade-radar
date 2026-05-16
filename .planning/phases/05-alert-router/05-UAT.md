---
status: complete
phase: 05-alert-router
source: 05-01-SUMMARY.md
started: 2026-05-16T10:47:22-07:00
updated: 2026-05-16T11:37:53-07:00
---

## Current Test

[testing complete]

## Tests

### 1. Router package import and empty input
expected: Run `uv run python -c "from signal_system.router import route_signals; print(route_signals([]))"`. It should print `[]` with no import error.
result: pass

### 2. Repository demotion metadata persistence
expected: Run `uv run pytest tests/test_smoke.py -k "new_schema or demoted_from" -q`. It should pass, confirming `init_db()` adds the `demoted_from` column and `insert_signal(..., demoted_from=...)` persists suppression metadata.
result: pass

### 3. Router allocation behavior
expected: Run `uv run pytest tests/test_alert_router.py -k "ar_budget_one_winner or mixed_batch_allocation or tiebreak_alphabetical" -q`. It should pass, confirming AR/INFO budget caps, highest-score winners, and alphabetical tiebreak behavior.
result: pass

### 4. Cross-run, purity, and guard behavior
expected: Run `uv run pytest tests/test_alert_router.py -k "cross_run_ar_full or cross_run_info_full or et_midnight_reset or pure_no_db_writes or monitoring_raises" -q`. It should pass, confirming no-eviction cross-run logic, ET midnight reset, router purity, and the MONITORING guard.
result: pass

### 5. Full router regression suite
expected: Run `uv run pytest tests/test_alert_router.py -q` and then `uv run pytest -q`. Both commands should pass; the router test file should report 10 passing tests and the full suite should be green.
result: pass

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none yet]
