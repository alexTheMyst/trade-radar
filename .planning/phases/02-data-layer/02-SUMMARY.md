---
phase: 02-data-layer
plan: "01"
subsystem: data
tags: [finnhub, tenacity, rate-limiting, token-bucket, retry, news]

requires:
  - phase: 01-foundation
    provides: Signal dataclass, thesis loader, universe partitioning, schema extensions

provides:
  - Token bucket rate limiter (_acquire_slot) enforcing ≤55 Finnhub calls/min
  - tenacity-based retry decorator for 429 with exponential backoff (max 5 attempts)
  - Paid-tier graceful degradation: 403/404 → return None/[] without retry
  - fetch_quotes(tickers) — bulk quote fetch, returns dict[str, dict | None]
  - fetch_company_news(ticker, from_date, to_date) — news headlines with ≥headline+source
  - fetch_spy_close() wired through _acquire_slot (counts against rate budget)
  - PAID_TIER_STATUS_CODES frozenset({403, 404}) as public constant

affects: [03-news-classifier, 04-discovery-agent]

tech-stack:
  added: [tenacity==9.1.4]
  patterns:
    - Module-level token bucket with threading.Lock for sequential rate limiting
    - tenacity retry_if_exception discriminator (429 retry, 403/404 skip)
    - Paid-tier graceful degradation pattern (log warning, return None/[])

key-files:
  created: []
  modified:
    - src/signal_system/data/finnhub_client.py
    - tests/test_smoke.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "_is_transient_error discriminates 429 (retry) from 403/404 (skip) — critical correctness guarantee"
  - "_acquire_slot uses threading.Lock for safe sequential access; no asyncio"
  - "fetch_spy_close wired through _acquire_slot to count against rate budget"
  - "fetch_company_news exhausted-retry path returns [] not None — consistent with empty-news contract"
  - "PAID_TIER_STATUS_CODES is public frozenset — Discovery Agent (Phase 4) may reference it directly"

patterns-established:
  - "_RETRY_DECORATOR pattern: module-level tenacity decorator, applied with @decorator syntax"
  - "Paid-tier error pattern: catch FinnhubAPIException, check status_code in PAID_TIER_STATUS_CODES, log warning, return None/[]"
  - "Rate-limit pattern: _acquire_slot() as first line inside each SDK-calling function"

requirements-completed: [DATA-01, DATA-02, DATA-03, DATA-04]

duration: 35min
completed: 2026-05-15
---

# Phase 2: Data Layer Summary

**Finnhub client extended with stdlib token bucket (≤55 calls/min), tenacity 429 retry (5 attempts, exponential backoff), paid-tier 403/404 graceful degradation, bulk quote fetch, and company news fetch — all tested TDD-style with 11 new mocked tests**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-05-15T18:30:00Z
- **Completed:** 2026-05-15T19:05:00Z
- **Tasks:** 6 (T1–T6)
- **Files modified:** 4

## Accomplishments

- Implemented `_acquire_slot()` token bucket with `threading.Lock` — enforces 60/55 ≈ 1.09s minimum interval between Finnhub calls
- Implemented `_is_transient_error` discriminator — 429 retried up to 5 times with `wait_exponential(min=2, max=60)`; 403/404 immediately returns None/[] without retry
- `fetch_quotes(tickers)` bulk loop — calls `_fetch_single_quote` per ticker, catches exhausted retries, returns `dict[str, dict | None]` never raises
- `fetch_company_news(ticker, from_date, to_date)` — date objects converted to YYYY-MM-DD strings; paid-tier and retry exhaustion both return `[]`
- `fetch_spy_close()` refactored to call `_acquire_slot()` first (counts against rate budget)
- 11 new tests (TDD RED→GREEN): token bucket, fetch_quotes, 429 retry, 403/404 paid-tier, company news variants, integration import check. Total: 28 tests passing

## Task Commits

1. **T1: Pin tenacity dependency** — `56640bb` (chore)
2. **T2: RED — token bucket, fetch_quotes, 429 retry** — `943ab07` (test)
3. **T3: Implement token bucket, retry decorator, fetch_quotes** — `d8db50c` (feat)
4. **T4: RED — 404 paid-tier, fetch_company_news** — `2b923ab` (test)
5. **T5: Implement fetch_company_news** — `eca7a2f` (feat)
6. **T6: Phase integration smoke test** — `d24aa9b` (test)

## Files Created/Modified

- `src/signal_system/data/finnhub_client.py` — Fully rewritten: token bucket, retry decorator, _fetch_single_quote, fetch_quotes, fetch_company_news, refactored fetch_spy_close
- `tests/test_smoke.py` — 11 new Phase 2 tests added (TDD RED→GREEN pattern)
- `pyproject.toml` — tenacity==9.1.4 added as dependency
- `uv.lock` — updated with tenacity resolution

## Decisions Made

- **threading.Lock over asyncio**: per CLAUDE.md — Windows event-loop quirks, sequential jobs don't need concurrency
- **_is_transient_error as separate function**: tenacity `retry_if_exception` requires a callable; makes the discriminator explicitly testable and auditable
- **fetch_company_news returns []**: consistent "empty means no news" contract regardless of failure reason — callers don't need to distinguish None vs []
- **PAID_TIER_STATUS_CODES as public frozenset**: Phase 4 Discovery Agent may need to reference it; keeping it internal would force duplication

## Deviations from Plan

**1. T1 PyPI verification assertion adapted**
- **Found during:** T1 (tenacity legitimacy check)
- **Issue:** The plan's curl script asserted `project_urls["Source"]` but tenacity's PyPI metadata uses `project_urls["Homepage"]`. The assertion caused a false failure.
- **Fix:** Adapted assertion to check Homepage key (still verifies `jd/tenacity` GitHub URL)
- **Files modified:** None (command-only fix)
- **Committed in:** `56640bb`

---

**Total deviations:** 1 auto-fixed (PyPI assertion key mismatch — non-security, non-scope)
**Impact on plan:** No scope change. The tenacity package identity was verified correctly via the adapted check.

## Issues Encountered

None beyond the PyPI assertion key mismatch documented above.

## Self-Check

- [x] 28 tests passing (> 17 Phase 1 baseline)
- [x] `_acquire_slot` appears ≥3 times (definition + _fetch_single_quote + fetch_spy_close)
- [x] `status_code == 429` appears ≥1 time
- [x] `PAID_TIER_STATUS_CODES` appears ≥2 times (definition + usage)
- [x] 0 asyncio, 0 httpx, 0 pyrate-limiter imports
- [x] PAID_TIER_STATUS_CODES contains both 403 and 404
- [x] All 4 Phase 2 public names importable from finnhub_client

## Next Phase Readiness

- **Phase 3 (News Classifier)**: Can import `fetch_company_news` — delivers rate-limited, retried, sanitization-ready news headlines. Sanitization (strip control chars, cap at 500 chars, `<headline>` delimiters) is Phase 3's responsibility (CLFY-01).
- **Phase 4 (Discovery Agent)**: Can import `fetch_quotes` — delivers bulk quote data with graceful None for paid-tier/missing endpoints. Discovery Agent must enforce no-score-on-None per DISC-02.
- **Risk register**: R-02-A1 through R-02-A5 must be validated on first live API run. All handled gracefully in code; empirical validation deferred to Phase 3/4 first runs.

---
*Phase: 02-data-layer*
*Completed: 2026-05-15*
