---
phase: "04"
slug: discovery-agent
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-16
---

# Phase 04 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 (via uv) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_discovery_agent.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~23 seconds |

**Current state:** 21 Discovery Agent tests pass in `tests/test_discovery_agent.py`; full repository suite is 99 passing tests.

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_discovery_agent.py -q`
- **After every plan wave:** Run `uv run pytest -q`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** ~23 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 04-T1 | 01 | 0 | DISC-02 | Ticker/API input validation | Invalid quotes are excluded; flat-day quotes do not crash scoring | unit | `uv run pytest tests/test_discovery_agent.py -k "score_floor or range_position_flat_day or news_activity" -q` | ✅ | ✅ green |
| 04-T2 | 01 | 0 | DISC-05 | SQL parameterization on `update_run_counts` | `runs` rows record `tickers_scanned` and `tickers_signaled` correctly for each invocation | unit | `uv run pytest tests/test_discovery_agent.py -k "update_run_counts or signal_price_snapshot" -q` | ✅ | ✅ green |
| 04-T3 | 01 | 1 | DISC-01, DISC-03, DISC-04 | NaN/division guard; hardcoded `MONITORING` | Weighted scoring, thresholds, Phase A/B branching, body prefix, and public exports match the phase contract | unit | `uv run pytest tests/test_discovery_agent.py -k "score_computation or phase_a_inserts_monitoring or phase_b_returns_signals or body_prefix or public_surface_smoke" -q` | ✅ | ✅ green |
| 04-T4 | 01 | 1 | DISC-04 | No delivery/router coupling | Discovery agent import/execution stays isolated from email delivery and router modules | subprocess integration | `uv run pytest tests/test_discovery_agent.py -k "isolated_from_delivery_and_router" -q` | ✅ | ✅ green |
| 04-T5 | 01 | 2 | DISC-01..DISC-05 | all | Full discovery validation plus regression coverage for the existing repo | integration | `uv run pytest tests/test_discovery_agent.py -q && uv run pytest -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Coverage Summary

| Requirement | Description | Tests | Status |
|-------------|-------------|-------|--------|
| DISC-01 | 4-factor cross-sectional scoring at 35/30/25/10 using `/quote` + `/company-news`; exported public surface and `_rank_values` helper match the contract | `test_score_computation`, `test_action_required_severity`, `test_informational_severity`, `test_cross_sectional_ranking_ties`, `test_single_ticker_universe`, `test_public_surface_smoke` | ✅ COVERED |
| DISC-02 | Score-floor guard excludes invalid quotes, treats missing news as zero, and allows `h == l` flat-day quotes without crashing | `test_score_floor_invalid_quote`, `test_score_floor_null_quote`, `test_range_position_flat_day`, `test_news_activity_missing`, `test_news_activity_empty` | ✅ COVERED |
| DISC-03 | `DISCOVERY_PHASE` alone controls calibration-vs-live behavior: Phase A inserts `MONITORING` and returns `[]`; Phase B returns `list[Signal]` and does not insert | `test_phase_a_inserts_monitoring`, `test_phase_b_returns_signals` | ✅ COVERED |
| DISC-04 | Emitted signals expose `sub_scores`, `signal_price_snapshot`, and documented body prefix; discovery agent stays isolated from email delivery and router coupling | `test_sub_scores_dict`, `test_signal_price_snapshot`, `test_signal_body_prefix`, `test_discovery_agent_isolated_from_delivery_and_router` | ✅ COVERED |
| DISC-05 | Each scoring run updates `tickers_scanned` and `tickers_signaled` in the `runs` table | `test_update_run_counts` | ✅ COVERED |

**21 discovery tests | 99 total tests | 0 failures | 0 gaps**

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. Discovery validation now lives in `tests/test_discovery_agent.py`; no additional framework/bootstrap work is required.

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
