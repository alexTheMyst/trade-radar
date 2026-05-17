from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock, call
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
    acted: int | None,
    outcome_price_30d: float | None = None,
    outcome_price_90d: float | None = None,
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO signals (
            alert_id, timestamp, agent, severity, ticker, title, body, score,
            acted, acted_at, user_note, outcome_price_30d, outcome_price_90d
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def test_outcome_backfill_respects_thresholds_and_stays_internal(tmp_path, monkeypatch):
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
        acted=1,
    )
    _insert_signal(
        str(db_path),
        alert_id="after-90d",
        ticker="MSFT",
        timestamp=now_et - timedelta(days=95),
        acted=0,
    )
    _insert_signal(
        str(db_path),
        alert_id="too-early",
        ticker="NVDA",
        timestamp=now_et - timedelta(days=10),
        acted=1,
    )
    _insert_signal(
        str(db_path),
        alert_id="no-feedback",
        ticker="TSLA",
        timestamp=now_et - timedelta(days=95),
        acted=None,
    )

    fetch_quote = MagicMock(side_effect=lambda ticker: {"c": {"AAPL": 101.5, "MSFT": 222.25}[ticker]})

    result = outcome_backfill.backfill_due_outcomes(now_et=now_et, fetch_quote=fetch_quote)

    assert result.filled_30d == 2
    assert result.filled_90d == 1
    fetch_quote.assert_has_calls([call("AAPL"), call("MSFT")], any_order=True)
    assert _read_outcomes(str(db_path)) == {
        "after-30d": (101.5, None),
        "after-90d": (222.25, 222.25),
        "no-feedback": (None, None),
        "too-early": (None, None),
    }
    assert "outcome-backfill" not in __main__.JOBS


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
        acted=1,
        outcome_price_30d=55.0,
    )

    first_fetch = MagicMock(return_value={"c": 88.0})
    first_result = outcome_backfill.backfill_due_outcomes(now_et=now_et, fetch_quote=first_fetch)

    assert first_result.filled_30d == 0
    assert first_result.filled_90d == 1
    assert _read_outcomes(str(db_path))["existing-30d"] == (55.0, 88.0)

    second_fetch = MagicMock(return_value={"c": 99.0})
    second_result = outcome_backfill.backfill_due_outcomes(now_et=now_et, fetch_quote=second_fetch)

    assert second_result.filled_30d == 0
    assert second_result.filled_90d == 0
    second_fetch.assert_not_called()
    assert _read_outcomes(str(db_path))["existing-30d"] == (55.0, 88.0)
