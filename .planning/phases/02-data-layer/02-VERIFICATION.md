---
phase: 02-data-layer
verified: 2026-05-16T06:18:00-06:00
status: passed
score: 4/4
overrides_applied: 0
re_verification: false
---

# Phase 2: Data Layer — Verification Report

**Phase Goal:** Extend the Finnhub client with a stdlib token bucket (≤55 calls/min), tenacity-based 429 retry with exponential backoff, paid-tier 403/404 graceful degradation, bulk quote fetch, and company news fetch — all wired through the rate limiter and tested TDD-style.

**Verified:** 2026-05-16T06:18:00-06:00
**Status:** PASSED
**Re-verification:** No — initial verification
**Test suite:** 28/28 tests pass (`uv run pytest tests/ -x -q`)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `_acquire_slot()` token bucket enforces ≤55 Finnhub calls/min via `threading.Lock` | VERIFIED | `test_token_bucket_calls_sleep` asserts `time.sleep` called with correct interval; `threading.Lock` used (not asyncio) per CLAUDE.md |
| 2 | 429 responses retried up to 5 attempts with exponential backoff via tenacity | VERIFIED | `test_retry_on_429` asserts `quote` called 5× on exhaustion; `_is_transient_error` discriminator routes 429 to retry |
| 3 | 403/404 responses return `None`/`[]` immediately without retry; warning logged | VERIFIED | `test_no_retry_on_403`, `test_paid_tier_404_returns_none`, `test_company_news_returns_empty_on_paid_tier`; `PAID_TIER_STATUS_CODES = frozenset({403, 404})` as public constant |
| 4 | `fetch_company_news()` returns headlines + source as list; returns `[]` on empty or paid-tier failure | VERIFIED | `test_company_news_returns_list`, `test_company_news_empty_on_no_results`, `test_company_news_passes_dates_as_strings` |

**Score:** 4/4 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/signal_system/data/finnhub_client.py` | Token bucket, retry decorator, `fetch_quotes`, `fetch_company_news`, `fetch_spy_close` rate-wired | VERIFIED | Fully rewritten; `_acquire_slot` called as first line in all SDK-calling functions |
| `tests/test_smoke.py` | 11 new Phase 2 tests (TDD RED→GREEN) | VERIFIED | 28 total tests passing (+11 from Phase 1 baseline of 17) |
| `pyproject.toml` | `tenacity==9.1.4` added | VERIFIED | Committed in `56640bb` |
| `uv.lock` | Resolved tenacity dependency | VERIFIED | Updated alongside pyproject.toml |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `fetch_quotes(tickers)` | `_fetch_single_quote` per ticker | `_acquire_slot()` rate gate then SDK call | WIRED | Returns `dict[str, dict | None]`; never raises |
| `fetch_company_news` | Finnhub SDK | `_acquire_slot()` + tenacity retry + paid-tier check | WIRED | Date objects converted to YYYY-MM-DD strings before SDK call |
| `fetch_spy_close()` | Finnhub SDK | `_acquire_slot()` (counts against rate budget) | WIRED | Refactored to call `_acquire_slot()` first in phase execution |
| `_is_transient_error` | tenacity `retry_if_exception` | Callable discriminator | WIRED | 429 → True (retry); 403/404 → False (skip); anything else → reraise |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| 28 tests pass end-to-end | `uv run pytest tests/ -x -q` | `28 passed` | PASS |
| Token bucket rate-limits calls | `test_token_bucket_calls_sleep` | `time.sleep` called with correct interval | PASS |
| 429 retries 5× then exhausts | `test_retry_on_429` | SDK called exactly 5 times | PASS |
| 403 returns None immediately | `test_no_retry_on_403` | None returned; no retry | PASS |
| 404 returns None immediately | `test_paid_tier_404_returns_none` | None returned; no retry | PASS |
| Company news 403 returns [] | `test_company_news_returns_empty_on_paid_tier` | [] returned without retry | PASS |
| No asyncio, httpx, pyrate-limiter | grep check | 0 imports of forbidden libs | PASS |
| PAID_TIER_STATUS_CODES public | import check | `frozenset({403, 404})` importable | PASS |

---

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|----------|
| DATA-01 | Bulk quote fetch with preemptive token bucket (≤55 calls/min) | SATISFIED | `_acquire_slot()` with `threading.Lock`; `test_token_bucket_calls_sleep`, `test_fetch_quotes_returns_dict`, `test_fetch_quotes_none_on_zero_price` |
| DATA-02 | Retry 429 via tenacity up to 5 attempts, exponential backoff | SATISFIED | `_RETRY_DECORATOR` with `wait_exponential(min=2, max=60)`; `test_retry_on_429` asserts 5 calls |
| DATA-03 | 403/404 → log warning, return None/[], caller skips | SATISFIED | `PAID_TIER_STATUS_CODES = frozenset({403, 404})`; `test_no_retry_on_403`, `test_paid_tier_404_returns_none`, `test_company_news_returns_empty_on_paid_tier` |
| DATA-04 | fetch_company_news: headlines + source, returns [] on empty or 403/404 | SATISFIED | `test_company_news_returns_list`, `test_company_news_empty_on_no_results`, `test_company_news_passes_dates_as_strings` |

---

### Deviations Noted

| Deviation | Impact | Resolution |
|-----------|--------|------------|
| PyPI assertion key mismatch (T1): tenacity uses `Homepage` not `Source` in project_urls | None — package identity still verified via `jd/tenacity` GitHub URL | Auto-fixed during T1; committed in `56640bb` |

---

### Human Verification Required

One manual gate deferred — live API behavior (rate limit enforcement, actual 429/403 responses from Finnhub) cannot be tested with mocked unit tests. Empirical validation documented in VALIDATION.md as manual-only gate R-02-A1 through R-02-A5; deferred to Phase 3/4 first live runs.

---

## Gaps Summary

No gaps. All 4 DATA requirements satisfied. 28 tests passing. One planned deviation (PyPI assertion) auto-fixed with no scope impact.

---

*Verified: 2026-05-16T06:18:00-06:00*
*Verifier: Claude (gsd-verifier)*
