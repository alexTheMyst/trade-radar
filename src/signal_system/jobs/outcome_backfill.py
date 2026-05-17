from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from signal_system.data import finnhub_client
from signal_system.state import repository

_ET = ZoneInfo("America/New_York")
OutcomeFetcher = Callable[[str], dict | None]


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


def _extract_close_price(quote: dict | None) -> float | None:
    if quote is None:
        return None
    close = quote.get("c")
    if close is None or close <= 0:
        return None
    return float(close)


def backfill_due_outcomes(
    *,
    now_et: datetime | None = None,
    fetch_quote: OutcomeFetcher = finnhub_client.fetch_quote,
) -> BackfillResult:
    """Fill due 30d/90d outcome prices for acted-on signals.

    This helper is intentionally internal-only for Phase 06. It is importable for
    tests and future scheduling, but is not exposed through the public CLI job
    dispatcher until post-go-live activation.
    """
    effective_now = _normalize_et(now_et or datetime.now(_ET))
    filled_30d = 0
    filled_90d = 0

    for candidate in repository.list_outcome_backfill_candidates():
        fill_30d = candidate.outcome_price_30d is None and _is_due(
            candidate.timestamp,
            now_et=effective_now,
            days=30,
        )
        fill_90d = candidate.outcome_price_90d is None and _is_due(
            candidate.timestamp,
            now_et=effective_now,
            days=90,
        )
        if not fill_30d and not fill_90d:
            continue

        close_price = _extract_close_price(fetch_quote(candidate.ticker))
        if close_price is None:
            continue

        repository.update_signal_outcomes(
            candidate.alert_id,
            outcome_price_30d=close_price if fill_30d else None,
            outcome_price_90d=close_price if fill_90d else None,
        )
        filled_30d += int(fill_30d)
        filled_90d += int(fill_90d)

    return BackfillResult(filled_30d=filled_30d, filled_90d=filled_90d)
