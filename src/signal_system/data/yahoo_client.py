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

    # Yahoo Finance uses dashes for share classes (BRK-B) while the rest of the
    # system (Finnhub, universe.csv) uses dots (BRK.B). Translate for the call
    # and map results back to the original symbols.
    yahoo_to_original: dict[str, str] = {t.replace(".", "-"): t for t in tickers}
    yahoo_tickers = list(yahoo_to_original)

    try:
        raw = yf.download(
            yahoo_tickers,
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

    for yahoo_ticker, original in yahoo_to_original.items():
        try:
            # group_by="ticker" yields MultiIndex (ticker, field) columns even
            # for single-ticker requests on current yfinance; older versions
            # returned flat columns for a single ticker.
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw[yahoo_ticker][["Close", "High", "Low"]].dropna()
            else:
                df = raw[["Close", "High", "Low"]].dropna()
            if not df.empty:
                result[original] = df
        except (KeyError, TypeError):
            logger.debug("No data for %r in yfinance response", original)
            continue

    return result
