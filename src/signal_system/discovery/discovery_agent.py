"""Discovery Agent — scores rotation universe tickers via cross-sectional factor ranking.

Uses /quote and /company-news (free-tier confirmed). Never sends email.
Phase A: inserts directly to DB with routing_status='MONITORING'. Phase B: returns
list[Signal] to caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.data.finnhub_client import fetch_company_news, fetch_quote
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_W_MOMENTUM: float = 35.0
_W_VOLUME: float = 30.0
_W_RANGE: float = 25.0
_W_NEWS: float = 10.0
SCORE_THRESHOLD_ACTION: float = 80.0
SCORE_THRESHOLD_INFORM: float = 60.0


def _rank_values(values: dict[str, float]) -> dict[str, float]:
    """Rank tickers 1.0 (top) to 0.0 (bottom), alphabetical tiebreak for equal values."""
    if not values:
        return {}
    sorted_items = sorted(values.items(), key=lambda x: (-x[1], x[0]))
    n = len(sorted_items)
    if n == 1:
        return {sorted_items[0][0]: 0.5}
    return {ticker: 1.0 - (i / (n - 1)) for i, (ticker, _) in enumerate(sorted_items)}


def score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]:
    """Score a list of tickers using cross-sectional factor ranking.

    Reads config.DISCOVERY_PHASE at call time (no module-level caching).
    Phase A: inserts signals directly with routing_status='MONITORING', returns [].
    Phase B: returns list[Signal].
    Always calls repository.update_run_counts() before returning.
    """
    tickers_scanned = len(tickers)
    if not tickers:
        repository.update_run_counts(run_id, 0, 0)
        return []

    today = datetime.fromisoformat(date_iso).date()
    news_from = today - timedelta(days=7)

    raw_quotes: dict[str, dict] = {}
    raw_news_counts: dict[str, int] = {}

    # Pass 1 — Fetch
    for ticker in tickers:
        quote = fetch_quote(ticker)
        if quote is None:
            logger.debug("Skipping %r — quote invalid or unavailable", ticker)
            continue
        raw_quotes[ticker] = quote
        news = fetch_company_news(ticker, news_from, today)
        raw_news_counts[ticker] = len(news) if news else 0

    if not raw_quotes:
        repository.update_run_counts(run_id, tickers_scanned, 0)
        return []

    # Pass 2 — Cross-sectional ranking
    range_raw: dict[str, float] = {}
    for t, q in raw_quotes.items():
        h, l, c = q["h"], q["l"], q["c"]
        range_raw[t] = (c - l) / (h - l) if h != l else 0.0

    momentum_ranks = _rank_values({t: q["dp"] for t, q in raw_quotes.items()})
    volume_ranks = _rank_values({t: float(q["v"]) for t, q in raw_quotes.items()})
    range_ranks = _rank_values(range_raw)
    news_ranks = _rank_values({t: float(raw_news_counts.get(t, 0)) for t in raw_quotes})

    # Pass 3 — Score and emit
    results: list[Signal] = []
    signals_emitted: list[Signal] = []
    timestamp = datetime.now(_ET)

    for ticker in raw_quotes:
        m_rank = momentum_ranks[ticker]
        v_rank = volume_ranks[ticker]
        r_rank = range_ranks[ticker]
        n_rank = news_ranks[ticker]

        composite = (
            _W_MOMENTUM * m_rank
            + _W_VOLUME * v_rank
            + _W_RANGE * r_rank
            + _W_NEWS * n_rank
        )

        if composite < SCORE_THRESHOLD_INFORM:
            continue

        severity = (
            "ACTION_REQUIRED" if composite >= SCORE_THRESHOLD_ACTION else "INFORMATIONAL"
        )

        alert_id = compute_alert_id(ticker, date_iso, "discovery", "discovery_agent")

        signal = Signal(
            ticker=ticker,
            score=composite,
            severity=severity,
            agent="discovery_agent",
            timestamp=timestamp,
            alert_id=alert_id,
            title=f"{ticker}: Discovery score {composite:.0f}",
            body=(
                f"weights=35/30/25/10 "
                f"momentum={m_rank:.2f} volume={v_rank:.2f} "
                f"range={r_rank:.2f} news={n_rank:.2f}"
            ),
            sub_scores={
                "price_momentum": m_rank,
                "volume_rank": v_rank,
                "range_position": r_rank,
                "news_activity": n_rank,
            },
            model_version=None,
            thesis_version_hash=None,
            signal_price_snapshot=raw_quotes[ticker]["c"],
        )

        signals_emitted.append(signal)

        if config.DISCOVERY_PHASE == "A":
            repository.insert_signal(signal, routing_status="MONITORING")
        else:
            results.append(signal)

    repository.update_run_counts(run_id, tickers_scanned, len(signals_emitted))
    return results
