---
plan_id: "02-01"
phase: "02-data-layer"
plan: "01"
type: execute
wave: 1
depends_on: ["01-01"]
files_modified:
  - pyproject.toml
  - uv.lock
  - src/signal_system/data/finnhub_client.py
  - tests/test_smoke.py
autonomous: true
requirements: [DATA-01, DATA-02, DATA-03, DATA-04]

must_haves:
  truths:
    - "Passing a list of tickers to fetch_quotes() never exceeds 55 Finnhub calls/min"
    - "A Finnhub 429 response triggers up to 5 retry attempts with exponential backoff; the attempt number is logged"
    - "A 403 or 404 response causes the client to log a warning and return None — no exception propagates to the caller"
    - "fetch_company_news() returns a list of dicts containing at minimum headline and source keys; returns [] when no news or on paid-tier error"
    - "fetch_spy_close() flows through _acquire_slot() so it counts against the rate budget"
    - "The test suite remains green (≥ 17 tests, 0 failures) after every task commit"
  artifacts:
    - path: "src/signal_system/data/finnhub_client.py"
      provides: "Token bucket, retry decorator, fetch_quotes, fetch_company_news, refactored fetch_spy_close"
      exports:
        - fetch_spy_close
        - fetch_quotes
        - fetch_company_news
        - PAID_TIER_STATUS_CODES
    - path: "tests/test_smoke.py"
      provides: "Tests for DATA-01 through DATA-04 (no live API calls — all mocked)"
      contains: "test_token_bucket, test_fetch_quotes, test_retry_429, test_no_retry_403, test_paid_tier_none, test_company_news"
    - path: "pyproject.toml"
      provides: "tenacity pinned as dependency"
      contains: "tenacity"
  key_links:
    - from: "fetch_quotes(tickers)"
      to: "_fetch_single_quote(ticker)"
      via: "per-ticker loop; each iteration catches exhausted retry exceptions and writes None"
      pattern: "_fetch_single_quote"
    - from: "_fetch_single_quote"
      to: "_acquire_slot() + _get_client().quote()"
      via: "_RETRY_DECORATOR wraps the inner call; _acquire_slot() is first line inside"
      pattern: "_acquire_slot"
    - from: "fetch_company_news"
      to: "_fetch_company_news_raw"
      via: "_RETRY_DECORATOR; converts date objects to YYYY-MM-DD strings before passing"
      pattern: "_fetch_company_news_raw"
---

<objective>
Extend finnhub_client.py with a stdlib token bucket (55 calls/min), tenacity-based 429 retry, paid-tier 403/404 detection, bulk quote fetch, and company news fetch. The existing fetch_spy_close() is also wired through the token bucket.

Purpose: Phases 3 (News Classifier) and 4 (Discovery Agent) both import from finnhub_client.py. Both depend on reliable, rate-respecting data access with graceful degradation on paid endpoints. This phase delivers those guarantees before either agent is built.

Output: A fully extended finnhub_client.py with three public functions (fetch_spy_close, fetch_quotes, fetch_company_news) and a green test suite demonstrating each behavior without live API calls.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/ROADMAP.md
@.planning/REQUIREMENTS.md
@.planning/phases/01-foundation/01-SUMMARY.md
@.planning/phases/02-data-layer/02-RESEARCH.md
@src/signal_system/data/finnhub_client.py
</context>

<interfaces>
<!-- Key contracts the executor needs. No codebase exploration needed. -->

From src/signal_system/data/finnhub_client.py (current, 21 lines):
  _client: finnhub.Client | None = None
  _get_client() -> finnhub.Client          # singleton, keyed by config.FINNHUB_API_KEY
  fetch_spy_close() -> float               # raises ValueError on c <= 0 or None

From finnhub SDK v2.4.28 (verified by source inspection):
  finnhub.Client.quote(symbol: str) -> dict          # keys: c, h, l, o, pc, t
  finnhub.Client.company_news(symbol, _from, to) -> list[dict]  # keys: headline, source, datetime, url, ...
  finnhub.exceptions.FinnhubAPIException             # raised for ALL non-OK HTTP responses
    .status_code: int                                # 429 = rate limit, 403/404 = paid tier
    .message: str

From Phase 1 (01-SUMMARY.md): 17 tests passing as baseline.

Public API after Phase 2 (to be implemented):
  fetch_spy_close() -> float               # existing — add _acquire_slot() before quote call
  fetch_quotes(tickers: list[str]) -> dict[str, dict | None]   # DATA-01 / DATA-02 / DATA-03
  fetch_company_news(ticker: str, from_date: date, to_date: date) -> list[dict]  # DATA-04

Private (module-internal):
  _MIN_INTERVAL: float = 60.0 / 55        # 1.0909... seconds
  _next_call_at: float = 0.0
  _lock: threading.Lock
  PAID_TIER_STATUS_CODES: frozenset = frozenset({403, 404})
  _acquire_slot() -> None
  _is_transient_error(exc: BaseException) -> bool
  _RETRY_DECORATOR                         # tenacity retry() instance
  _fetch_single_quote(ticker: str) -> dict | None
  _fetch_company_news_raw(ticker, from_str, to_str) -> list[dict]
</interfaces>

<tasks>

<!-- ═══════════════════════════════════════════════════════════════
     T1: Pin tenacity dependency
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto">
  <name>T1: Pin tenacity and verify PyPI legitimacy</name>
  <files>pyproject.toml, uv.lock</files>
  <action>
Before touching any code, verify tenacity's identity on PyPI, then add it:

  curl -s "https://pypi.org/pypi/tenacity/json" | python3 -c "
  import sys, json
  d = json.load(sys.stdin)
  info = d['info']
  urls = d['urls']
  assert 'jd/tenacity' in info.get('project_urls', {}).get('Source', ''), f'Unexpected source: {info.get(\"project_urls\")}'
  assert info['version'] >= '9.0.0', f'Old version: {info[\"version\"]}'
  print(f'OK: tenacity {info[\"version\"]} from {info[\"project_urls\"][\"Source\"]}')
  "

If the assertion passes, run:

  uv add tenacity

Commit message: `chore(02): pin tenacity dependency`

Do NOT add pyrate-limiter, asyncio extras, or httpx. tenacity is the only new dependency this phase.
  </action>
  <verify>
    <automated>
      grep -c "tenacity" pyproject.toml
      # Must return ≥ 1

      python -c "import tenacity; print(tenacity.__version__)"
      # Must print a version ≥ 9.0.0 without ImportError

      grep -cE "pyrate.limiter" pyproject.toml || true
      # Must return 0
    </automated>
  </verify>
  <done>tenacity appears in pyproject.toml and uv.lock; importable in the venv; no pyrate-limiter present.</done>
</task>


<!-- ═══════════════════════════════════════════════════════════════
     T2 (RED): Failing tests — token bucket + fetch_quotes + 429 retry
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto" tdd="true">
  <name>T2 (RED): Failing tests for token bucket, fetch_quotes, and 429 retry</name>
  <files>tests/test_smoke.py</files>
  <behavior>
    - test_token_bucket_calls_sleep: patch time.monotonic with a counter starting at 0 and time.sleep to a no-op capture; call _acquire_slot() twice; assert sleep was called with a value close to _MIN_INTERVAL on the second call (first call _next_call_at=0, so wait=0-0=0, sleep not called with positive value; second call wait = _MIN_INTERVAL - elapsed; use monkeypatch on signal_system.data.finnhub_client.time.monotonic and signal_system.data.finnhub_client.time.sleep).
    - test_fetch_quotes_returns_dict: monkeypatch finnhub.Client.quote to return {"c": 150.0, "h": 151.0, "l": 149.0, "o": 149.5, "pc": 148.0, "t": 1700000000}; also patch _acquire_slot to no-op; call fetch_quotes(["AAPL", "MSFT"]); assert result == {"AAPL": {...}, "MSFT": {...}} with c > 0.
    - test_fetch_quotes_none_on_zero_price: patch quote to return {"c": 0, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}; patch _acquire_slot; call fetch_quotes(["UNKNOWN"]); assert result["UNKNOWN"] is None.
    - test_retry_on_429: build a fake FinnhubAPIException with status_code=429. Pattern: create mock_resp = MagicMock(); mock_resp.status_code = 429; mock_resp.json.return_value = {"error": "rate limit"}; exc = FinnhubAPIException(mock_resp). Patch _get_client().quote to raise this exception on each call. Also patch _acquire_slot to no-op. Call _fetch_single_quote("AAPL") inside pytest.raises(FinnhubAPIException). Assert that quote was called exactly 5 times (tenacity stop_after_attempt(5)).
    - test_no_retry_on_403: build FinnhubAPIException with status_code=403 using same mock_resp pattern. Patch quote to raise it. Patch _acquire_slot. Call _fetch_single_quote("AAPL") and assert it returns None (paid-tier skip) — and that quote was called exactly once (no retry).
  </behavior>
  <action>
Add these five test functions to tests/test_smoke.py, after the existing Phase 1 tests. Each test is self-contained with its own monkeypatches. Do not delete or modify existing tests.

Use this import block at the top of the new test section (or add to existing imports if already present):
  from unittest.mock import MagicMock, patch
  from finnhub.exceptions import FinnhubAPIException
  import signal_system.data.finnhub_client as fc

Helper for building a fake FinnhubAPIException (define once at module scope or as a fixture):
  def _make_finnhub_exc(status_code: int):
      r = MagicMock()
      r.status_code = status_code
      r.json.return_value = {"error": f"http {status_code}"}
      return FinnhubAPIException(r)

The token bucket test: monkeypatch signal_system.data.finnhub_client.time.monotonic — return a sequence [0.0, 0.0, 1.2] for successive calls (first call: now=0.0, _next_call_at=0.0, wait=0.0; second call: now=0.0, set _next_call_at = 0.0 + _MIN_INTERVAL; if called again: now=1.2, wait could be negative or zero). Capture calls to time.sleep by replacing it. Verify sleep is not called with a negative value.

Commit message: `test(02): RED — token bucket, fetch_quotes, 429 retry`

These tests MUST fail at this point because the implementation does not exist yet. Confirm with:
  uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry_429 or no_retry or paid_tier" -x 2>&1 | tail -5
Expected: at least one FAILED line. If all pass, the tests are not actually testing new behavior.
  </action>
  <verify>
    <automated>
      uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry_429 or no_retry_403 or paid_tier" --tb=no -q 2>&1 | grep -E "failed|error"
      # Must show ≥ 1 failed — these are RED tests
    </automated>
  </verify>
  <done>Five new test functions exist in test_smoke.py and at least one fails because the implementation is absent.</done>
</task>


<!-- ═══════════════════════════════════════════════════════════════
     T3 (GREEN): Token bucket + _fetch_single_quote + fetch_quotes
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto" tdd="true">
  <name>T3 (GREEN): Implement token bucket, retry decorator, fetch_quotes, refactor fetch_spy_close</name>
  <files>src/signal_system/data/finnhub_client.py</files>
  <action>
Rewrite finnhub_client.py in full. The file must follow this exact declaration order:

1. Module docstring: `"""finnhub_client.py — all Finnhub API access for signal-system."""`

2. Imports:
   from __future__ import annotations
   import logging
   import threading
   import time
   from datetime import date
   import finnhub
   from finnhub.exceptions import FinnhubAPIException
   import requests.exceptions
   from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential, before_sleep_log
   from signal_system import config

3. Module-level logger: logger = logging.getLogger(__name__)

4. Rate-limit state (token bucket):
   _MIN_INTERVAL: float = 60.0 / 55   # 1.0909... seconds
   _next_call_at: float = 0.0
   _lock = threading.Lock()

5. Singleton client state:
   _client: finnhub.Client | None = None

6. Constants:
   PAID_TIER_STATUS_CODES: frozenset[int] = frozenset({403, 404})

7. _get_client() — unchanged from Phase 1.

8. _acquire_slot():
   global _next_call_at
   with _lock:
       now = time.monotonic()
       wait = _next_call_at - now
       if wait > 0:
           time.sleep(wait)
       _next_call_at = max(now, _next_call_at) + _MIN_INTERVAL

9. _is_transient_error(exc: BaseException) -> bool:
   Returns True ONLY for FinnhubAPIException with status_code == 429, requests.exceptions.ConnectionError, or requests.exceptions.Timeout.
   Returns False for status_code 403, 404, 400, 401, and all other cases.
   IMPORTANT: This discriminator is the core correctness guarantee of this module — 403/404 must NOT be retried.

10. _RETRY_DECORATOR = retry(
        retry=retry_if_exception(_is_transient_error),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

11. @_RETRY_DECORATOR
    def _fetch_single_quote(ticker: str) -> dict | None:
        _acquire_slot()
        try:
            response = _get_client().quote(ticker)
        except FinnhubAPIException as exc:
            if exc.status_code in PAID_TIER_STATUS_CODES:
                logger.warning("Quote unavailable for %r (HTTP %s) — paid tier or unknown, skipping", ticker, exc.status_code)
                return None
            raise   # 429 and other errors re-raise → tenacity sees them
        close = response.get("c")
        if close is None or close <= 0:
            logger.debug("No price data for %r (c=%r) — skipping", ticker, close)
            return None
        return response

12. def fetch_quotes(tickers: list[str]) -> dict[str, dict | None]:
        Docstring: "Fetch quotes for a list of tickers. Returns ticker -> quote dict or None. Never raises."
        results: dict[str, dict | None] = {}
        for ticker in tickers:
            try:
                results[ticker] = _fetch_single_quote(ticker)
            except Exception as exc:
                logger.error("Giving up on %r after exhausted retries: %s", ticker, exc)
                results[ticker] = None
        return results

13. def fetch_spy_close() -> float:
        _acquire_slot()   # ADD THIS LINE — counts against rate budget
        response = _get_client().quote("SPY")
        close = response.get("c")
        if close is None or close <= 0:
            raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
        return float(close)

Do NOT use asyncio, httpx, or pyrate-limiter anywhere.
Do NOT add any imports beyond those listed in step 2.

Commit message: `feat(02): implement token bucket, retry decorator, fetch_quotes (DATA-01/02/03)`
  </action>
  <verify>
    <automated>
      uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry_429 or no_retry_403 or paid_tier" -x -q
      # Must show 0 failed (all T2 tests now GREEN)

      uv run pytest tests/test_smoke.py -x -q
      # Full file must pass — no regressions in Phase 1 tests

      grep -c "_acquire_slot" src/signal_system/data/finnhub_client.py
      # Must return ≥ 3 (definition + call in _fetch_single_quote + call in fetch_spy_close)

      grep -cE "status_code\s*==\s*429" src/signal_system/data/finnhub_client.py
      # Must return ≥ 1 — discriminator is present

      grep -cE "^import asyncio|^from asyncio" src/signal_system/data/finnhub_client.py || true
      # Must return 0

      grep -cE "^import httpx|^from httpx" src/signal_system/data/finnhub_client.py || true
      # Must return 0

      grep -cE "pyrate.limiter" src/signal_system/data/finnhub_client.py pyproject.toml || true
      # Must return 0
    </automated>
  </verify>
  <done>
    - All T2 tests pass (token_bucket, fetch_quotes, retry_429, no_retry_403, paid_tier)
    - All Phase 1 tests still pass (no regressions)
    - _acquire_slot appears ≥ 3 times (definition + two call sites)
    - No asyncio, httpx, or pyrate-limiter in client or pyproject.toml
  </done>
</task>


<!-- ═══════════════════════════════════════════════════════════════
     T4 (RED): Failing tests — paid-tier 404 and fetch_company_news
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto" tdd="true">
  <name>T4 (RED): Failing tests for 404 paid-tier and fetch_company_news</name>
  <files>tests/test_smoke.py</files>
  <behavior>
    - test_paid_tier_404_returns_none: build FinnhubAPIException with status_code=404. Patch _get_client().quote to raise it. Patch _acquire_slot to no-op. Call _fetch_single_quote("UNKNOWN") and assert result is None — called exactly once (no retry).
    - test_company_news_returns_list: patch _acquire_slot to no-op; patch finnhub.Client.company_news to return [{"headline": "Test headline", "source": "Reuters", "datetime": 1700000000, "url": "", "summary": "", "id": 1, "image": "", "category": "", "related": "AAPL"}]; call fetch_company_news("AAPL", date(2026, 5, 1), date(2026, 5, 15)); assert result is a list of length 1; assert result[0]["headline"] == "Test headline"; assert result[0]["source"] == "Reuters".
    - test_company_news_empty_on_no_results: patch company_news to return []; patch _acquire_slot; call fetch_company_news("TICKER", date(2026, 5, 1), date(2026, 5, 15)); assert result == [].
    - test_company_news_returns_empty_on_paid_tier: patch company_news to raise FinnhubAPIException with status_code=403; patch _acquire_slot; assert fetch_company_news("TICKER", date(2026, 5, 1), date(2026, 5, 15)) == [].
    - test_company_news_date_format: patch _acquire_slot; capture args passed to the underlying SDK call; assert _from and to args are strings in "YYYY-MM-DD" format (isoformat). Verify by capturing the call via MagicMock and inspecting call_args.
  </behavior>
  <action>
Add these five test functions to tests/test_smoke.py, after T2's tests. Import `from datetime import date` at the top if not already present.

Reuse the _make_finnhub_exc() helper from T2 (same module-level function — do not duplicate).

For the date format test, patch _fetch_company_news_raw directly via monkeypatch.setattr and capture what from_str/to_str it receives.

Commit message: `test(02): RED — 404 paid-tier, fetch_company_news behaviors`

Confirm RED:
  uv run pytest tests/test_smoke.py -k "paid_tier_404 or company_news" --tb=no -q 2>&1 | grep -E "failed|error"
  # Must show ≥ 1 failed
  </action>
  <verify>
    <automated>
      uv run pytest tests/test_smoke.py -k "paid_tier_404 or company_news" --tb=no -q 2>&1 | grep -E "failed|error"
      # Must show ≥ 1 failed (RED)

      uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry_429 or no_retry_403 or paid_tier" -x -q
      # Prior T2/T3 tests must still pass — no regressions
    </automated>
  </verify>
  <done>Five new test functions exist and at least one fails because fetch_company_news is not yet implemented.</done>
</task>


<!-- ═══════════════════════════════════════════════════════════════
     T5 (GREEN): Implement fetch_company_news
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto" tdd="true">
  <name>T5 (GREEN): Implement fetch_company_news and _fetch_company_news_raw</name>
  <files>src/signal_system/data/finnhub_client.py</files>
  <action>
Append two functions to finnhub_client.py after fetch_quotes. Do NOT modify any existing functions.

1. @_RETRY_DECORATOR
   def _fetch_company_news_raw(ticker: str, from_str: str, to_str: str) -> list[dict]:
       """Rate-limited, retried news fetch. Returns [] on paid-tier 403/404 or missing data."""
       _acquire_slot()
       try:
           result = _get_client().company_news(ticker, from_str, to_str)
       except FinnhubAPIException as exc:
           if exc.status_code in PAID_TIER_STATUS_CODES:
               logger.warning("News unavailable for %r (HTTP %s) — paid tier or unknown, returning []", ticker, exc.status_code)
               return []
           raise
       return result if isinstance(result, list) else []

2. def fetch_company_news(ticker: str, from_date: date, to_date: date) -> list[dict]:
       """Fetch company news headlines for ticker within date range.

       Args:
           ticker: Stock symbol (e.g. "AAPL").
           from_date: Start date (inclusive).
           to_date: End date (inclusive).

       Returns:
           List of news item dicts. Each item includes at minimum 'headline' and 'source'.
           Returns [] if no news, paid-tier error, or exhausted retries.
       """
       try:
           return _fetch_company_news_raw(
               ticker,
               from_date.isoformat(),   # YYYY-MM-DD
               to_date.isoformat(),
           )
       except Exception as exc:
           logger.error("Giving up on news for %r after exhausted retries: %s", ticker, exc)
           return []

Commit message: `feat(02): implement fetch_company_news (DATA-04)`
  </action>
  <verify>
    <automated>
      uv run pytest tests/test_smoke.py -k "paid_tier_404 or company_news" -x -q
      # All T4 tests must now PASS

      uv run pytest tests/test_smoke.py -x -q
      # Full file must pass — no regressions

      grep -c "fetch_company_news" src/signal_system/data/finnhub_client.py
      # Must return ≥ 2 (definition of _fetch_company_news_raw + fetch_company_news)

      grep -cE "^import asyncio|^from asyncio" src/signal_system/data/finnhub_client.py || true
      # Must return 0
    </automated>
  </verify>
  <done>
    - All T4 tests pass (paid_tier_404, company_news variants)
    - All prior tests still pass (0 regressions)
    - fetch_company_news and _fetch_company_news_raw both present in finnhub_client.py
  </done>
</task>


<!-- ═══════════════════════════════════════════════════════════════
     T6: Full integration smoke test
     ═══════════════════════════════════════════════════════════════ -->

<task type="auto">
  <name>T6: Integration smoke test — all surfaces importable, full suite green</name>
  <files>tests/test_smoke.py</files>
  <action>
Add a final integration test function:

  def test_phase2_public_api_importable():
      """All Phase 2 public surfaces are importable and have correct signatures."""
      from signal_system.data.finnhub_client import (
          fetch_spy_close,
          fetch_quotes,
          fetch_company_news,
          PAID_TIER_STATUS_CODES,
      )
      import inspect
      # fetch_quotes accepts list, returns dict
      sig = inspect.signature(fetch_quotes)
      assert "tickers" in sig.parameters
      # fetch_company_news accepts ticker + two dates
      sig2 = inspect.signature(fetch_company_news)
      assert set(sig2.parameters.keys()) == {"ticker", "from_date", "to_date"}
      # PAID_TIER_STATUS_CODES is a frozenset containing 403 and 404
      assert 403 in PAID_TIER_STATUS_CODES
      assert 404 in PAID_TIER_STATUS_CODES

Run the full suite and confirm total test count exceeds Phase 1 baseline (17 tests):
  uv run pytest -x -q 2>&1 | tail -3

Commit message: `test(02): phase integration smoke test — all Phase 2 surfaces verified`
  </action>
  <verify>
    <automated>
      uv run pytest -x -q
      # Must exit 0

      uv run pytest --co -q 2>&1 | tail -3
      # Test count must be > 17 (Phase 1 baseline was 17 tests)

      python -c "
      from signal_system.data.finnhub_client import fetch_spy_close, fetch_quotes, fetch_company_news, PAID_TIER_STATUS_CODES
      print('All Phase 2 imports OK')
      assert 403 in PAID_TIER_STATUS_CODES
      assert 404 in PAID_TIER_STATUS_CODES
      print('PAID_TIER_STATUS_CODES correct')
      "
    </automated>
  </verify>
  <done>
    - Full suite passes with 0 failures
    - Total test count exceeds 17 (Phase 1 baseline)
    - All four Phase 2 public names importable from finnhub_client
    - PAID_TIER_STATUS_CODES frozenset contains both 403 and 404
  </done>
</task>

</tasks>


<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Finnhub API → finnhub_client.py | All HTTP responses (including error codes and response bodies) cross here; response data is untrusted |
| ticker symbols → finnhub_client.py | Ticker strings originate from universe.csv (operator-controlled) but pass through to API calls |
| Finnhub headlines → caller | Raw headline text returned from fetch_company_news is untrusted; sanitization is Phase 3 (CLFY-01) responsibility, NOT this module |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-02-01 | Tampering | Ticker symbols passed to `quote()` and `company_news()` | accept | Finnhub SDK encodes all params via `requests` URL encoding. Universe tickers originate from operator-maintained `universe.csv`, not user input. No additional validation added here. |
| T-02-02 | Denial of Service | Rate-limit exhaustion — 429 storm consuming retries | mitigate | `wait_exponential(min=2, max=60)` + `stop_after_attempt(5)` bounds per-ticker retry cost to ~240s worst case. Token bucket enforces ≤55 calls/min preemptively. |
| T-02-03 | Information Disclosure | API key logged in error messages | mitigate | Logger calls use only `ticker` and `exc.status_code` — never `config.FINNHUB_API_KEY`. Validated via code review of all logger.* call sites. |
| T-02-04 | Elevation of Privilege | Paid-tier endpoint silent pass-through returning zero-scores | mitigate | `PAID_TIER_STATUS_CODES = frozenset({403, 404})` catches both status codes; logs WARNING; returns `None` or `[]`; caller (Discovery Agent, DISC-02) enforces no-score-on-None policy. |
| T-02-05 | Tampering | Headline content containing prompt injection characters | transfer | Raw headlines are returned as-is from `fetch_company_news`. Sanitization (strip control chars, 500-char cap, `<headline>` delimiters) is CLFY-01's responsibility in Phase 3. This boundary is documented in the Phase 3 plan. |
| T-02-SC | Tampering | Supply chain — `tenacity` PyPI package | mitigate | T1 verifies PyPI JSON shows `jd/tenacity` as source repo before `uv add`. Package has >10-year history (2016+), high download count, well-known in ecosystem. `[ASSUMED]` per research — human-verified in T1 action before install. |
</threat_model>


<risk_register>
## Empirical Validation Required

The following assumptions from the research phase (02-RESEARCH.md) MUST be validated on first live API run. The code handles all cases gracefully, but the behavior may differ from expectation:

| Risk ID | Assumption | Code Behavior if Wrong | Validation Step |
|---------|-----------|----------------------|-----------------|
| R-02-A1 | `quote` and `company-news` are Finnhub free-tier endpoints | If paid: all tickers return 403 → fetch_quotes returns all None → agents score nothing | First Discovery Agent run (Phase 4): check logs for mass WARNING lines; if present, scoring formula needs adjustment |
| R-02-A2 | Unknown tickers return `{"c": 0, ...}` not a 404 FinnhubAPIException | If they raise 404: code returns None (correct by coincidence — paid-tier handler catches it), but `c <= 0` guard is never reached | Log DEBUG output on first run with novel tickers; verify no unexpected 404s for known universe tickers |
| R-02-A3 | `company_news` date params use `YYYY-MM-DD` string format | If wrong format: HTTP 400 → FinnhubAPIException → `_is_transient_error` returns False → raises → `fetch_company_news` catches and returns `[]` | First news-morning run (Phase 3): verify non-empty results for a ticker with known recent news |
| R-02-A4 | Finnhub returns HTTP 429 (not 503 or other) on rate limit | If 503: `_is_transient_error` returns False → call fails immediately without retry → fetch_quotes returns None for that ticker | Monitor logs during first high-volume Discovery run; if 503s appear, add status_code=503 to `_is_transient_error` |
| R-02-A5 | `company_news` returns `[]` for no-news (not None) | If None: `isinstance(result, list)` guard in `_fetch_company_news_raw` catches it, returns `[]` — low risk | Covered by T4 test_company_news_empty_on_no_results; runtime validation on first news-morning run |
</risk_register>


<goal_backward_check>
## Requirements → Task Coverage

| Req ID | Requirement | Task(s) | Verified By |
|--------|------------|---------|-------------|
| DATA-01 | Bulk quote fetch with preemptive token bucket (≤55 calls/min) | T2 (RED), T3 (GREEN) | test_token_bucket_calls_sleep, test_fetch_quotes_returns_dict, grep: _acquire_slot ≥ 3 |
| DATA-02 | Retry 429 via tenacity up to 5 attempts, exponential backoff | T2 (RED), T3 (GREEN) | test_retry_on_429 (assert quote called 5× on exhaustion) |
| DATA-03 | 403/404 → log warning, return None, caller skips | T2 (RED test_no_retry_on_403), T3 (GREEN), T4 (RED test_paid_tier_404), T5 (GREEN) | test_no_retry_on_403, test_paid_tier_404_returns_none, test_company_news_returns_empty_on_paid_tier |
| DATA-04 | fetch_company_news: headlines + source, returns [] on empty or 403/404 | T4 (RED), T5 (GREEN) | test_company_news_returns_list, test_company_news_empty_on_no_results, test_company_news_returns_empty_on_paid_tier |

## Phase 2 Success Criteria → Task Map

| ROADMAP Success Criterion | Covered By | How Verified |
|--------------------------|------------|-------------|
| 1. 100-ticker bulk quote ≤55 calls/min | T2/T3 token bucket tests | test_token_bucket_calls_sleep confirms _acquire_slot enforces interval |
| 2. 429 → retry ≤5 times, retry count logged | T2/T3 | test_retry_on_429 asserts quote called exactly 5×; before_sleep_log logs each retry |
| 3. 403/404 → warning + None + caller skips | T2/T3/T4/T5 | test_no_retry_on_403, test_paid_tier_404_returns_none — assert called once, returns None |
| 4. fetch_company_news returns headline + source | T4/T5 | test_company_news_returns_list asserts result[0]["headline"] and result[0]["source"] |
</goal_backward_check>


<verification>
## Phase-Level Verification

Run after T6 completes:

```bash
# 1. Full suite green with growth above Phase 1 baseline
uv run pytest -x -q
uv run pytest --co -q | tail -3   # confirm > 17 tests

# 2. All public surfaces importable
python -c "
from signal_system.data.finnhub_client import (
    fetch_spy_close, fetch_quotes, fetch_company_news, PAID_TIER_STATUS_CODES
)
assert 403 in PAID_TIER_STATUS_CODES and 404 in PAID_TIER_STATUS_CODES
print('Phase 2 public API: OK')
"

# 3. No forbidden dependencies in client
grep -cE "^import asyncio|^from asyncio" src/signal_system/data/finnhub_client.py || true
# expect 0
grep -cE "^import httpx|^from httpx" src/signal_system/data/finnhub_client.py || true
# expect 0
grep -cE "pyrate.limiter" src/signal_system/data/finnhub_client.py pyproject.toml || true
# expect 0

# 4. Rate-limit discriminator is present (403/404 skip, 429 retry)
grep -cE "status_code\s*==\s*429" src/signal_system/data/finnhub_client.py
# expect ≥ 1
grep -c "PAID_TIER_STATUS_CODES" src/signal_system/data/finnhub_client.py
# expect ≥ 2 (definition + at least one usage)

# 5. fetch_spy_close flows through _acquire_slot
grep -c "_acquire_slot" src/signal_system/data/finnhub_client.py
# expect ≥ 3 (definition + fetch_spy_close call + _fetch_single_quote call)

# 6. tenacity in pyproject.toml
grep -c "tenacity" pyproject.toml
# expect ≥ 1
```
</verification>


<success_criteria>
Phase 2 is complete when:

1. `uv run pytest -x` exits 0 with more than 17 tests collected (Phase 1 baseline was 17)
2. `from signal_system.data.finnhub_client import fetch_spy_close, fetch_quotes, fetch_company_news, PAID_TIER_STATUS_CODES` succeeds
3. `grep -c "tenacity" pyproject.toml` returns ≥ 1
4. `grep -c "_acquire_slot" src/signal_system/data/finnhub_client.py` returns ≥ 3
5. `grep -cE "status_code\s*==\s*429" src/signal_system/data/finnhub_client.py` returns ≥ 1 (discriminator present)
6. `grep -cE "^import asyncio|^from asyncio" src/signal_system/data/finnhub_client.py` returns 0
7. `grep -cE "pyrate.limiter" pyproject.toml src/signal_system/data/finnhub_client.py` returns 0
8. All 5 risk register items (R-02-A1 through R-02-A5) are documented — empirical validation deferred to Phase 3/4 first runs
</success_criteria>


<output>
When complete, create `.planning/phases/02-data-layer/02-SUMMARY.md` using the summary template at `@$HOME/.claude/get-shit-done/templates/summary.md`.
</output>
