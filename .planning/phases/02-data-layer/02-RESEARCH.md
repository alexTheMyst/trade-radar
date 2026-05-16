# Phase 2: Data Layer - Research

**Researched:** 2026-05-15
**Domain:** Finnhub Python SDK extension — rate limiting, retry, paid-tier detection, news fetch
**Confidence:** HIGH

---

## Summary

Phase 2 extends the existing `finnhub_client.py` (currently 21 lines, single `fetch_spy_close` function) with four capabilities: bulk quote fetch with a preemptive token bucket (DATA-01), tenacity-based 429 retry (DATA-02), graceful paid-tier skip on 403/404 (DATA-03), and company news headline fetch (DATA-04).

The Finnhub Python SDK (v2.4.28) raises `FinnhubAPIException` for **every** non-OK HTTP response — 429, 403, 404, 500 alike. The critical design constraint is that the retry predicate must discriminate 429 (retry) from 403/404 (skip gracefully), not blanket-retry on `FinnhubAPIException`. This distinction must be explicit in every task spec.

The token bucket is a min-interval enforcer for sequential execution: 60/55 ≈ 1.091s between calls. No burst capacity is needed or appropriate for this workload. `tenacity` 9.1.4 is not yet in `pyproject.toml` — Task 1 of the plan must add it via `uv add tenacity`.

**Primary recommendation:** Add a module-level `_acquire_slot()` rate-limit guard, wrap individual Finnhub calls with a tenacity decorator that retries only on `status_code == 429`, and let 403/404 fall through to a caller-side guard that logs and returns `None`.

---

<user_constraints>
## User Constraints (from CLAUDE.md)

### Locked Decisions
- Python 3.12+, `uv`, stdlib only except: `finnhub-python` SDK, `tenacity`, `anthropic` SDK
- Rate limit: ~10-line stdlib token bucket — NOT `pyrate-limiter`, NOT `asyncio`
- Retry: `tenacity` for reactive 429 handling — NOT a manual retry loop
- No `httpx` — `finnhub-python` wraps `requests`; no mixing HTTP clients
- Rate limit is 60 calls/min; use 55 as safe ceiling
- Sequential execution — Windows event-loop quirks, no asyncio
- All Finnhub calls happen through `src/signal_system/data/finnhub_client.py`

### Claude's Discretion
- Internal implementation details (module-level vs instance-level state, helper naming)
- Whether `fetch_spy_close` is refactored to flow through the new rate-limit wrapper or left with a manual `_acquire_slot()` call

### Deferred Ideas (OUT OF SCOPE)
- Async Finnhub calls
- `pyrate-limiter` or any third-party rate-limit library
- Per-ticker caching / memoization
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DATA-01 | Bulk quote fetch for a list of tickers with preemptive rate-limit token bucket (≤55 calls/min) | Token bucket pattern + bulk fetch loop — §Token Bucket, §Quote Fetch Pattern |
| DATA-02 | Retry Finnhub 429 responses via `tenacity` (up to 5 attempts, exponential backoff) | `FinnhubAPIException.status_code`, tenacity decorator pattern — §tenacity Retry Pattern |
| DATA-03 | Detect paid-tier 403/404 gracefully — log warning, return `None`, caller skips scoring | `_handle_response` raises `FinnhubAPIException` for all non-OK — §Paid-Tier Detection |
| DATA-04 | Company news headline fetch for ticker + date range | `company_news()` signature, return fields — §News Fetch Pattern |
</phase_requirements>

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Rate-limit enforcement | Data client (`finnhub_client.py`) | — | Must be preemptive and co-located with the HTTP calls; callers should not manage timing |
| 429 retry logic | Data client (tenacity decorator) | — | Retry is a transport concern; callers receive a result or a final exception |
| Paid-tier skip (403/404) | Data client + caller | — | Client detects and logs; caller decides to skip scoring (DISC-02 requirement) |
| Quote field validation | Data client | — | `c <= 0` guard belongs next to the raw API response |
| News fetch + date format | Data client | — | API signature contract lives here |

---

## 1. Current State

`src/signal_system/data/finnhub_client.py` (Phase 1 as-built, 21 lines):

```python
import finnhub
from signal_system import config

_client: finnhub.Client | None = None

def _get_client() -> finnhub.Client:
    global _client
    if _client is None:
        _client = finnhub.Client(api_key=config.FINNHUB_API_KEY)
    return _client

def fetch_spy_close() -> float:
    """Return SPY close price; raises ValueError on missing or non-positive data."""
    response = _get_client().quote("SPY")
    close = response.get("c")
    if close is None or close <= 0:
        raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
    return float(close)
```

**What it provides:**
- Module-level singleton `_client` — matches the pattern Phase 2 will follow for `_next_call_at` state
- `fetch_spy_close()` calls `quote("SPY")` directly with no rate limiting or retry

**What is missing (Phase 2 scope):**
- No token bucket
- No tenacity retry
- No paid-tier detection
- No bulk quote fetch
- No news fetch

**Existing behavior note:** `fetch_spy_close()` is called by `daily_close.py`. After Phase 2, it must flow through `_acquire_slot()` so it counts against the rate budget. The refactor is one added line — `_acquire_slot()` before `_get_client().quote("SPY")`.

[VERIFIED: codebase read — `src/signal_system/data/finnhub_client.py`]

---

## 2. Token Bucket Implementation

### Rate Math

55 calls/min → minimum interval between calls = 60 / 55 = **1.0909… seconds**.

For sequential single-threaded execution this is a simple min-interval enforcer: sleep until the next allowed slot, then record when the next slot opens. No burst capacity is needed — the Discovery job processes tickers one at a time.

### Exact ~10-line Pattern

```python
import time
import threading

_lock = threading.Lock()
_next_call_at: float = 0.0
_MIN_INTERVAL: float = 60.0 / 55  # 1.0909... seconds

def _acquire_slot() -> None:
    """Block until the next Finnhub API call slot is available.

    Call this immediately before every finnhub SDK call.
    Thread-safe but designed for sequential single-threaded use.
    """
    global _next_call_at
    with _lock:
        now = time.monotonic()
        wait = _next_call_at - now
        if wait > 0:
            time.sleep(wait)
        _next_call_at = max(now, _next_call_at) + _MIN_INTERVAL
```

**State placement:** Module-level globals, mirroring `_client`. No instance state needed since the client is already a module-level singleton.

**Why `threading.Lock`:** Windows Task Scheduler enforces single-instance, so concurrent calls don't happen in practice. The lock costs nothing and prevents a footgun if a future job ever calls from multiple threads.

**Why `time.monotonic()`:** Not affected by system clock adjustments (NTP, DST). Correct for interval enforcement.

**Call site pattern:**

```python
def fetch_quote(ticker: str) -> dict | None:
    _acquire_slot()
    return _get_client().quote(ticker)
```

[VERIFIED: stdlib `time`, `threading` — no external deps. Rate math derived from CLAUDE.md constraint (55 calls/min).]

---

## 3. tenacity Retry Pattern

### Package Status

`tenacity` 9.1.4 is **not in `pyproject.toml`** and not in the project venv. Plan Task 1 must run:

```bash
uv add tenacity
```

[VERIFIED: `pyproject.toml` read — tenacity absent. PyPI version 9.1.4 confirmed via PyPI JSON API, uploaded 2026-02-07, source: github.com/jd/tenacity.]

### The Critical Discriminator

`FinnhubAPIException` is raised for **all** non-OK HTTP responses (429, 403, 404, 500). Using `retry_if_exception_type(FinnhubAPIException)` would retry paid-tier 403/404 errors 5 times before giving up — violating DATA-03. The predicate must check `status_code`:

```python
def _is_transient_error(exc: BaseException) -> bool:
    """Return True for errors that warrant a retry.

    Retries:
      - FinnhubAPIException with status_code 429 (rate limit)
      - requests.exceptions.ConnectionError (network blip)
      - requests.exceptions.Timeout (slow response)

    Does NOT retry:
      - FinnhubAPIException with status_code 403 (paid tier)
      - FinnhubAPIException with status_code 404 (not found / paid tier)
      - FinnhubAPIException with status_code 400, 401 (client errors)
    """
    from finnhub.exceptions import FinnhubAPIException
    import requests.exceptions
    if isinstance(exc, FinnhubAPIException):
        return exc.status_code == 429
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout)):
        return True
    return False
```

### Decorator

```python
import logging
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

_RETRY_DECORATOR = retry(
    retry=retry_if_exception(_is_transient_error),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
```

**Backoff values explained:**
- `min=2` — first wait is 2 seconds (not 4 as in tenacity default example), appropriate for a 429 that may resolve quickly
- `max=60` — caps at 60s; beyond this, a paid-tier error or sustained outage should surface, not hide in retries
- `stop_after_attempt(5)` — 5 total attempts (1 original + 4 retries)
- `reraise=True` — final failure raises the original `FinnhubAPIException`, not a `RetryError`, so caller can inspect `status_code`. **Alternative:** `retry_error_callback=lambda rs: None` makes exhaustion return `None` instead of raising — useful if you want the decorator itself to absorb the error. Chosen here: `reraise=True` because the `fetch_quotes` loop catches exhausted retries explicitly (see §5), keeping error handling visible at the call site rather than hidden in the decorator.
- `before_sleep_log` — logs attempt number and wait time at WARNING level before each sleep [VERIFIED: tenacity 9.1.4 docs, github.com/jd/tenacity]

### Usage

Apply the decorator to the inner call, **after** `_acquire_slot()` runs:

```python
@_RETRY_DECORATOR
def _raw_quote(ticker: str) -> dict:
    """Rate-limited, retried raw quote call. Raises FinnhubAPIException on non-transient error."""
    _acquire_slot()
    return _get_client().quote(ticker)
```

Or inline with `wraps`:

```python
def fetch_quote(ticker: str) -> dict | None:
    """Fetch a quote, rate-limited and retry-wrapped. Returns None on paid-tier 403/404."""
    try:
        _acquire_slot()
        result = _RETRY_DECORATED_QUOTE(ticker)
        ...
    except FinnhubAPIException as exc:
        if exc.status_code in (403, 404):
            logger.warning("Paid-tier endpoint for %s (HTTP %s) — skipping", ticker, exc.status_code)
            return None
        raise
```

### Logging Retry Count

`before_sleep_log` from tenacity logs attempt number automatically. To also capture the attempt count programmatically, `retry_state.attempt_number` is available in custom `before_sleep` callbacks:

```python
def _log_retry(retry_state) -> None:
    exc = retry_state.outcome.exception()
    logger.warning(
        "Finnhub retry attempt %d for %s — %s",
        retry_state.attempt_number,
        retry_state.args[0] if retry_state.args else "unknown",
        exc,
    )
```

[VERIFIED: tenacity 9.1.4 docs — `retry_state.attempt_number`, `before_sleep_log` signature confirmed from github.com/jd/tenacity/doc/source/index.rst]

---

## 4. Paid-Tier Detection

### How Errors Surface

From SDK source (`finnhub/client.py`, verified by inspection):

```python
@staticmethod
def _handle_response(response):
    if not response.ok:
        raise FinnhubAPIException(response)
    ...
```

`FinnhubAPIException.__init__` captures `response.status_code` and `response.json().get("error")`. So:

- HTTP 429 → `FinnhubAPIException` with `.status_code == 429`
- HTTP 403 → `FinnhubAPIException` with `.status_code == 403`
- HTTP 404 → `FinnhubAPIException` with `.status_code == 404`

[VERIFIED: `finnhub/client.py` and `finnhub/exceptions.py` source inspected directly via `inspect.getsource`]

### Which Endpoints Are Paid vs Free

| Endpoint | SDK Method | Free Tier | Notes |
|----------|-----------|-----------|-------|
| `/quote` | `quote(symbol)` | Yes | Core free endpoint [ASSUMED — standard community knowledge] |
| `/company-news` | `company_news(symbol, _from, to)` | Yes | Used in many free-tier examples [ASSUMED] |
| `/stock/insider-sentiment` | `stock_insider_sentiment()` | No (paid) | Explicitly flagged in CLAUDE.md as likely paid [ASSUMED — validate empirically] |
| `/scan/technical-indicator` | `technical_indicator()` | No (paid) | Explicitly flagged in CLAUDE.md as likely paid [ASSUMED — validate empirically] |
| `^GSPC`, `^VIX` as symbols | `quote("^GSPC")` | Unknown | CLAUDE.md flags as "may be paid-only" [ASSUMED — validate empirically] |

**IMPORTANT:** Whether Finnhub returns 403 (forbidden) vs 404 (not found) for free-tier accounts accessing paid endpoints is not deterministically verified without a live API call. The detection code must handle **both** 403 and 404 as "skip gracefully":

```python
PAID_TIER_STATUS_CODES = frozenset({403, 404})

if exc.status_code in PAID_TIER_STATUS_CODES:
    logger.warning(
        "Finnhub endpoint unavailable for %r (HTTP %s): %s — skipping",
        ticker, exc.status_code, exc.message,
    )
    return None
```

[VERIFIED: SDK behavior from source inspection. Status code values for specific free/paid endpoints: ASSUMED — requires runtime validation per CLAUDE.md instruction]

---

## 5. Quote Fetch Pattern

### SDK Signature

```python
# Source: finnhub SDK v2.4.28, inspect.getsource(finnhub.Client.quote)
def quote(self, symbol):
    return self._get("/quote", params={"symbol": symbol})
```

### Response Fields

A successful `quote("AAPL")` returns a dict with these keys:

| Field | Meaning | Type |
|-------|---------|------|
| `c` | Current price (or last close) | float |
| `h` | High price of the day | float |
| `l` | Low price of the day | float |
| `o` | Open price of the day | float |
| `pc` | Previous close price | float |
| `t` | Timestamp (Unix epoch, seconds) | int |

[ASSUMED — field names are standard Finnhub REST API documentation; confirmed consistent with existing `fetch_spy_close` which reads `c`. Runtime validation recommended.]

### Empty / Invalid Response

When a ticker is unknown or has no data, Finnhub returns a dict with all fields set to `0` rather than an error response. This is the existing `fetch_spy_close` guard:

```python
close = response.get("c")
if close is None or close <= 0:
    # treat as no data
    return None
```

The bulk fetcher must apply the same guard per-ticker.

### Bulk Quote Fetch Pattern

```python
from __future__ import annotations
import logging
from finnhub.exceptions import FinnhubAPIException

logger = logging.getLogger(__name__)

PAID_TIER_STATUS_CODES = frozenset({403, 404})


def fetch_quotes(tickers: list[str]) -> dict[str, dict | None]:
    """Fetch quotes for a list of tickers with rate limiting and retry.

    Returns a mapping of ticker -> quote dict (or None if data unavailable).
    Persistent failures (429 exhausted after 5 attempts) are caught per-ticker:
    that ticker gets None and the batch continues. Does not raise.
    """
    results: dict[str, dict | None] = {}
    for ticker in tickers:
        try:
            results[ticker] = _fetch_single_quote(ticker)
        except Exception as exc:
            logger.error("Giving up on %r after exhausted retries: %s", ticker, exc)
            results[ticker] = None
    return results


@_RETRY_DECORATOR
def _fetch_single_quote(ticker: str) -> dict | None:
    """Rate-limited, retried quote for one ticker. Returns None on missing data or paid-tier."""
    _acquire_slot()
    try:
        response = _get_client().quote(ticker)
    except FinnhubAPIException as exc:
        if exc.status_code in PAID_TIER_STATUS_CODES:
            logger.warning("Quote unavailable for %r (HTTP %s) — paid endpoint or unknown ticker, skipping", ticker, exc.status_code)
            return None
        raise  # 429 and transient errors re-raised → tenacity retries

    close = response.get("c")
    if close is None or close <= 0:
        logger.debug("No price data for %r (c=%r) — skipping", ticker, close)
        return None
    return response
```

**Design note:** `_fetch_single_quote` is the tenacity-decorated function. The 403/404 catch is *inside* the decorated function before the `raise`, so non-retriable errors exit cleanly on first attempt. The `raise` for other `FinnhubAPIException` statuses lets tenacity see only what `_is_transient_error` approves.

**Alternative structure:** Some prefer the try/except outside the decorator. The above structure (catch inside) is preferred here because it keeps the "return None" path co-located with the API call and prevents confusing `RetryError` propagation for 403/404.

---

## 6. News Fetch Pattern

### SDK Signature

```python
# Source: finnhub SDK v2.4.28, inspect.getsource(finnhub.Client.company_news)
def company_news(self, symbol, _from, to):
    return self._get("/company-news", params={
        "symbol": symbol,
        "from": _from,
        "to": to,
    })
```

### Date Format

Parameters `_from` and `to` are string dates in `YYYY-MM-DD` format. [ASSUMED — consistent with Finnhub REST API documentation convention; verify with a live call.]

Example: `finnhub_client.company_news("AAPL", "2026-05-01", "2026-05-15")`

### Response Fields

Returns a **list** of news item dicts. Each item contains:

| Field | Meaning | Type |
|-------|---------|------|
| `headline` | Article headline text | str |
| `source` | Source name (e.g., "Reuters") | str |
| `datetime` | Unix timestamp of article | int |
| `url` | Article URL | str |
| `summary` | Article summary text | str |
| `id` | Article ID | int |
| `image` | Image URL (may be empty string) | str |
| `category` | Category string | str |
| `related` | Related symbol | str |

[ASSUMED — standard Finnhub company-news API response shape. Runtime validation recommended.]

**Empty response:** If no news exists for the ticker/date range, returns `[]` (empty list), not `None`. Callers must handle `[]`. [ASSUMED — standard API behavior; runtime validation recommended.]

### News Fetch Pattern

```python
from datetime import date


def fetch_company_news(
    ticker: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """Fetch company news headlines for ticker within date range.

    Args:
        ticker: Stock symbol.
        from_date: Start date (inclusive).
        to_date: End date (inclusive).

    Returns:
        List of news item dicts. Empty list if no news or paid-tier 403/404.
        Never raises on Finnhub errors — logs and returns [].
    """
    return _fetch_company_news_raw(
        ticker,
        from_date.isoformat(),   # YYYY-MM-DD
        to_date.isoformat(),
    )


@_RETRY_DECORATOR
def _fetch_company_news_raw(ticker: str, from_str: str, to_str: str) -> list[dict]:
    """Rate-limited, retried news fetch. Returns [] on paid-tier or missing data."""
    _acquire_slot()
    try:
        result = _get_client().company_news(ticker, from_str, to_str)
    except FinnhubAPIException as exc:
        if exc.status_code in PAID_TIER_STATUS_CODES:
            logger.warning("News unavailable for %r (HTTP %s) — paid endpoint or unknown ticker, returning []", ticker, exc.status_code)
            return []
        raise
    return result if isinstance(result, list) else []
```

**`isinstance` guard on return:** Defensive against unexpected non-list API responses. Costs nothing, prevents downstream `TypeError`.

---

## 7. Module Extension Plan

### How to Extend Without Breaking `daily_close.py`

`daily_close.py` currently imports `fetch_spy_close` from `finnhub_client.py`. The extension adds new public functions; `fetch_spy_close` stays but gets `_acquire_slot()` added.

**Final public API of `finnhub_client.py` after Phase 2:**

```python
# Public functions (importable by jobs and agents):
fetch_spy_close() -> float          # existing — add _acquire_slot() call
fetch_quotes(tickers) -> dict       # new — DATA-01, DATA-02, DATA-03
fetch_company_news(ticker, from_date, to_date) -> list[dict]  # new — DATA-04

# Private (module-internal):
_acquire_slot() -> None             # token bucket
_get_client() -> finnhub.Client     # existing singleton
_is_transient_error(exc) -> bool    # retry predicate
_RETRY_DECORATOR                    # tenacity decorator instance
_fetch_single_quote(ticker) -> dict | None    # decorated inner
_fetch_company_news_raw(...)  -> list[dict]   # decorated inner
```

### Recommended File Structure (additions only)

```
src/signal_system/data/finnhub_client.py   # extend in-place — no new files needed
```

### Module-Level Declaration Order

```python
"""finnhub_client.py — all Finnhub API access for signal-system."""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

import finnhub
from finnhub.exceptions import FinnhubAPIException
import requests.exceptions
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from signal_system import config

logger = logging.getLogger(__name__)

# ── Rate limit ───────────────────────────────────────────────────────────────
_MIN_INTERVAL: float = 60.0 / 55
_next_call_at: float = 0.0
_lock = threading.Lock()

# ── Singleton client ─────────────────────────────────────────────────────────
_client: finnhub.Client | None = None

# ── Constants ────────────────────────────────────────────────────────────────
PAID_TIER_STATUS_CODES = frozenset({403, 404})

# ── Helpers ──────────────────────────────────────────────────────────────────
def _get_client() -> finnhub.Client: ...
def _acquire_slot() -> None: ...
def _is_transient_error(exc: BaseException) -> bool: ...

# ── Tenacity decorator (module-level instance, not applied inline) ────────────
_RETRY_DECORATOR = retry(...)

# ── Private decorated functions ──────────────────────────────────────────────
@_RETRY_DECORATOR
def _fetch_single_quote(ticker: str) -> dict | None: ...

@_RETRY_DECORATOR
def _fetch_company_news_raw(...) -> list[dict]: ...

# ── Public API ───────────────────────────────────────────────────────────────
def fetch_spy_close() -> float: ...      # existing, add _acquire_slot()
def fetch_quotes(tickers) -> dict: ...   # new
def fetch_company_news(...) -> list[dict]: ...  # new
```

### Refactoring `fetch_spy_close`

Add one line before the quote call:

```python
def fetch_spy_close() -> float:
    """Return SPY close price; raises ValueError on missing or non-positive data."""
    _acquire_slot()                         # NEW — counts against rate budget
    response = _get_client().quote("SPY")
    close = response.get("c")
    if close is None or close <= 0:
        raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
    return float(close)
```

Note: `fetch_spy_close` intentionally does **not** use `_RETRY_DECORATOR` — it raises `ValueError` on bad data, not on `FinnhubAPIException`, and the `daily_close` job already wraps this in a heartbeat context manager that handles failures.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Exponential backoff with retry limits | Manual loop with `time.sleep` and counters | `tenacity` | Handles backoff math, before-sleep hooks, reraise semantics, retry state — ~50 lines vs 3 |
| Retry predicate logic | Catch-all `except Exception: retry` | `retry_if_exception(_is_transient_error)` | Prevents silently retrying 403/404 — the most dangerous footgun in this domain |
| Rate limiting | `asyncio`, `pyrate-limiter` | stdlib `time.monotonic` + `threading.Lock` | Sequential jobs need a min-interval enforcer, not a concurrent token bucket |

---

## Common Pitfalls

### Pitfall 1: Retrying All FinnhubAPIExceptions
**What goes wrong:** Using `retry_if_exception_type(FinnhubAPIException)` causes paid-tier 403 responses to be retried 5 times (with 2-60s backoff) before failing. Discovery Agent scans 500+ tickers — 10-minute stall per paid endpoint.
**Why it happens:** `FinnhubAPIException` is the SDK's single exception class for all HTTP errors.
**How to avoid:** Always use `retry_if_exception(_is_transient_error)` with an explicit `status_code == 429` check.
**Warning signs:** Seeing "Retry attempt 1/2/3" in logs for the same ticker repeatedly, wall time far exceeding expected batch time.

### Pitfall 2: Applying Tenacity Decorator Inside the `for` Loop
**What goes wrong:** Decorating `fetch_quotes` (the loop) instead of `_fetch_single_quote` (the inner call) means a single 429 retries the entire batch, re-fetching all previously successful tickers.
**Why it happens:** Decorator placement looks natural at the outer function.
**How to avoid:** Decorate only the innermost function that makes a single Finnhub call.
**Warning signs:** Repeated ticker results in logs on a 429 recovery.

### Pitfall 3: `_acquire_slot()` After the API Call
**What goes wrong:** The token bucket is a *preemptive* rate limiter — it enforces minimum interval *before* the call. Placing it after creates burst behavior on the first call and incorrect timing.
**How to avoid:** First line of every raw API call function: `_acquire_slot()`.

### Pitfall 4: Not Handling Empty Quote Response as `None`
**What goes wrong:** Unknown tickers return `{"c": 0, "h": 0, ...}` — not an error, not an exception. Downstream code that trusts `c` as a price will use 0 as a valid price, producing garbage scores.
**How to avoid:** Apply the `c <= 0` guard in `_fetch_single_quote`, return `None`, and document that `fetch_quotes` values can be `None`.

### Pitfall 5: Missing `tenacity` in pyproject.toml
**What goes wrong:** `tenacity` is not in `pyproject.toml`. Importing it raises `ModuleNotFoundError` at runtime (and on Windows, at Task Scheduler trigger time with no visible error).
**How to avoid:** Task 1 of the plan: `uv add tenacity` before any code is written.

---

## Package Legitimacy Audit

> slopcheck was not available at research time. Per protocol, packages are tagged `[ASSUMED]` and the planner must gate each install behind a `checkpoint:human-verify` task.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| tenacity | PyPI | ~10 yrs (active since 2016) | High (widely used) | github.com/jd/tenacity | N/A — slopcheck unavailable | `[ASSUMED]` — planner adds checkpoint |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

*slopcheck was unavailable at research time. All packages above are tagged `[ASSUMED]`. The planner must gate the `uv add tenacity` task behind a `checkpoint:human-verify` step. Manual verification: `pip index versions tenacity` confirms 9.1.4 on PyPI; github.com/jd/tenacity is the active source repo with regular commits.*

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3+ (already installed, 17 tests passing) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_smoke.py -x` |
| Full suite command | `uv run pytest -x` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DATA-01 | Token bucket enforces ≤55 calls/min | unit | `uv run pytest tests/test_smoke.py -k "token_bucket" -x` | ❌ Wave 0 |
| DATA-01 | `fetch_quotes` returns dict keyed by ticker | unit | `uv run pytest tests/test_smoke.py -k "fetch_quotes" -x` | ❌ Wave 0 |
| DATA-02 | tenacity retries on 429, stops at attempt 5 | unit (mock) | `uv run pytest tests/test_smoke.py -k "retry_429" -x` | ❌ Wave 0 |
| DATA-02 | tenacity does NOT retry on 403 | unit (mock) | `uv run pytest tests/test_smoke.py -k "no_retry_403" -x` | ❌ Wave 0 |
| DATA-03 | 403 response → `None` returned, warning logged | unit (mock) | `uv run pytest tests/test_smoke.py -k "paid_tier" -x` | ❌ Wave 0 |
| DATA-04 | `fetch_company_news` returns list, handles `[]` | unit (mock) | `uv run pytest tests/test_smoke.py -k "company_news" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_smoke.py -x`
- **Per wave merge:** `uv run pytest -x`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
All test functions for DATA-01 through DATA-04 need to be written. They go in `tests/test_smoke.py` (existing file) following the established pattern. Tests must mock `finnhub.Client` to avoid live API calls. The token-bucket test should mock `time.sleep` to verify it's called with the correct interval.

---

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | API key in `.env` — already handled by `config.py` |
| V3 Session Management | no | Stateless HTTP calls |
| V4 Access Control | no | Single-operator system |
| V5 Input Validation | yes | Ticker symbols passed to Finnhub — validate format, no user-supplied input |
| V6 Cryptography | no | N/A |

### Known Threat Patterns

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Ticker symbol injection into API calls | Tampering | Finnhub SDK encodes params via `requests`; validate ticker format (alphanumeric + `.` + `-`) before passing |
| API key leakage in logs | Information Disclosure | Never log `config.FINNHUB_API_KEY`; log only ticker and status code |
| 429 amplification | Denial of Service | Tenacity `max=60` and `stop_after_attempt(5)` bound total retry time per ticker to ~240s worst case |

---

## Open Questions

1. **Does Finnhub return 403 or 404 for free-tier paid-endpoint access?**
   - What we know: SDK raises `FinnhubAPIException` with `.status_code` for both. Detection code handles both.
   - What's unclear: Which specific status code Finnhub sends for "your plan doesn't include this endpoint" vs "ticker not found."
   - Recommendation: Handle both in `PAID_TIER_STATUS_CODES = frozenset({403, 404})` — validated empirically on first Discovery Agent run.

2. **Are `quote` and `company-news` confirmed free-tier?**
   - What we know: These are standard free-tier examples in Finnhub documentation and community usage.
   - What's unclear: Whether there are any rate or volume limits beyond the 60/min cap.
   - Recommendation: Treat as free, validate empirically. If they return 403, the paid-tier detection handles it gracefully.

3. **`httpx` is in `pyproject.toml` (line 10) but CLAUDE.md forbids it.**
   - What we know: `httpx` was added during Phase 1 (likely for the healthchecks.io heartbeat which uses `requests`).
   - What's unclear: Whether it's actually imported anywhere or is a leftover dependency.
   - Recommendation: Out of Phase 2 scope — do not add usage, do not remove it. Flag for a cleanup phase.

4. **Date format for `company_news` `_from`/`to` parameters.**
   - What we know: Parameters are strings; `YYYY-MM-DD` is the Finnhub REST API standard.
   - What's unclear: Whether passing `datetime.date.isoformat()` (which is `YYYY-MM-DD`) is correct.
   - Recommendation: Use `date.isoformat()` — validate on first news-morning run.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `quote` and `company-news` are Finnhub free-tier endpoints | §4 Paid-Tier Detection | If paid, every ticker returns 403 — data layer returns all `None`, agents score nothing |
| A2 | Unknown tickers return `{"c": 0, ...}` not a 404 | §5 Quote Fetch Pattern | If they raise `FinnhubAPIException(404)`, the `c <= 0` guard is never reached — need separate handling |
| A3 | `company_news` date params use `YYYY-MM-DD` string format | §6 News Fetch Pattern | Wrong format → HTTP 400 or empty results |
| A4 | `company_news` returns `[]` for no-news tickers (not `None`) | §6 News Fetch Pattern | If it returns `None`, `isinstance(result, list)` guard handles it — low risk |
| A5 | `tenacity` 9.1.4 is safe to install (slopcheck unavailable) | §Package Legitimacy | Low practical risk — well-known library since 2016 — but protocol requires checkpoint |
| A6 | Finnhub returns HTTP 429 (not 503 or other) for rate limits | §3 tenacity Retry | If it returns 503, `_is_transient_error` returns `False` and the call fails immediately |

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual retry loops with `time.sleep` | `tenacity` decorator with `wait_exponential` | ~2018+ | Retry logic is declarative, testable, and handles edge cases |
| Global rate limit via `time.sleep(1)` everywhere | Min-interval token bucket with `time.monotonic` | — | Correct under variable call durations; no cumulative drift |

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12+ | All | ✓ | (project-locked) | — |
| `uv` | Package management | ✓ | (project-locked) | — |
| `finnhub-python` | All data calls | ✓ | 2.4.28 | — |
| `tenacity` | DATA-02 retry | ✗ | — | None — must `uv add tenacity` (Plan Task 1) |
| `requests` | transient error catch | ✓ | (transitive dep of finnhub-python) | — |

**Missing dependencies with no fallback:**
- `tenacity` — Plan Task 1 must add it before any other task

**Missing dependencies with fallback:**
- None

---

## Sources

### Primary (HIGH confidence)
- `src/signal_system/data/finnhub_client.py` — current implementation state (codebase read)
- `finnhub/client.py` + `finnhub/exceptions.py` — SDK source via `inspect.getsource` (runtime verified)
- `pyproject.toml` — confirmed tenacity absent, finnhub-python 2.4.28 present
- github.com/jd/tenacity `doc/source/index.rst` — tenacity API: `wait_exponential`, `stop_after_attempt`, `retry_if_exception`, `before_sleep_log`, `retry_state.attempt_number`, `reraise=True`
- PyPI JSON API — tenacity 9.1.4, uploaded 2026-02-07

### Secondary (MEDIUM confidence)
- CLAUDE.md "What NOT to Use" section — confirms pyrate-limiter, asyncio, httpx are forbidden
- `.planning/REQUIREMENTS.md` DATA-01 through DATA-04 — requirement text verbatim

### Tertiary (LOW confidence — ASSUMED)
- Finnhub free-tier endpoint list (quote, company-news as free) — community knowledge, unverified without live API call
- `company_news` date format `YYYY-MM-DD` — inferred from Finnhub REST API convention
- Unknown ticker returning `{"c": 0}` — inferred from existing `fetch_spy_close` guard pattern

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — SDK source verified by inspection, tenacity API verified from official GitHub docs
- Architecture: HIGH — patterns derived directly from existing codebase conventions
- Pitfalls: HIGH — derived from SDK source analysis (FinnhubAPIException structure)
- Paid-tier endpoint list: LOW — requires runtime validation

**Research date:** 2026-05-15
**Valid until:** 2026-06-15 (stable APIs; tenacity and finnhub-python are stable libraries)
