from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from signal_system import __main__
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


def test_backfill_advice_outcomes_fills_due_rows(tmp_path, monkeypatch):
    from signal_system.jobs import outcome_backfill

    _ET2 = ZoneInfo("America/New_York")
    db_path = tmp_path / "test_advice_backfill.db"
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    repository.init_db()

    now_et = datetime(2026, 9, 1, 9, 0, tzinfo=_ET2)
    ts_31d_ago = (now_et - timedelta(days=31)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO advice (
            advice_id, run_id, timestamp, ticker, account, held,
            verdict, confidence, mom_axis, news_axis, factors_json, flags,
            shadow_mode, acted, acted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "adv-1", "run-1", ts_31d_ago, "FCX", "schwab_main", 1,
        "SELL", 0.8, "bearish", "bearish", "{}", "",
        1, 1, ts_31d_ago,
    ))
    conn.commit()
    conn.close()

    fetch_close = MagicMock(return_value=35.5)
    result = outcome_backfill.backfill_advice_outcomes(
        now_et=now_et, fetch_close_on_date=fetch_close
    )

    assert result.filled_30d == 1
    assert result.filled_90d == 0

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT outcome_price_30d FROM advice WHERE advice_id = 'adv-1'"
    ).fetchone()
    conn.close()
    assert row[0] == 35.5


def test_outcome_backfill_job_is_registered(tmp_path):
    """Improvement 1b: outcome-backfill is in JOBS and runnable via CLI."""
    assert "outcome-backfill" in __main__.JOBS


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
