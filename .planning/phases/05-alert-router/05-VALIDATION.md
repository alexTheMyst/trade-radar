---
phase: "05"
slug: alert-router
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-16
---

# Phase 05 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 (via uv) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_alert_router.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~23 seconds |

**Current state:** 10 router tests pass in `tests/test_alert_router.py`; full repository suite is 101 passing tests.

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_alert_router.py -q`
- **After every plan wave:** `uv run pytest -q`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** ~23 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 05-T1 | 01 | 0 | ROUT-02 | SQL parameterization | `demoted_from` column exists after `init_db()` and `insert_signal(..., demoted_from=...)` persists suppression metadata correctly | unit | `uv run pytest tests/test_smoke.py -k "new_schema or demoted_from" -q` | ✅ | ✅ green |
| 05-T2 | 01 | 1 | ROUT-01, ROUT-02 | MONITORING guard; no DB writes | `route_signals` imports cleanly, returns `[]` for empty input, rejects `MONITORING`, and stays pure (no insert/write path) | unit | `uv run pytest tests/test_alert_router.py -k "empty_input or monitoring_raises or pure_no_db_writes" -q` | ✅ | ✅ green |
| 05-T3 | 01 | 1 | ROUT-01, ROUT-05 | Deterministic score competition | Intra-batch AR competition uses score-descending plus alphabetical ticker tiebreak | unit | `uv run pytest tests/test_alert_router.py -k "ar_budget_one_winner or tiebreak_alphabetical" -q` | ✅ | ✅ green |
| 05-T4 | 01 | 2 | ROUT-01, ROUT-02, ROUT-03, ROUT-04 | No eviction; ET date budget reset | Mixed-batch allocation, cross-run budget caps, INFO caps, and ET midnight reset all behave as specified | unit + integration | `uv run pytest tests/test_alert_router.py -k "mixed_batch_allocation or cross_run_ar_full or cross_run_info_full or et_midnight_reset" -q` | ✅ | ✅ green |
| 05-T5 | 01 | 2 | ROUT-01..ROUT-05 | all | Full router validation plus repo-wide regression coverage | integration | `uv run pytest tests/test_alert_router.py -q && uv run pytest -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Coverage Summary

| Requirement | Description | Tests | Status |
|-------------|-------------|-------|--------|
| ROUT-01 | Hard caps: 1 `ACTION_REQUIRED` + 3 `INFORMATIONAL` per day across agents | `test_empty_input`, `test_ar_budget_one_winner`, `test_mixed_batch_allocation` | ✅ COVERED |
| ROUT-02 | Slot competition, `demoted_from` reason codes, and repository/schema support for suppression metadata | `test_delivered_demoted_from_is_none`, `test_insert_signal_persists_demoted_from`, suppressed-path assertions in `test_ar_budget_one_winner`, `test_mixed_batch_allocation`, `test_cross_run_ar_full`, `test_cross_run_info_full` | ✅ COVERED |
| ROUT-03 | Cross-run budget awareness with no eviction of previously delivered alerts | `test_cross_run_ar_full`, `test_cross_run_info_full` | ✅ COVERED |
| ROUT-04 | Delivery budget resets at `America/New_York` midnight | `test_et_midnight_reset`, `test_count_delivered_today_filters_by_routing_status` | ✅ COVERED |
| ROUT-05 | Equal-score ties resolve deterministically by alphabetical ticker | `test_tiebreak_alphabetical` | ✅ COVERED |

**10 router tests | 101 total tests | 0 failures | 0 gaps**

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. Phase 5 validation lives in `tests/test_alert_router.py` and `tests/test_smoke.py`; no additional framework/bootstrap work is required.

---

## Manual-Only Verifications

All phase behaviors have automated verification.

---

## Validation Sign-Off

- [x] All tasks have automated verify or manual-only justification
- [x] Sampling continuity: every task has a verify command
- [x] Wave 0 covers all requirements (pre-existing infra)
- [x] No watch-mode flags
- [x] Feedback latency < 23s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-05-16
