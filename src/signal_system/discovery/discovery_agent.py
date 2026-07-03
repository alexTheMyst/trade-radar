"""Discovery Agent — scores rotation universe via cross-sectional multi-day momentum.

Uses yfinance for 20-day historical candles (Finnhub /stock/candle is 403 on free tier).
Uses Finnhub /quote for real-time price snapshot only.
Factors: momentum_20d (50), momentum_5d (30), range_vs_20d (20).
Always routes through the alert router (Phase B). Position-weight amplifier
adjusts severity thresholds per ticker.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from signal_system.data.finnhub_client import fetch_quote
from signal_system.data.universe import get_position_weights
from signal_system.data.yahoo_client import fetch_history
from signal_system.models import Signal, compute_alert_id
from signal_system.scoring.weight_amplifier import adjusted_severity
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_W_MOMENTUM_20D: float = 50.0
_W_MOMENTUM_5D: float = 30.0
_W_RANGE: float = 20.0
SCORE_THRESHOLD_ACTION: float = 80.0
SCORE_THRESHOLD_INFORM: float = 60.0
_MIN_TRADING_DAYS: int = 5

_FACTOR_LABELS: dict[str, str] = {
    "momentum_20d": "mom20",
    "momentum_5d": "mom5",
    "range_vs_20d": "range",
}


def _rank_values(values: dict[str, float]) -> dict[str, float]:
    """Rank tickers 1.0 (top) to 0.0 (bottom), alphabetical tiebreak for equal values."""
    if not values:
        return {}
    sorted_items = sorted(values.items(), key=lambda x: (-x[1], x[0]))
    n = len(sorted_items)
    if n == 1:
        return {sorted_items[0][0]: 0.5}
    return {ticker: 1.0 - (i / (n - 1)) for i, (ticker, _) in enumerate(sorted_items)}


def _compute_factors(df: pd.DataFrame) -> dict[str, float] | None:
    """Compute momentum and range factors from a candle DataFrame.

    Returns None if fewer than _MIN_TRADING_DAYS of data.
    Caps input to trailing 21 rows so momentum_20d uses closes[0] (20 true
    returns) and momentum_5d uses closes[n-6] (5 true returns).
    """
    if len(df) < _MIN_TRADING_DAYS:
        return None

    df = df.tail(21)
    n = len(df)

    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values

    current_close = float(closes[-1])
    high_20d = float(highs.max())
    low_20d = float(lows.min())

    start_close_20d = float(closes[0])
    momentum_20d = (current_close - start_close_20d) / start_close_20d if start_close_20d > 0 else 0.0

    idx_5d = max(0, n - 6)
    start_close_5d = float(closes[idx_5d])
    momentum_5d = (current_close - start_close_5d) / start_close_5d if start_close_5d > 0 else 0.0

    range_span = high_20d - low_20d
    range_position = (current_close - low_20d) / range_span if range_span > 0 else 0.0

    return {
        "momentum_20d": momentum_20d,
        "momentum_5d": momentum_5d,
        "range_vs_20d": range_position,
    }


def score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]:
    """Score tickers using cross-sectional multi-day momentum ranking.

    Fetches historical candles via yfinance (batch), computes factors,
    ranks cross-sectionally, applies position-weight severity amplifier.
    Returns list[Signal] for the router.
    """
    tickers_scanned = len(tickers)
    if not tickers:
        repository.update_run_counts(run_id, 0, 0)
        return []

    history = fetch_history(tickers, days=26)
    weights = get_position_weights()

    raw_factors: dict[str, dict[str, float]] = {}
    for ticker in tickers:
        df = history.get(ticker)
        if df is None:
            logger.debug("No candle data for %r — skipping", ticker)
            continue
        factors = _compute_factors(df)
        if factors is None:
            logger.debug("Fewer than %d days for %r — skipping", _MIN_TRADING_DAYS, ticker)
            continue
        raw_factors[ticker] = factors

    if not raw_factors:
        repository.update_run_counts(run_id, tickers_scanned, 0)
        return []

    momentum_20d_ranks = _rank_values({t: f["momentum_20d"] for t, f in raw_factors.items()})
    momentum_5d_ranks = _rank_values({t: f["momentum_5d"] for t, f in raw_factors.items()})
    range_ranks = _rank_values({t: f["range_vs_20d"] for t, f in raw_factors.items()})

    results: list[Signal] = []
    signals_emitted: list[Signal] = []
    timestamp = datetime.now(_ET)

    for ticker in raw_factors:
        factors = [
            ("momentum_20d", _W_MOMENTUM_20D, momentum_20d_ranks[ticker]),
            ("momentum_5d", _W_MOMENTUM_5D, momentum_5d_ranks[ticker]),
            ("range_vs_20d", _W_RANGE, range_ranks[ticker]),
        ]
        weight_sum = sum(w for _, w, _ in factors)
        composite = 100.0 * sum(w * r for _, w, r in factors) / weight_sum

        severity = adjusted_severity(
            score=composite,
            ticker=ticker,
            weights=weights,
            base_thresholds=(SCORE_THRESHOLD_ACTION, SCORE_THRESHOLD_INFORM),
        )

        if severity == "MONITORING":
            continue

        alert_id = compute_alert_id(ticker, date_iso, "discovery", "discovery_agent")

        quote = fetch_quote(ticker)
        price_snapshot = float(quote["c"]) if quote else None

        weights_str = "/".join(str(int(w)) for _, w, _ in factors)
        rank_str = " ".join(f"{_FACTOR_LABELS[name]}={rank:.2f}" for name, _, rank in factors)

        signal = Signal(
            ticker=ticker,
            score=composite,
            severity=severity,
            agent="discovery_agent",
            timestamp=timestamp,
            alert_id=alert_id,
            title=f"{ticker}: Discovery score {composite:.0f}",
            body=f"weights={weights_str} {rank_str}",
            sub_scores={name: rank for name, _, rank in factors},
            model_version=None,
            thesis_version_hash=None,
            signal_price_snapshot=price_snapshot,
        )

        signals_emitted.append(signal)
        results.append(signal)

    repository.update_run_counts(run_id, tickers_scanned, len(signals_emitted))
    return results
