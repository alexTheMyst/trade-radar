---
phase: 06-job-orchestration
plan: 03
subsystem: ops
tags: [measurement, backfill, scheduler, windows, checklist, testing]
requires:
  - phase: 06-job-orchestration
    provides: runnable news-morning/discovery jobs and shared digest helpers from Plans 01-02
provides:
  - internal-only deferred outcome backfill logic for 30d/90d measurement fields
  - repository helpers for selecting and updating eligible outcome rows idempotently
  - scrubbed Windows Task Scheduler reference and operator setup handoff docs
affects: [phase-06-closeout, phase-06-measurement, phase-06-ops]
tech-stack:
  added: []
  patterns: [internal-only backfill helper, idempotent outcome persistence, checklist-first Windows ops handoff]
key-files:
  created: [src/signal_system/jobs/outcome_backfill.py, tests/test_outcome_backfill.py, ops/task-scheduler-reference.xml, ops/windows-task-scheduler.md, ops/operator-setup-checklist.md]
  modified: [src/signal_system/state/repository.py]
key-decisions:
  - "Keep MEAS-02 as importable internal code only; do not register a public CLI job before the deferred post-go-live activation window."
  - "Use current quote snapshots to fill due 30d/90d outcome fields while preserving idempotent non-overwrite semantics in repository writes."
  - "Standardize Windows scheduling guidance on absolute-path `uv run python -m signal_system <job>` commands with StartWhenAvailable, IgnoreNew single-instance policy, and password-backed logon."
patterns-established:
  - "Outcome backfill eligibility is repository-driven (`acted IS NOT NULL` plus missing outcome columns) while threshold checks stay in job code."
  - "Operator handoff docs mirror the XML artifact and checklist the Gmail, Healthchecks, MEAS-02 deferment, and 7-day feedback workflow."
requirements-completed: [MEAS-01, MEAS-02, OPS-01, OPS-02]
duration: 4min
completed: 2026-05-17
---

# Phase 6 Plan 03: Measurement + ops handoff Summary

**Internal deferred outcome backfill plus scrubbed Windows scheduling and operator workflow handoff for post-go-live measurement.**

## Performance

- **Duration:** 4 min
- **Started:** 2026-05-17T04:05:54Z
- **Completed:** 2026-05-17T04:08:32Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Added an internal-only outcome backfill helper that fills due 30d/90d prices without exposing a new CLI job.
- Added repository helpers and focused tests covering deferred activation and idempotent non-overwriting writes.
- Shipped scrubbed Task Scheduler, Windows setup, and operator checklist artifacts covering ET triggers, absolute paths, Gmail filtering, Healthchecks alerts, and the 7-day feedback workflow.

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement deferred internal outcome backfill** - `40b2ece` (test), `accefae` (feat)
2. **Task 2: Create scrubbed ops artifacts and operator checklist** - `a9b0dd5` (docs)

**Plan metadata:** current docs commit

## Files Created/Modified
- `src/signal_system/jobs/outcome_backfill.py` - internal helper that fills due 30d/90d outcome fields from quote data.
- `src/signal_system/state/repository.py` - adds candidate selection and idempotent outcome update helpers.
- `tests/test_outcome_backfill.py` - covers threshold gating, internal-only exposure, and idempotence.
- `ops/task-scheduler-reference.xml` - scrubbed Windows Task Scheduler reference using `uv run python -m signal_system <job>`.
- `ops/windows-task-scheduler.md` - Windows setup guide for ET triggers, StartWhenAvailable, absolute paths, single-instance enforcement, and password-backed logon.
- `ops/operator-setup-checklist.md` - checklist for Gmail filter, Healthchecks non-email alerts, manual `daily-close`, deferred MEAS-02 activation, and 7-day operator feedback.

## Decisions Made
- Keep backfill importable but internal-only until the deferred activation window after go-live.
- Fill due outcomes without overwriting any existing 30d/90d measurement values on reruns.
- Document Task Scheduler around absolute `uv` paths, Eastern Time, StartWhenAvailable, IgnoreNew single-instance behavior, and password-backed logon.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required for this plan slice beyond the documented operator setup steps.

## Next Phase Readiness

- Audit-closeout work can cite the new backfill tests and ops artifacts as Phase 6 evidence.
- MEAS-02 remains intentionally inactive until roughly 30 days post-go-live.

## Self-Check: PASSED
