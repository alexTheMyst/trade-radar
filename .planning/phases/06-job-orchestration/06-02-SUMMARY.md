---
phase: 06-job-orchestration
plan: 02
subsystem: jobs
tags: [jobs, discovery, digest, router, sqlite, tests]
requires:
  - phase: 04-discovery-agent
    provides: phase-aware scoring contract and run-count updates
  - phase: 05-alert-router
    provides: pure routing tuples with demotion metadata
  - phase: 06-job-orchestration
    provides: shared digest rendering and routed persistence helpers from Plan 01
provides:
  - runnable discovery job wiring in the CLI dispatcher
  - Phase A discovery execution that stops after scoring with no email or router calls
  - Phase B discovery routing, persistence, and digest completeness validation
affects: [phase-06-measurement, phase-06-ops, phase-06-closeout]
tech-stack:
  added: []
  patterns: [heartbeat-wrapped discovery lifecycle, phase-driven branching, digest completeness validation]
key-files:
  created: [src/signal_system/jobs/discovery.py]
  modified: [src/signal_system/__main__.py, tests/test_job_orchestration.py]
key-decisions:
  - "Branch discovery on config.DISCOVERY_PHASE instead of the returned signal list so Phase A and zero-alert Phase B behave correctly."
  - "Reuse the shared digest renderer and validate its counts against persisted routing outcomes before any Phase B email send."
patterns-established:
  - "Discovery Phase A ends immediately after score_universe() while still marking the run successful inside heartbeat."
  - "Discovery Phase B always sends one digest, including explicit zero-alert confirmation when nothing is delivered."
requirements-completed: [JOBS-02, JOBS-03]
duration: 8min
completed: 2026-05-17
---

# Phase 6 Plan 02: discovery Summary

**Config-driven discovery orchestration with Phase A inbox silence and Phase B routed digest validation.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-05-17T03:00:00Z
- **Completed:** 2026-05-17T03:08:00Z
- **Tasks:** 1
- **Files modified:** 3

## Accomplishments
- Added a runnable `python -m signal_system discovery` job.
- Preserved Discovery Phase A as score-and-log only with no router or email path.
- Added Phase B routing, persistence, zero-alert digest delivery, and digest mismatch failure coverage.

## Task Commits

Each task was committed atomically:

1. **Task 1: Build `src/signal_system/jobs/discovery.py`** - `e6cd26a` (test), `0348ad1` (feat)

**Plan metadata:** current docs commit

## Files Created/Modified
- `src/signal_system/jobs/discovery.py` - orchestrates discovery runs, phase branching, persistence, and digest sending.
- `src/signal_system/__main__.py` - registers the `discovery` job in the CLI dispatcher.
- `tests/test_job_orchestration.py` - covers Phase A silence, Phase B routing/digests, mismatch failure, and dispatcher wiring.

## Decisions Made
- Discovery branches on `config.DISCOVERY_PHASE`, not on the `score_universe()` return value.
- Phase B validates digest counts against persisted routing results before sending the email.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required for this plan slice.

## Next Phase Readiness

- Deferred outcome backfill and ops artifacts can build on the now-runnable discovery dispatcher.
- Closeout work can rely on focused discovery orchestration tests for Phase 6 verification evidence.

## Self-Check: PASSED
