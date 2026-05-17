---
phase: 06-job-orchestration
plan: 01
subsystem: jobs
tags: [jobs, news-morning, digest, router, sqlite, tests]
requires:
  - phase: 03-news-classifier
    provides: headline classification, parse-failure MONITORING fallback, dedup key semantics
  - phase: 04-discovery-agent
    provides: run count persistence pattern and signal contracts
  - phase: 05-alert-router
    provides: pure routing tuples with demotion metadata
provides:
  - core-holdings lookup for job orchestration
  - latest successful daily-close date lookup for previous-close anchoring
  - shared routed persistence and digest rendering helpers
  - news-morning job dispatcher wiring with digest completeness guardrails
affects: [phase-06-discovery, phase-06-ops, phase-06-measurement]
tech-stack:
  added: []
  patterns: [heartbeat-wrapped job lifecycle, digest completeness validation, dedup-before-cap overflow monitoring]
key-files:
  created: [src/signal_system/jobs/common.py, src/signal_system/jobs/news_morning.py, tests/test_job_orchestration.py]
  modified: [src/signal_system/data/universe.py, src/signal_system/state/repository.py, src/signal_system/__main__.py]
key-decisions:
  - "Anchor news-morning to the latest successful daily-close ET date at 4:00 PM instead of naive date subtraction."
  - "Validate digest counts and zero-alert confirmation before sending email so the run fails closed on mismatches."
  - "Deduplicate windowed headlines newest-first before the 50-headline cap; persist every overflow headline as its own MONITORING row."
patterns-established:
  - "Job helpers centralize routed tuple persistence and plain-text digest rendering for reuse by discovery."
  - "MONITORING classifier outputs bypass the router and are persisted directly by the job."
requirements-completed: [JOBS-01, JOBS-03, JOBS-04]
duration: 25min
completed: 2026-05-17
---

# Phase 6 Plan 01: Shared orchestration + news-morning Summary

**Core-holdings `news-morning` job with previous-close anchoring, digest guardrails, and overflow-to-MONITORING handling.**

## Performance

- **Duration:** 25 min
- **Started:** 2026-05-17T02:35:00Z
- **Completed:** 2026-05-17T03:00:02Z
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments
- Added core-holdings and latest-successful-run helpers needed for job orchestration.
- Added shared routed-persistence and digest-rendering helpers for job-level delivery behavior.
- Implemented `python -m signal_system news-morning` with fail-closed cold start, thesis gate, dedup-before-cap overflow handling, direct MONITORING persistence, and digest validation.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add core-holdings lookup and previous-close date helper** - `a3cf7e8`, `91d2839`
2. **Task 2: Create shared job helpers for digest rendering and routed persistence** - `6b5ae5a`, `c3a21d9`
3. **Task 3: Build `src/signal_system/jobs/news_morning.py` and register `news-morning`** - `9b24d04`, `a0fc363`

**Plan metadata:** current docs commit

## Files Created/Modified
- `src/signal_system/data/universe.py` - adds `get_core_holdings()` with preserved K-1 filtering and CSV-order semantics.
- `src/signal_system/state/repository.py` - adds `get_latest_successful_run_date()` for ET-date run anchoring.
- `src/signal_system/jobs/common.py` - adds routed tuple persistence, digest rendering, and digest validation helpers.
- `src/signal_system/jobs/news_morning.py` - wires the news job lifecycle, previous-close windowing, headline cap logic, direct MONITORING persistence, and digest send.
- `src/signal_system/__main__.py` - registers `news-morning` in the CLI dispatcher.
- `tests/test_job_orchestration.py` - adds focused orchestration coverage for helpers, cold start, thesis gate, cap overflow, digest counts, parse-failure persistence, and dispatcher wiring.

## Decisions Made
- Used the newest successful `daily-close` run's ET date as the canonical lower-bound anchor, then converted it to 4:00 PM ET inside `news_morning.py`.
- Enforced digest completeness in code by validating rendered counts and zero-alert language before sending email.
- Reused the classifier's dedup key semantics in `news_morning.py` so cap handling and classifier dedup stay aligned.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required for this plan slice.

## Next Phase Readiness

- Discovery can now reuse the shared digest and routed-persistence helpers added here.
- Phase 06 still needs discovery orchestration, deferred outcome backfill, and ops artifacts in later plans.

## Self-Check: PASSED
