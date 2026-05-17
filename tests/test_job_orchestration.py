from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import call, patch

import pytest

from signal_system.data import universe
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_core_holdings_filters_to_core_only_and_preserves_csv_order(tmp_path, monkeypatch):
    csv_path = tmp_path / "universe.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "core_holding", "k1_etf"])
        writer.writeheader()
        writer.writerow({"ticker": " spy ", "core_holding": "1", "k1_etf": "0"})
        writer.writerow({"ticker": "QQQ", "core_holding": "0", "k1_etf": "0"})
        writer.writerow({"ticker": "k1x", "core_holding": "1", "k1_etf": "1"})
        writer.writerow({"ticker": "AAPL", "core_holding": "1", "k1_etf": "0"})

    monkeypatch.setattr(universe, "UNIVERSE_PATH", csv_path)

    assert universe.get_core_holdings() == ["SPY", "AAPL"]


def test_latest_successful_run_date_returns_newest_success_only(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        """
        INSERT INTO runs (run_id, job, started_at, ended_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("run-1", "daily-close", "2026-05-14T16:00:00-04:00", "2026-05-14T16:01:00-04:00", "success"),
            ("run-2", "daily-close", "2026-05-15T16:00:00-04:00", "2026-05-15T16:01:00-04:00", "failed"),
            ("run-3", "daily-close", "2026-05-16T16:00:00-04:00", "2026-05-16T16:01:00-04:00", "success"),
            ("run-4", "daily-close", "2026-05-17T16:00:00-04:00", None, "running"),
            ("run-5", "news-morning", "2026-05-18T08:00:00-04:00", "2026-05-18T08:01:00-04:00", "success"),
        ],
    )
    conn.commit()
    conn.close()

    assert repository.get_latest_successful_run_date("daily-close") == date(2026, 5, 16)


def test_latest_successful_run_date_returns_none_when_absent(db):
    assert repository.get_latest_successful_run_date("daily-close") is None


def _sig(
    *,
    ticker: str,
    severity: str = "INFORMATIONAL",
    agent: str = "news_classifier",
    score: float = 0.75,
) -> Signal:
    timestamp = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc)
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=timestamp,
        alert_id=compute_alert_id(ticker, "2026-05-19", f"rule-{ticker}", agent),
        title=f"{ticker}: important update",
        body=f"{ticker} detailed body",
    )


def test_routed_persistence_stores_every_tuple_with_demotions():
    from signal_system.jobs.common import persist_routed_signals

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    suppressed = _sig(ticker="MSFT", severity="INFORMATIONAL", score=0.71)

    with patch("signal_system.state.repository.insert_signal") as mock_insert:
        summary = persist_routed_signals(
            [
                (delivered, "DELIVERED", None),
                (suppressed, "SUPPRESSED", "outscored"),
            ]
        )

    assert summary.delivered_signals == [delivered]
    assert summary.status_counts == {"DELIVERED": 1, "SUPPRESSED": 1, "MONITORING": 0}
    mock_insert.assert_has_calls(
        [
            call(delivered, routing_status="DELIVERED", demoted_from=None),
            call(suppressed, routing_status="SUPPRESSED", demoted_from="outscored"),
        ]
    )


def test_shared_digest_details_only_delivered_and_counts_non_delivered():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        delivered_signals=[delivered],
        status_counts={"DELIVERED": 1, "SUPPRESSED": 2, "MONITORING": 1},
    )

    assert "News Morning" in payload.subject
    assert "Scanned 3 tickers, 1 alert" in payload.body
    assert "AAPL: important update" in payload.body
    assert "AAPL detailed body" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 1" in payload.body


def test_shared_digest_zero_alert_confirmation():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=4,
        delivered_signals=[],
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 3},
    )

    assert "Scanned 4 tickers, 0 alerts" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 3" in payload.body
