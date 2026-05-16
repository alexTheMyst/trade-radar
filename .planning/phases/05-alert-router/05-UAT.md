---
status: complete
phase: 05-alert-router
source: [05-01-SUMMARY.md]
started: "2026-05-16T18:05:00.000Z"
updated: "2026-05-16T18:05:00.000Z"
---

## Current Test

number: 5
name: MONITORING guard
expected: |
  ValueError raised on MONITORING severity input
result: pass (all 5 complete)
awaiting: done

## Tests

### 1. Import and empty input
expected: route_signals([]) returns [] — package importable, basic contract works
result: pass

### 2. AR budget cap — 5 signals, 1 winner
expected: 5 AR signals with scores 50..90 → T4 (score=90) DELIVERED, 4 others SUPPRESSED with demoted_from='outscored'
result: pass

### 3. Cross-run budget awareness
expected: When DB already has 1 AR DELIVERED today, a new AR signal gets routing_status=SUPPRESSED, demoted_from='budget_cap_ar'
result: pass

### 4. Alphabetical tiebreak
expected: AAPL and MSFT at equal score → AAPL DELIVERED, MSFT SUPPRESSED with demoted_from='outscored'
result: pass

### 5. MONITORING guard
expected: Passing a MONITORING-severity signal raises ValueError
result: pass

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps
