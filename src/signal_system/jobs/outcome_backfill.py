from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from signal_system.data import yahoo_client as _yahoo_client
from signal_system.monitoring import heartbeat
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
HistoricalFetcher = Callable[[str, datetime], float | None]


@dataclass(frozen=True, slots=True)
class BackfillResult:
    filled_30d: int = 0
    filled_90d: int = 0


def _normalize_et(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=_ET)
    return timestamp.astimezone(_ET)


def _is_due(timestamp: datetime, *, now_et: datetime, days: int) -> bool:
    return _normalize_et(timestamp) <= now_et - timedelta(days=days)


def _default_fetch_close_on_date(ticker: str, target_date: datetime) -> float | None:
    """Fetch the close price on the first trading day >= target_date.

    Uses yahoo_client.fetch_history with enough calendar days to cover from
    target_date to today. Returns None if no data is available (target_date is
    in the future, or the ticker has no history covering the date).
    """
    target_dt = _normalize_et(target_date)
    days_needed = max(
        30,
        (datetime.now(_ET) - target_dt).days + 10,
    )
    history = _yahoo_client.fetch_history([ticker], days=days_needed)
    df = history.get(ticker)
    if df is None or df.empty:
        return None
    # Daily candles have a tz-naive date index; compare on dates so the
    # tz-aware target doesn't raise (and a tz-aware index still works).
    index = df.index
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    mask = index.normalize() >= pd.Timestamp(target_dt.date())
    matching = df[mask]
    if matching.empty:
        return None
    return float(matching.iloc[0]["Close"])


def backfill_due_outcomes(
    *,
    now_et: datetime | None = None,
    fetch_close_on_date: HistoricalFetcher = _default_fetch_close_on_date,
) -> BackfillResult:
    """Fill due 30d/90d outcome prices with historical closes at signal_date+horizon.

    Only fills a horizon when the target date's close actually exists — never
    uses a same-day quote as a proxy.
    """
    effective_now = _normalize_et(now_et or datetime.now(_ET))
    candidates = repository.list_outcome_backfill_candidates()
    filled_30d = 0
    filled_90d = 0

    for candidate in candidates:
        fill_30d = candidate.outcome_price_30d is None and _is_due(
            candidate.timestamp, now_et=effective_now, days=30
        )
        fill_90d = candidate.outcome_price_90d is None and _is_due(
            candidate.timestamp, now_et=effective_now, days=90
        )
        if not fill_30d and not fill_90d:
            continue

        close_30d: float | None = None
        close_90d: float | None = None

        if fill_30d:
            close_30d = fetch_close_on_date(
                candidate.ticker,
                candidate.timestamp + timedelta(days=30),
            )
        if fill_90d:
            close_90d = fetch_close_on_date(
                candidate.ticker,
                candidate.timestamp + timedelta(days=90),
            )

        if close_30d is None and close_90d is None:
            continue

        repository.update_signal_outcomes(
            candidate.alert_id,
            outcome_price_30d=close_30d,
            outcome_price_90d=close_90d,
        )
        filled_30d += 1 if close_30d is not None else 0
        filled_90d += 1 if close_90d is not None else 0

    return BackfillResult(filled_30d=filled_30d, filled_90d=filled_90d)


def backfill_advice_outcomes(
    *,
    now_et: datetime | None = None,
    fetch_close_on_date: HistoricalFetcher = _default_fetch_close_on_date,
) -> BackfillResult:
    """Fill due 30d/90d outcome prices for advice rows with historical closes.

    Mirrors backfill_due_outcomes() but operates on the advice table.
    """
    effective_now = _normalize_et(now_et or datetime.now(_ET))
    candidates = repository.list_advice_backfill_candidates()
    filled_30d = 0
    filled_90d = 0

    for candidate in candidates:
        fill_30d = candidate.outcome_price_30d is None and _is_due(
            candidate.timestamp, now_et=effective_now, days=30
        )
        fill_90d = candidate.outcome_price_90d is None and _is_due(
            candidate.timestamp, now_et=effective_now, days=90
        )
        if not fill_30d and not fill_90d:
            continue

        close_30d: float | None = None
        close_90d: float | None = None

        if fill_30d:
            close_30d = fetch_close_on_date(
                candidate.ticker,
                candidate.timestamp + timedelta(days=30),
            )
        if fill_90d:
            close_90d = fetch_close_on_date(
                candidate.ticker,
                candidate.timestamp + timedelta(days=90),
            )

        if close_30d is None and close_90d is None:
            continue

        repository.update_advice_outcomes(
            candidate.advice_id,
            outcome_price_30d=close_30d,
            outcome_price_90d=close_90d,
        )
        filled_30d += 1 if close_30d is not None else 0
        filled_90d += 1 if close_90d is not None else 0

    return BackfillResult(filled_30d=filled_30d, filled_90d=filled_90d)


def run() -> None:
    """Scheduled outcome-backfill job (heartbeat-wrapped, writable to DB)."""
    run_id = repository.insert_run("outcome-backfill")
    try:
        with heartbeat.heartbeat():
            signals_result = backfill_due_outcomes()
            advice_result = backfill_advice_outcomes()
            logger.info(
                "outcome-backfill complete: %d signals 30d, %d signals 90d | "
                "%d advice 30d, %d advice 90d",
                signals_result.filled_30d, signals_result.filled_90d,
                advice_result.filled_30d, advice_result.filled_90d,
            )
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
