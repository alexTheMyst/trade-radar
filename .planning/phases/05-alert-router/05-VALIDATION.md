---
phase: 5
slug: alert-router
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-16
---

# Phase 5 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (discovered via `uv run pytest`) |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/test_alert_router.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~30 seconds |

**Current state:** 87 tests collected. Phase 5 gate: 95+ tests pass (8+ new router tests added).

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_alert_router.py -x`
- **After every plan wave:** `uv run pytest`
- **Before `/gsd-verify-work`:** Full suite must be green (95+ tests)
- **Max feedback latency:** ~30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 05-00-01 | 01 | 0 | ROUT-02 | — | demoted_from column added idempotently | unit | `uv run pytest tests/test_smoke.py -x` | ✅ | ⬜ pending |
| 05-01-01 | 01 | 1 | ROUT-01 | — | route_signals importable, returns [] for empty | unit | `uv run python -c "from signal_system.router import route_signals; assert route_signals([])==[]"` | ❌ Wave 0 | ⬜ pending |
| 05-02-01 | 01 | 2 | ROUT-01 | — | AR cap=1 enforced intra-batch | unit | `uv run pytest tests/test_alert_router.py::test_ar_budget_one_winner -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-02 | 01 | 2 | ROUT-01 | — | Mixed batch 1 AR + 3 INFO DELIVERED | unit | `uv run pytest tests/test_alert_router.py::test_mixed_batch_allocation -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-03 | 01 | 2 | ROUT-02 | — | demoted_from reason codes correct | unit | `uv run pytest tests/test_alert_router.py::test_demoted_from_reason_codes -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-04 | 01 | 2 | ROUT-03 | — | Cross-run: DB full → budget_cap_ar | unit | `uv run pytest tests/test_alert_router.py::test_cross_run_ar_full -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-05 | 01 | 2 | ROUT-04 | — | ET midnight reset: signals on different ET dates see independent budgets | unit | `uv run pytest tests/test_alert_router.py::test_et_midnight_reset -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-06 | 01 | 2 | ROUT-05 | — | Equal scores → alphabetical tiebreak deterministic | unit | `uv run pytest tests/test_alert_router.py::test_tiebreak_alphabetical -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-07 | 01 | 2 | D-15 | ValueError guard | MONITORING input → ValueError | unit | `uv run pytest tests/test_alert_router.py::test_monitoring_raises -x` | ❌ Wave 0 | ⬜ pending |
| 05-02-08 | 01 | 2 | D-13 | — | Empty input → [] | unit | `uv run pytest tests/test_alert_router.py::test_empty_input -x` | ❌ Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_alert_router.py` — covers all ROUT-01..ROUT-05 requirements (≥8 tests)
- [ ] `src/signal_system/router/__init__.py` — package init exporting `route_signals`
- [ ] `src/signal_system/router/alert_router.py` — main implementation

*`tests/conftest.py` already has env var setup — no changes needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Schema `demoted_from` column visible in live DB | ROUT-02 | Schema inspect | `sqlite3 state/signals.db ".schema signals" \| grep demoted_from` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
