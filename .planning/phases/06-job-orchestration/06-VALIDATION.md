---
phase: "06"
slug: job-orchestration
status: draft
nyquist_compliant: false
wave_0_complete: false
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

**Current baseline:** full suite passes with `101` tests before Phase 6 work begins.

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
| 06-01-01 | 01 | 0 | JOBS-01, JOBS-02 | T-06-01 | Shared helpers preserve heartbeat/run lifecycle and keep DB writes in `repository.py` | unit | `uv run pytest tests/test_job_orchestration.py -k "shared or dispatcher" -q` | ❌ W0 | ⬜ pending |
| 06-01-02 | 01 | 1 | JOBS-01, JOBS-03, JOBS-04 | T-06-02 | `news-morning` caps after dedup, writes overflow MONITORING rows, and always emits a digest | unit/integration | `uv run pytest tests/test_job_orchestration.py -k "news_morning or headline_cap or zero_alert" -q` | ❌ W0 | ⬜ pending |
| 06-02-01 | 02 | 2 | JOBS-02, JOBS-03 | T-06-03 | Discovery Phase A sends no email; Phase B persists routed signals and emits explicit zero-alert digest | unit/integration | `uv run pytest tests/test_job_orchestration.py -k "discovery_phase_a or discovery_phase_b" -q` | ❌ W0 | ⬜ pending |
| 06-03-01 | 03 | 3 | MEAS-01, MEAS-02 | T-06-04 | Outcome backfill stays deferred/idempotent and operator feedback fields remain available | unit | `uv run pytest tests/test_outcome_backfill.py -q` | ❌ W0 | ⬜ pending |
| 06-03-02 | 03 | 3 | OPS-01, OPS-02 | T-06-05 | Ops artifacts are scrubbed, non-secret, and document the required scheduler/filter/notification settings | source/docs | `uv run pytest -q` | ✅ existing suite | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_job_orchestration.py` — new focused coverage for dispatcher wiring, `news-morning`, `discovery`, digests, and routing persistence
- [ ] `tests/test_outcome_backfill.py` — idempotent deferred backfill coverage
- [ ] shared fixtures/mocks for repository, email, heartbeat, Finnhub news, and classifier outputs

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Task Scheduler XML imports and behaves correctly on Windows | OPS-01 | Current host is Darwin; Task Scheduler cannot be validated locally | Import the scrubbed XML on the target Windows host, confirm StartWhenAvailable, single-instance enforcement, ET trigger, and absolute paths |
| Gmail filter and Healthchecks notification setup | OPS-02 | External operator account configuration is outside automated test scope | Follow the setup guide in a real Gmail + Healthchecks account and confirm the filter and SMS/push notification settings |
| Live `thesis.yaml` + credentialed `news-morning` run | JOBS-01 | Local session does not validate live secrets or Windows scheduler environment | Create `thesis.yaml` from `thesis.example.yaml`, run `python -m signal_system news-morning`, and confirm heartbeat, DB rows, and digest delivery |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or explicit Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 25s
- [ ] `nyquist_compliant: true` set in frontmatter after implementation completes

**Approval:** pending
