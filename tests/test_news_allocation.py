"""Plan 12b — per-ticker round-robin headline allocation, held tickers first."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from signal_system.jobs.news_morning import (
    _MAX_PER_TICKER,
    _MAX_TOTAL_COMPANY,
    _WINDOW_MULTIPLIER_CAP,
    _window_days,
    allocate_headlines,
)

ET = ZoneInfo("America/New_York")
_NOW = datetime(2026, 10, 19, 9, 0, tzinfo=ET)


def _rows(ticker: str, count: int) -> list[tuple[str, dict, datetime]]:
    return [
        (ticker, {"id": f"{ticker}-{i}", "headline": f"{ticker} story {i}"}, _NOW - timedelta(minutes=i))
        for i in range(count)
    ]


def test_low_volume_held_ticker_survives_nvda_flood():
    held = _rows("BTG", 1)
    flood = _rows("NVDA", 200)

    kept, overflow = allocate_headlines(held + flood, held={"BTG"}, window_days=3)

    assert ("BTG", {"id": "BTG-0", "headline": "BTG story 0"}, held[0][2]) in kept
    assert len(kept) <= _MAX_TOTAL_COMPANY
    assert len(kept) + len(overflow) == 201


def test_held_bucket_gets_item_one_before_nonheld_gets_item_two():
    held = _rows("MU", 2)
    other = _rows("NVDA", 2)

    kept, _overflow = allocate_headlines(held + other, held={"MU"}, window_days=1)

    assert [row[0] for row in kept] == ["MU", "NVDA", "MU", "NVDA"]


def test_ceiling_stops_at_120_and_per_ticker_cap_holds():
    rows = []
    for index in range(30):
        rows.extend(_rows(f"T{index:02d}", 10))

    kept, overflow = allocate_headlines(rows, held=set(), window_days=1)

    assert len(kept) == _MAX_TOTAL_COMPANY
    counts: dict[str, int] = {}
    for ticker, _item, _dt in kept:
        counts[ticker] = counts.get(ticker, 0) + 1
    assert max(counts.values()) <= _MAX_PER_TICKER
    assert len(kept) + len(overflow) == 300


def test_window_days_uses_elapsed_time_not_trading_calendar():
    assert _window_days(_NOW - timedelta(hours=17), _NOW) == 1

    friday_close = datetime(2026, 10, 16, 16, 0, tzinfo=ET)
    monday_open = datetime(2026, 10, 19, 9, 0, tzinfo=ET)
    assert _window_days(friday_close, monday_open) == 3


def test_ten_day_window_clamps_per_ticker_multiplier():
    rows = _rows("NVDA", 100)
    kept, _overflow = allocate_headlines(rows, held=set(), window_days=10)

    assert len(kept) == _MAX_PER_TICKER * _WINDOW_MULTIPLIER_CAP
