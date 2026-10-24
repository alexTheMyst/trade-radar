from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from signal_system import __main__
from signal_system.data import yahoo_client
from signal_system.state import repository


_ET = ZoneInfo("America/New_York")


def _insert_signal(
    db: str,
    *,
    alert_id: str,
    ticker: str | None,
    timestamp: datetime,
    routing_status: str | None = "DELIVERED",
    acted: int | None = None,
    outcome_price_30d: float | None = None,
    outcome_price_90d: float | None = None,
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO signals (
            alert_id, timestamp, agent, severity, ticker, title, body, score,
            routing_status, acted, acted_at, user_note,
            outcome_price_30d, outcome_price_90d
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_id,
            timestamp.isoformat(),
            "discovery_agent",
            "INFORMATIONAL",
            ticker,
            f"{alert_id} title",
            "body",
            1.0,
            routing_status,
            acted,
            timestamp.isoformat() if acted is not None else None,
            "annotated" if acted is not None else None,
            outcome_price_30d,
            outcome_price_90d,
        ),
    )
    conn.commit()
    conn.close()


def _read_outcomes(db: str) -> dict[str, tuple[float | None, float | None]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        """
        SELECT alert_id, outcome_price_30d, outcome_price_90d
        FROM signals
        ORDER BY alert_id
        """
    ).fetchall()
    conn.close()
    return {alert_id: (price_30d, price_90d) for alert_id, price_30d, price_90d in rows}


def test_outcome_backfill_uses_historical_closes_at_horizon_dates(tmp_path, monkeypatch):
    """B1 fix: 30d and 90d outcomes come from dates, not the same quote."""
    from signal_system.jobs import outcome_backfill

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    now_et = datetime(2026, 6, 19, 9, 0, tzinfo=_ET)
    _insert_signal(
        str(db_path),
        alert_id="after-30d",
        ticker="AAPL",
        timestamp=now_et - timedelta(days=31),
        routing_status="DELIVERED",
        acted=1,
    )
    _insert_signal(
        str(db_path),
        alert_id="after-90d",
        ticker="MSFT",
        timestamp=now_et - timedelta(days=95),
        routing_status="SUPPRESSED",
        acted=0,
    )
    _insert_signal(
        str(db_path),
        alert_id="too-early",
        ticker="NVDA",
        timestamp=now_et - timedelta(days=10),
        routing_status="DELIVERED",
        acted=1,
    )
    # no-feedback: routable but never reviewed — should now be measured
    _insert_signal(
        str(db_path),
        alert_id="no-feedback",
        ticker="TSLA",
        timestamp=now_et - timedelta(days=95),
        routing_status="DELIVERED",
        acted=None,
    )

    fetch_close = MagicMock(
        side_effect=lambda ticker, target: {"AAPL": 101.5, "MSFT": 222.25, "TSLA": 300.0}[ticker]
    )

    result = outcome_backfill.backfill_due_outcomes(
        now_et=now_et, fetch_close_on_date=fetch_close
    )

    assert result.filled_30d == 3  # AAPL, MSFT, TSLA
    assert result.filled_90d == 2  # MSFT, TSLA
    outcomes = _read_outcomes(str(db_path))
    assert outcomes["after-30d"] == (101.5, None)
    assert outcomes["after-90d"] == (222.25, 222.25)
    assert outcomes["no-feedback"] == (300.0, 300.0)
    assert outcomes["too-early"] == (None, None)


def test_outcome_backfill_is_idempotent_and_does_not_overwrite_existing_values(tmp_path, monkeypatch):
    from signal_system.jobs import outcome_backfill

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    now_et = datetime(2026, 8, 20, 9, 0, tzinfo=_ET)
    _insert_signal(
        str(db_path),
        alert_id="existing-30d",
        ticker="AAPL",
        timestamp=now_et - timedelta(days=95),
        routing_status="DELIVERED",
        acted=1,
        outcome_price_30d=55.0,
    )

    first_fetch = MagicMock(return_value=88.0)
    first_result = outcome_backfill.backfill_due_outcomes(
        now_et=now_et, fetch_close_on_date=first_fetch
    )

    assert first_result.filled_30d == 0
    assert first_result.filled_90d == 1
    assert _read_outcomes(str(db_path))["existing-30d"] == (55.0, 88.0)

    second_fetch = MagicMock(return_value=99.0)
    second_result = outcome_backfill.backfill_due_outcomes(
        now_et=now_et, fetch_close_on_date=second_fetch
    )

    assert second_result.filled_30d == 0
    assert second_result.filled_90d == 0
    second_fetch.assert_not_called()
    assert _read_outcomes(str(db_path))["existing-30d"] == (55.0, 88.0)


def test_outcome_backfill_job_is_registered(tmp_path):
    """Improvement 1b: outcome-backfill is in JOBS and runnable via CLI."""
    assert "outcome-backfill" in __main__.JOBS


def test_backfill_benchmark_command_importable():
    """backfill-benchmark is a special-case CLI command (no JOBS entry)."""
    assert hasattr(__main__, "backfill_benchmark")


def test_default_fetch_close_on_date_handles_tz_naive_index(monkeypatch):
    """Regression: tz-naive DataFrame index vs tz-aware target must not raise TypeError."""
    import pandas as pd
    from signal_system.data import yahoo_client
    from signal_system.jobs.outcome_backfill import _default_fetch_close_on_date

    target = datetime(2026, 5, 1, 0, 0, tzinfo=_ET)  # tz-aware

    # Build a tz-naive DataFrame (what yfinance actually returns)
    dates = pd.date_range("2026-05-01", periods=5, freq="B")  # tz-naive
    df = pd.DataFrame({"Close": [100.0, 101.0, 102.0, 103.0, 104.0],
                       "High": [101.0]*5, "Low": [99.0]*5}, index=dates)
    assert df.index.tz is None  # confirm the fixture is tz-naive

    monkeypatch.setattr(yahoo_client, "fetch_history", lambda tickers, days: {"AAPL": df})

    # Must not raise TypeError; should return the close on the first day >= target
    result = _default_fetch_close_on_date("AAPL", target)
    assert result == 100.0


def test_outcome_backfill_monitoring_not_measured(tmp_path, monkeypatch):
    """MONITORING signals are excluded from outcome measurement."""
    from signal_system.jobs import outcome_backfill

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    now_et = datetime(2026, 6, 19, 9, 0, tzinfo=_ET)
    _insert_signal(
        str(db_path),
        alert_id="monitoring-sig",
        ticker="AAPL",
        timestamp=now_et - timedelta(days=95),
        routing_status="MONITORING",
        acted=None,
    )

    fetch_close = MagicMock(return_value=101.5)
    result = outcome_backfill.backfill_due_outcomes(
        now_et=now_et, fetch_close_on_date=fetch_close
    )

    assert result.filled_30d == 0
    assert result.filled_90d == 0
    fetch_close.assert_not_called()


def _benchmark_df(start: str = "2026-04-01", periods: int = 60) -> "object":
    import pandas as pd
    dates = pd.date_range(start, periods=periods, freq="B")
    return pd.DataFrame({"Close": [500.0 + i for i in range(periods)]}, index=dates)


def _read_benchmarks(db: str) -> list[tuple[str, str, float, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, date, close, source FROM benchmark_prices ORDER BY date"
    ).fetchall()
    conn.close()
    return rows


def test_backfill_benchmark_upserts_spy_history_since_earliest_signal(tmp_path, monkeypatch):
    from signal_system.jobs import outcome_backfill

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    # Earliest signal is 2026-05-15
    _insert_signal(
        str(db_path),
        alert_id="oldest-signal",
        ticker="AAPL",
        timestamp=datetime(2026, 5, 15, 9, 0, tzinfo=_ET),
        routing_status="DELIVERED",
    )

    df = _benchmark_df(start="2026-04-01", periods=60)  # covers Apr–Jun
    monkeypatch.setattr(
        yahoo_client, "fetch_history",
        lambda tickers, days: {"SPY": df} if tickers == ["SPY"] else {},
    )

    result = outcome_backfill.backfill_benchmark()

    rows = _read_benchmarks(str(db_path))
    assert all(row[0] == "SPY" for row in rows)
    assert all(row[3] == "yfinance_backfill" for row in rows)
    # Backfill starts at the earliest signal date (2026-05-15), not earlier
    assert rows[0][1] == "2026-05-15"
    # The 2026-05-15 close from the fabricated df: idx 32 (2026-04-01 + 32 business days)
    assert rows[0][2] == 532.0
    assert result > 0


def test_backfill_benchmark_overwrites_finnhub_quote_row(tmp_path, monkeypatch):
    from signal_system.jobs import outcome_backfill as ob

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    _insert_signal(
        str(db_path),
        alert_id="oldest-signal",
        ticker="AAPL",
        timestamp=datetime(2026, 5, 15, 9, 0, tzinfo=_ET),
        routing_status="DELIVERED",
    )
    # daily-close already wrote a quote row for a backfilled date
    repository.upsert_benchmark_price(
        ticker="SPY", date_iso="2026-05-15", close=999.0, source="finnhub_quote"
    )

    df = _benchmark_df(start="2026-05-01", periods=30)
    monkeypatch.setattr(yahoo_client, "fetch_history", lambda tickers, days: {"SPY": df})

    ob.backfill_benchmark()

    rows = _read_benchmarks(str(db_path))
    assert rows[0][1] == "2026-05-15"
    assert rows[0][3] == "yfinance_backfill"
    assert rows[0][2] == 510.0  # 2026-05-15 idx 10 in the May df


def test_backfill_benchmark_no_signal_history_returns_zero(tmp_path, monkeypatch):
    from signal_system.jobs import outcome_backfill as ob

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    result = ob.backfill_benchmark()
    assert result == 0
    assert _read_benchmarks(str(db_path)) == []
