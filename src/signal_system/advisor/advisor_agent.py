"""Advisor Agent orchestration.

produce_advice() is the main entry point. All external I/O (price history,
live quotes, news signals, discovery candidates) is injected via callables
so unit tests run without network or DB access.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from signal_system import config
from signal_system.advisor.verdict_engine import (
    HISTORY_DAYS,
    NEWS_LOOKBACK_DAYS,
    compute_verdict,
)
from signal_system.advisor.rationale import generate_rationale

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

MAX_NEW_BUY_CANDIDATES: int = 5


def _advice_id(ticker: str, account: str | None, d: date) -> str:
    raw = f"{ticker}:{d.isoformat()}:{account or 'none'}:advisor"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _compute_sma(closes: list[float], days: int) -> float | None:
    if len(closes) < days:
        return None
    return sum(closes[-days:]) / days


def compute_news_net(news_signals: list[tuple[float, float]]) -> float:
    """Net direction x confidence. Returns 0.0 when list is empty."""
    if not news_signals:
        return 0.0
    return sum(d * c for d, c in news_signals) / len(news_signals)


def has_thesis_break(news_signals: list[tuple[float, float]]) -> bool:
    """True if any high-confidence (>=0.85) negative (direction=-1.0) news signal exists."""
    return any(direction == -1.0 and conf >= 0.85 for direction, conf in news_signals)


def _no_data_row(
    ticker: str, account: str | None, run_id: str,
    now: datetime, thesis_version_hash: str, shadow_mode: bool,
) -> dict:
    return {
        "advice_id": _advice_id(ticker, account, now.date()),
        "run_id": run_id,
        "timestamp": now.isoformat(),
        "ticker": ticker,
        "account": account,
        "held": True,
        "verdict": "NO_DATA",
        "confidence": 0.0,
        "mom_axis": "neutral",
        "news_axis": "neutral",
        "factors_json": "{}",
        "flags": "no_data",
        "rationale": f"{ticker}: insufficient price history -- manual review required.",
        "rationale_source": "template",
        "model_version": config.ANTHROPIC_MODEL,
        "thesis_version_hash": thesis_version_hash,
        "signal_price_snapshot": None,
        "shadow_mode": shadow_mode,
    }


def produce_advice(
    *,
    holdings,                                                # list[Holding]
    fetch_history: Callable[[list[str], int], dict[str, pd.DataFrame]],
    fetch_quote: Callable[[str], float | None],
    get_recent_signals: Callable[[str, date], list[tuple[float, float]]],
    get_discovery_candidates: Callable[[date, set[str]], list[dict]],
    thesis_text: str,
    thesis_version_hash: str,
    run_id: str,
    shadow_mode: bool = True,
    today: date | None = None,
) -> list[dict]:
    """Produce advice rows for all holdings and top new-buy candidates.

    Never raises -- degrades per-ticker. An unexpected error on one ticker
    produces a NO_DATA row so the rest of the run continues.
    """
    now = datetime.now(_ET)
    if today is None:
        today = now.date()

    since_news = today - timedelta(days=NEWS_LOOKBACK_DAYS)
    held_tickers: set[str] = {h.ticker for h in holdings}
    advice_rows: list[dict] = []

    # --- Held positions ---
    held_ticker_list = list(dict.fromkeys(h.ticker for h in holdings))  # preserve order, dedupe
    history_map = fetch_history(held_ticker_list, HISTORY_DAYS)

    for holding in holdings:
        ticker = holding.ticker
        try:
            df = history_map.get(ticker)
            if df is None or len(df) < 201:
                log.warning("Insufficient history for held ticker %s (%s rows)",
                            ticker, len(df) if df is not None else 0)
                advice_rows.append(_no_data_row(ticker, holding.account, run_id, now,
                                                thesis_version_hash, shadow_mode))
                continue

            closes: list[float] = df["Close"].tolist()
            sma50 = _compute_sma(closes, 50)
            sma200 = _compute_sma(closes, 200)
            if sma50 is None or sma200 is None:
                advice_rows.append(_no_data_row(ticker, holding.account, run_id, now,
                                                thesis_version_hash, shadow_mode))
                continue

            price = fetch_quote(ticker) or closes[-1]
            close_high_20d = max(closes[-20:])
            close_low_20d = min(closes[-20:])

            news_sigs = get_recent_signals(ticker, since_news)
            net = compute_news_net(news_sigs)
            t_break = has_thesis_break(news_sigs)

            result = compute_verdict(
                price=price, sma50=sma50, sma200=sma200, news_net=net,
                close_high_20d=close_high_20d, close_low_20d=close_low_20d,
                cost_basis=holding.cost_basis, held=True, thesis_break=t_break,
            )

            rationale_text, rationale_source = generate_rationale(
                ticker=ticker, verdict=result.verdict,
                mom_axis=result.mom_axis, news_axis=result.news_axis,
                factors=result.factors, flags=list(result.flags),
                thesis_text=thesis_text, job="advisor",
            )

            advice_rows.append({
                "advice_id": _advice_id(ticker, holding.account, today),
                "run_id": run_id,
                "timestamp": now.isoformat(),
                "ticker": ticker,
                "account": holding.account,
                "held": True,
                "verdict": result.verdict,
                "confidence": result.confidence,
                "mom_axis": result.mom_axis,
                "news_axis": result.news_axis,
                "factors_json": json.dumps(result.factors),
                "flags": ",".join(result.flags),
                "rationale": rationale_text,
                "rationale_source": rationale_source,
                "model_version": config.ANTHROPIC_MODEL,
                "thesis_version_hash": thesis_version_hash,
                "signal_price_snapshot": price,
                "shadow_mode": shadow_mode,
            })

        except Exception:
            log.exception("Unexpected error processing held position %s", ticker)
            advice_rows.append(_no_data_row(ticker, holding.account, run_id, now,
                                            thesis_version_hash, shadow_mode))

    # --- New-buy candidates from Discovery ---
    candidates = get_discovery_candidates(today - timedelta(days=14), held_tickers)
    cand_tickers = [c["ticker"] for c in candidates if c["ticker"] not in held_tickers]
    cand_history = fetch_history(cand_tickers, HISTORY_DAYS)

    new_buy_count = 0
    for cand in candidates:
        if new_buy_count >= MAX_NEW_BUY_CANDIDATES:
            break
        ticker = cand["ticker"]
        if ticker in held_tickers:
            continue
        try:
            df = cand_history.get(ticker)
            if df is None or len(df) < 201:
                continue
            closes = df["Close"].tolist()
            sma50 = _compute_sma(closes, 50)
            sma200 = _compute_sma(closes, 200)
            if sma50 is None or sma200 is None:
                continue
            price = fetch_quote(ticker) or closes[-1]
            close_high_20d = max(closes[-20:])
            close_low_20d = min(closes[-20:])

            news_sigs = get_recent_signals(ticker, since_news)
            net = compute_news_net(news_sigs)

            result = compute_verdict(
                price=price, sma50=sma50, sma200=sma200, news_net=net,
                close_high_20d=close_high_20d, close_low_20d=close_low_20d,
                cost_basis=None, held=False, thesis_break=False,
            )
            if result.verdict != "BUY":
                continue

            rationale_text, rationale_source = generate_rationale(
                ticker=ticker, verdict=result.verdict,
                mom_axis=result.mom_axis, news_axis=result.news_axis,
                factors=result.factors, flags=list(result.flags),
                thesis_text=thesis_text, job="advisor",
            )
            advice_rows.append({
                "advice_id": _advice_id(ticker, None, today),
                "run_id": run_id,
                "timestamp": now.isoformat(),
                "ticker": ticker,
                "account": None,
                "held": False,
                "verdict": result.verdict,
                "confidence": result.confidence,
                "mom_axis": result.mom_axis,
                "news_axis": result.news_axis,
                "factors_json": json.dumps(result.factors),
                "flags": ",".join(result.flags),
                "rationale": rationale_text,
                "rationale_source": rationale_source,
                "model_version": config.ANTHROPIC_MODEL,
                "thesis_version_hash": thesis_version_hash,
                "signal_price_snapshot": price,
                "shadow_mode": shadow_mode,
            })
            new_buy_count += 1

        except Exception:
            log.exception("Error processing candidate ticker %s", ticker)

    return advice_rows
