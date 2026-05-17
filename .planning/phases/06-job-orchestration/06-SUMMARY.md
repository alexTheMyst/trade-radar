---
phase: 06-job-orchestration
plan: "01-04"
subsystem: orchestration
tags: [jobs, digest, discovery, news-morning, measurement, ops, verification]
requires:
  - phase: 03-news-classifier
    provides: thesis-driven classification with parse-failure MONITORING fallback
  - phase: 04-discovery-agent
    provides: phase-aware discovery scoring and run-count logging
  - phase: 05-alert-router
    provides: pure routing tuples with deterministic delivery budgets
provides:
  - runnable `news-morning` and `discovery` jobs wired through heartbeat, repository persistence, and digest delivery
  - zero-alert digest confirmation and routed-delivery persistence guardrails
  - deferred internal-only outcome backfill plus operator feedback workflow handoff
  - Windows Task Scheduler, Gmail filter, and Healthchecks operator setup artifacts
affects: [milestone-v1.0-audit, operator-handoff, manual-go-live-validation]
tech-stack:
  added: []
  patterns:
    - heartbeat-wrapped job orchestration for every runnable batch job
    - digest-first delivery with explicit zero-alert confirmation
    - manual evidence tracked separately from automated verification for Windows/email/live-secret steps
key-files:
  created:
    - src/signal_system/jobs/common.py
    - src/signal_system/jobs/news_morning.py
    - src/signal_system/jobs/discovery.py
    - src/signal_system/jobs/outcome_backfill.py
    - ops/task-scheduler-reference.xml
    - ops/windows-task-scheduler.md
    - ops/operator-setup-checklist.md
  modified:
    - src/signal_system/__main__.py
    - src/signal_system/data/universe.py
    - src/signal_system/state/repository.py
    - tests/test_job_orchestration.py
    - tests/test_outcome_backfill.py
key-decisions:
  - "Anchor `news-morning` to the latest successful `daily-close` ET date at 4:00 PM and fail closed on cold start."
  - "Send one digest per live job run, always including zero-alert confirmation when nothing is delivered."
  - "Keep MEAS-02 internal-only and defer any scheduler activation until roughly 30 days post-go-live."
  - "Treat Windows Task Scheduler import, Gmail filter + Healthchecks setup, and a credentialed `news-morning` run as manual evidence requirements, not automated verification."
patterns-established:
  - "Phase 6 jobs persist MONITORING outputs directly and persist routed tuples through shared helpers before digest send."
  - "Operator handoff docs mirror the XML/task checklist and preserve the 7-day feedback workflow around `acted`, `acted_at`, and `user_note`."
requirements-completed: [JOBS-01, JOBS-02, JOBS-03, JOBS-04, MEAS-01, MEAS-02, OPS-01, OPS-02]
duration: multi-plan
completed: 2026-05-17
---

# Phase 6: Job Orchestration Summary

**Runnable `news-morning` and `discovery` jobs with digest guardrails, deferred measurement backfill, and Windows/operator go-live handoff artifacts.**

## What shipped

- `python -m signal_system news-morning` now runs end-to-end inside heartbeat: core-holdings news fetch, thesis load, classification, routing, SQLite persistence, and one digest email.
- `python -m signal_system discovery` now runs end-to-end inside heartbeat: universe load, discovery scoring, Phase A no-email behavior, Phase B routing/persistence, and one digest email.
- Both live jobs enforce explicit zero-alert confirmation instead of silence.
- Phase 6 added internal-only deferred outcome backfill code plus repository helpers for 30d/90d measurement fields.
- Phase 6 shipped the operator handoff artifacts: scrubbed Task Scheduler XML, Windows setup guide, and Gmail/Healthchecks/operator checklist.

## User-visible behavior

### `news-morning`
- Requires one prior successful `daily-close` run before scanning news.
- Uses the previous successful `daily-close` ET date as the prior-close anchor.
- Scans core holdings only.
- Deduplicates headlines newest-first before applying the 50-headline cap.
- Persists overflow headlines and classifier parse failures as `MONITORING`.
- Sends a single digest even when delivered alerts = 0.

### `discovery`
- Uses the existing discovery agent scoring flow inside a heartbeat-wrapped job.
- Branches on `DISCOVERY_PHASE`, not on returned signal count.
- Phase A logs only and sends no email.
- Phase B routes signals, persists routing outcomes, and still sends a zero-alert digest when nothing is delivered.

### Measurement + ops handoff
- Operator feedback fields `acted`, `acted_at`, and `user_note` remain part of the live workflow and are expected within 7 days of alert.
- Outcome backfill code exists but remains intentionally unscheduled until roughly 30 days post-go-live.
- Windows scheduling guidance standardizes on absolute-path `uv run python -m signal_system <job>` commands with StartWhenAvailable, IgnoreNew single-instance behavior, Eastern Time triggers, and password-backed logon.

## Evidence from executed plans

- **Plan 06-01:** Shared orchestration helpers + `news-morning` runtime (`a0fc363`, docs `ba4aea2`)
- **Plan 06-02:** `discovery` runtime wiring (`0348ad1`, docs `8f3b818`)
- **Plan 06-03:** deferred outcome backfill + ops artifacts (`accefae`, `a9b0dd5`, docs `f36ed4e`)

## Automated verification basis

- `uv run pytest -q` passes with **120 passing tests** after Phase 6 implementation.
- `tests/test_job_orchestration.py` covers:
  - previous-`daily-close` fail-closed behavior
  - thesis gate before digest send
  - core-holdings-only `news-morning`
  - dedup-before-cap overflow persistence
  - parse-failure `MONITORING` persistence
  - discovery Phase A/Phase B branching
  - zero-alert digests and digest mismatch guardrails
  - CLI dispatcher wiring
- `tests/test_outcome_backfill.py` covers due-threshold gating, internal-only exposure, and idempotent non-overwrite behavior.

## Manual follow-up required before go-live

These items are intentionally **manual evidence requirements**, not automated proof:

1. Import or recreate the Task Scheduler task on the target Windows host and confirm StartWhenAvailable, single-instance IgnoreNew behavior, ET trigger intent, absolute paths, and password-backed logon.
2. Configure the Gmail filter (`from:GMAIL_USERNAME` → never send to spam) and Healthchecks SMS/push notifications in the real operator accounts.
3. Run one live credentialed `news-morning` execution with `thesis.yaml`, API keys, Gmail SMTP, and Healthchecks configured; confirm heartbeat pings, DB rows, and digest delivery.
4. Keep the 7-day operator feedback workflow active for every alert by filling `acted`, `acted_at`, and `user_note`.

## Phase readiness

- Automated implementation work for JOBS-01..OPS-02 is complete.
- Manual go-live evidence is still required on the real Windows/operator environment.
