"""yahoo_client.py — batch historical OHLCV via yfinance.

Used by the Discovery Agent for multi-day momentum calculation.
Finnhub /stock/candle is 403 on free tier; Yahoo Finance provides
free historical daily candles with no API key.
"""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_history(tickers: list[str], days: int = 25) -> dict[str, pd.DataFrame]:
    """Batch-download daily OHLCV for tickers, return {ticker: DataFrame}.

    Args:
        tickers: List of ticker symbols to fetch.
        days: Calendar days of history to request (default 25 to get ~20 trading days).

    Returns:
        Dict mapping ticker to DataFrame with columns [Close, High, Low].
        Tickers that returned no data are excluded from the result.
        Returns empty dict on any download failure (never raises).
    """
    if not tickers:
        return {}

    try:
        raw = yf.download(
            tickers,
            period=f"{days}d",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=False,
        )
    except Exception as exc:
        logger.error("yfinance download failed: %s", exc)
        return {}

    if raw.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}

    # With group_by="ticker", yfinance returns MultiIndex columns keyed by
    # ticker for both single- and multi-ticker downloads, so access each
    # ticker's frame the same way. (A single-ticker special case using
    # raw[["Close","High","Low"]] breaks on the MultiIndex.)
    for ticker in tickers:
        try:
            df = raw[ticker][["Close", "High", "Low"]].dropna()
            if not df.empty:
                result[ticker] = df
        except (KeyError, TypeError):
            logger.debug("No data for %r in yfinance response", ticker)
            continue

    return result
