"""finnhub_client.py — all Finnhub API access for signal-system."""
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

logger = logging.getLogger(__name__)

# Token bucket state
_MIN_INTERVAL: float = 60.0 / 55  # 1.0909... seconds between calls
_next_call_at: float = 0.0
_lock = threading.Lock()

# Singleton client
_client: finnhub.Client | None = None

PAID_TIER_STATUS_CODES: frozenset[int] = frozenset({403, 404})


def _get_client() -> finnhub.Client:
    global _client
    if _client is None:
        _client = finnhub.Client(api_key=config.FINNHUB_API_KEY)
    return _client


def _acquire_slot() -> None:
    global _next_call_at
    with _lock:
        now = time.monotonic()
        wait = _next_call_at - now
        if wait > 0:
            time.sleep(wait)
        _next_call_at = max(now, _next_call_at) + _MIN_INTERVAL


def _is_transient_error(exc: BaseException) -> bool:
    """Return True only for errors worth retrying (429, connection drops)."""
    if isinstance(exc, FinnhubAPIException):
        return exc.status_code == 429
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return False


_RETRY_DECORATOR = retry(
    retry=retry_if_exception(_is_transient_error),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


@_RETRY_DECORATOR
def _fetch_single_quote(ticker: str) -> dict | None:
    _acquire_slot()
    try:
        response = _get_client().quote(ticker)
    except FinnhubAPIException as exc:
        if exc.status_code in PAID_TIER_STATUS_CODES:
            logger.warning(
                "Quote unavailable for %r (HTTP %s) — paid tier or unknown, skipping",
                ticker,
                exc.status_code,
            )
            return None
        raise  # 429 and other errors re-raise → tenacity sees them
    close = response.get("c")
    if close is None or close <= 0:
        logger.debug("No price data for %r (c=%r) — skipping", ticker, close)
        return None
    return response


def fetch_quotes(tickers: list[str]) -> dict[str, dict | None]:
    """Fetch quotes for a list of tickers. Returns ticker -> quote dict or None. Never raises."""
    results: dict[str, dict | None] = {}
    for ticker in tickers:
        try:
            results[ticker] = _fetch_single_quote(ticker)
        except Exception as exc:
            logger.error("Giving up on %r after exhausted retries: %s", ticker, exc)
            results[ticker] = None
    return results


def fetch_spy_close() -> float:
    """Return SPY close price; raises ValueError on missing or non-positive data."""
    _acquire_slot()
    response = _get_client().quote("SPY")
    close = response.get("c")
    if close is None or close <= 0:
        raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
    return float(close)


@_RETRY_DECORATOR
def _fetch_company_news_raw(ticker: str, from_str: str, to_str: str) -> list[dict]:
    """Rate-limited, retried news fetch. Returns [] on paid-tier 403/404 or missing data."""
    _acquire_slot()
    try:
        result = _get_client().company_news(ticker, from_str, to_str)
    except FinnhubAPIException as exc:
        if exc.status_code in PAID_TIER_STATUS_CODES:
            logger.warning(
                "News unavailable for %r (HTTP %s) — paid tier or unknown, returning []",
                ticker,
                exc.status_code,
            )
            return []
        raise
    return result if isinstance(result, list) else []


def fetch_company_news(ticker: str, from_date: date, to_date: date) -> list[dict]:
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
            from_date.isoformat(),  # YYYY-MM-DD
            to_date.isoformat(),
        )
    except Exception as exc:
        logger.error("Giving up on news for %r after exhausted retries: %s", ticker, exc)
        return []
