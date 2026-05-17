---
phase: "06"
slug: job-orchestration
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-16
---

# Phase 06 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 (via `uv`) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_job_orchestration.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~25 seconds |

**Current baseline:** full suite passes with `120` tests after Phase 6 implementation and closeout.

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_job_orchestration.py -q`
- **After every plan wave:** Run `uv run pytest -q`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** ~25 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 06-01-01 | 01 | 0 | JOBS-01 | T-06-01 | Core-holdings lookup and previous-close date helper preserve the ET market-close boundary and fail-closed cold-start rules | unit | `uv run pytest tests/test_job_orchestration.py -k "core_holdings or latest_successful_run_date" -q` | ✅ | ✅ green |
| 06-01-02 | 01 | 0 | JOBS-01, JOBS-03 | T-06-02 | Shared digest helpers preserve routed tuple persistence, count-only non-delivered sections, and explicit zero-alert confirmation | unit | `uv run pytest tests/test_job_orchestration.py -k "shared_digest or routed_persistence" -q` | ✅ | ✅ green |
| 06-01-03 | 01 | 1 | JOBS-01, JOBS-03, JOBS-04 | T-06-03 | `news-morning` preserves thesis fail-closed behavior, core-holdings-only scope, parse-failure `MONITORING`, dedup-before-cap, overflow rows, digest count consistency, and fail-on-mismatch guardrails | unit/integration | `uv run pytest tests/test_job_orchestration.py -k "news_morning or core_holdings or headline_cap or zero_alert or dispatcher or thesis or parse_failure or digest_counts or digest_mismatch" -q` | ✅ | ✅ green |
| 06-02-01 | 02 | 2 | JOBS-02, JOBS-03 | T-06-04 | Discovery Phase A sends no email; Phase B persists routed signals, enforces digest count consistency, and emits explicit zero-alert digest | unit/integration | `uv run pytest tests/test_job_orchestration.py -k "discovery_phase_a or discovery_phase_b or dispatcher or digest_counts or digest_mismatch" -q` | ✅ | ✅ green |
| 06-03-01 | 03 | 3 | MEAS-02 | T-06-05 | Outcome backfill stays deferred, internal-only, and idempotent | unit | `uv run pytest tests/test_outcome_backfill.py -q` | ✅ | ✅ green |
| 06-03-02 | 03 | 3 | MEAS-01, OPS-01, OPS-02 | T-06-06 | Ops artifacts are scrubbed, non-secret, document the required scheduler/filter/notification settings, and preserve the 7-day operator feedback workflow | source/docs | `test -f ops/task-scheduler-reference.xml && test -f ops/windows-task-scheduler.md && test -f ops/operator-setup-checklist.md && rg -n "uv run python -m signal_system|StartWhenAvailable|single-instance|absolute path|Eastern Time|run whether logged on or not|password-backed" ops/task-scheduler-reference.xml ops/windows-task-scheduler.md && rg -n "Gmail filter|Healthchecks|30 days post-go-live|7 days|acted|acted_at|user_note" ops/windows-task-scheduler.md ops/operator-setup-checklist.md` | ✅ | ✅ green |
| 06-04-01 | 04 | 4 | JOBS-01, JOBS-02, JOBS-03, JOBS-04 | T-06-07 | Summary/UAT closeout captures the built operator workflow and completes Phase 6 UAT | source/docs | `test -f .planning/phases/06-job-orchestration/06-SUMMARY.md && rg -n "^status: complete$" .planning/phases/06-job-orchestration/06-UAT.md && rg -n "^source: 06-SUMMARY.md$" .planning/phases/06-job-orchestration/06-UAT.md` | ✅ | ✅ green |
| 06-04-02 | 04 | 4 | MEAS-01, OPS-01, OPS-02 | T-06-08 | Phase 6 verification records the manual-only checks plus the 7-day operator feedback workflow, and closeout updates both validation flags | source/docs | `test -f .planning/phases/04-discovery-agent/04-VERIFICATION.md && test -f .planning/phases/05-alert-router/05-VERIFICATION.md && test -f .planning/phases/06-job-orchestration/06-VERIFICATION.md && rg -n "^nyquist_compliant: true$" .planning/phases/06-job-orchestration/06-VALIDATION.md && rg -n "^wave_0_complete: true$" .planning/phases/06-job-orchestration/06-VALIDATION.md && rg -n "Task Scheduler|Gmail filter|Healthchecks|news-morning|acted|acted_at|user_note|7 days" .planning/phases/06-job-orchestration/06-VERIFICATION.md` | ✅ | ✅ green |
| 06-04-03 | 04 | 4 | JOBS-01, JOBS-02, JOBS-03, JOBS-04, MEAS-01, MEAS-02, OPS-01, OPS-02 | T-06-09 | Traceability and milestone audit reflect the verified Phase 6 state instead of stale blockers | source/docs | `test -f .planning/v1.0-v1.0-MILESTONE-AUDIT.md && ! rg -n "JOBS-01.*Pending|JOBS-02.*Pending|JOBS-03.*Pending|JOBS-04.*Pending|MEAS-01.*Pending|MEAS-02.*Pending|OPS-01.*Pending|OPS-02.*Pending" .planning/REQUIREMENTS.md && ! rg -n "missing 04-VERIFICATION|missing 05-VERIFICATION|missing build artifacts|No `\\*-SUMMARY\\.md`" .planning/v1.0-v1.0-MILESTONE-AUDIT.md` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_job_orchestration.py` — focused coverage for dispatcher wiring, core-holdings-only `news-morning`, parse-failure persistence, `discovery`, digests, and routing persistence
- [x] `tests/test_outcome_backfill.py` — idempotent deferred backfill coverage
- [x] shared fixtures/mocks for repository, email, heartbeat, Finnhub news, and classifier outputs

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Task Scheduler XML imports and behaves correctly on Windows | OPS-01 | Current host is Darwin; Task Scheduler cannot be validated locally | Import the scrubbed XML on the target Windows host, confirm StartWhenAvailable, single-instance enforcement, ET trigger, and absolute paths |
| Gmail filter and Healthchecks notification setup | OPS-02 | External operator account configuration is outside automated test scope | Follow the setup guide in a real Gmail + Healthchecks account and confirm the filter and SMS/push notification settings |
| Live `thesis.yaml` + credentialed `news-morning` run | JOBS-01 | Local session does not validate live secrets or Windows scheduler environment | Create `thesis.yaml` from `thesis.example.yaml`, run `python -m signal_system news-morning`, and confirm heartbeat, DB rows, digest delivery, and direct persistence of any parse-failure `MONITORING` recovery output |
| Operator feedback workflow remains active within 7 days of alert | MEAS-01 | Manual workflow timing cannot be proven by unit tests alone | Use the checklist and verification docs to confirm `acted`, `acted_at`, and `user_note` are still part of the operator process and are expected within 7 days of alert |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or explicit Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 25s
- [x] `nyquist_compliant: true` set in frontmatter after implementation completes

**Approval:** approved 2026-05-17

---

## Validation Audit 2026-05-17

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

Full suite: 120/120 passed. All 9 per-task verification commands (unit, integration, source/docs) confirmed green. `nyquist_compliant: true` unchanged.
