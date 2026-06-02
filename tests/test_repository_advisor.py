import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from signal_system.state import repository

_ET = ZoneInfo("America/New_York")


def _insert_news_signal(
    db_path, *, alert_id, ticker, direction, score, routing_status="DELIVERED", severity="INFORMATIONAL"
):
    timestamp = datetime(2026, 5, 25, 10, 0, tzinfo=_ET).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO signals
           (alert_id, timestamp, agent, severity, ticker, title, score, routing_status, direction)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (alert_id, timestamp, "news_classifier", severity, ticker, f"{ticker} news",
         score, routing_status, direction),
    )
    conn.commit()
    conn.close()


def test_get_recent_signals_returns_direction_and_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    db = str(tmp_path / "test.db")

    _insert_news_signal(db, alert_id="s1", ticker="FCX", direction="positive", score=0.8)
    _insert_news_signal(db, alert_id="s2", ticker="FCX", direction="negative", score=0.9)

    results = repository.get_recent_signals("FCX", date(2026, 5, 20))
    assert len(results) == 2
    directions = {d for d, _ in results}
    assert 1.0 in directions
    assert -1.0 in directions
    confs = {c for _, c in results}
    assert 0.8 in confs
    assert 0.9 in confs


def test_get_recent_signals_excludes_monitoring(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    db = str(tmp_path / "test.db")

    _insert_news_signal(
        db, alert_id="m1", ticker="FCX", direction=None, score=None,
        routing_status="MONITORING", severity="MONITORING",
    )

    results = repository.get_recent_signals("FCX", date(2026, 5, 20))
    assert results == []


def test_get_recent_signals_filters_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        """INSERT INTO signals
           (alert_id, timestamp, agent, severity, ticker, title, score, routing_status, direction)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("old", "2026-05-01T10:00:00-04:00", "news_classifier",
         "INFORMATIONAL", "FCX", "old news", 0.7, "DELIVERED", "positive"),
    )
    conn.commit()
    conn.close()

    results = repository.get_recent_signals("FCX", date(2026, 5, 10))
    assert results == []


def test_get_recent_signals_neutral_direction_maps_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    db = str(tmp_path / "test.db")

    _insert_news_signal(db, alert_id="n1", ticker="FCX", direction="neutral", score=0.7)

    results = repository.get_recent_signals("FCX", date(2026, 5, 20))
    assert len(results) == 1
    assert results[0] == (0.0, 0.7)


def test_insert_advice_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    row = {
        "advice_id": "abc123",
        "run_id": "run-1",
        "timestamp": datetime(2026, 6, 1, 10, tzinfo=_ET).isoformat(),
        "ticker": "FCX",
        "account": "schwab_main",
        "held": True,
        "verdict": "HOLD",
        "confidence": 0.5,
        "mom_axis": "bullish",
        "news_axis": "neutral",
        "factors_json": "{}",
        "flags": "",
        "rationale": "Test rationale.",
        "rationale_source": "template",
        "model_version": "claude-sonnet-4-6",
        "thesis_version_hash": "deadbeef",
        "signal_price_snapshot": 42.0,
        "shadow_mode": True,
    }

    first = repository.insert_advice(row)
    second = repository.insert_advice(row)

    assert first is True
    assert second is False


def test_insert_advice_row_is_queryable(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    row = {
        "advice_id": "xyz789",
        "run_id": "run-2",
        "timestamp": datetime(2026, 6, 1, 10, tzinfo=_ET).isoformat(),
        "ticker": "NVDA",
        "account": None,
        "held": False,
        "verdict": "BUY",
        "confidence": 0.8,
        "mom_axis": "bullish",
        "news_axis": "bullish",
        "factors_json": '{"price": 900}',
        "flags": "",
        "rationale": "NVDA BUY rationale.",
        "rationale_source": "claude",
        "model_version": "claude-sonnet-4-6",
        "thesis_version_hash": "abc",
        "signal_price_snapshot": 900.0,
        "shadow_mode": True,
    }
    repository.insert_advice(row)

    conn = sqlite3.connect(tmp_path / "test.db")
    result = conn.execute(
        "SELECT ticker, verdict, held FROM advice WHERE advice_id = 'xyz789'"
    ).fetchone()
    conn.close()
    assert result == ("NVDA", "BUY", 0)  # held=False stored as 0


def test_get_delivered_discovery_signals_excludes_held(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    conn = sqlite3.connect(tmp_path / "test.db")
    for ticker, alert_id in [("NVDA", "d1"), ("FCX", "d2"), ("AAPL", "d3")]:
        conn.execute(
            """INSERT INTO signals
               (alert_id, timestamp, agent, severity, ticker, title, score, routing_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (alert_id, "2026-05-28T10:00:00-04:00", "discovery_agent",
             "INFORMATIONAL", ticker, f"{ticker} momentum", 0.85, "DELIVERED"),
        )
    conn.commit()
    conn.close()

    results = repository.get_delivered_discovery_signals(
        date(2026, 5, 20), excluded_tickers={"FCX"}
    )
    tickers = [r["ticker"] for r in results]
    assert "FCX" not in tickers
    assert "NVDA" in tickers
    assert "AAPL" in tickers


def test_update_advice_outcomes_fills_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    row = {
        "advice_id": "outcome-test",
        "run_id": "r1",
        "timestamp": datetime(2026, 6, 1, tzinfo=_ET).isoformat(),
        "ticker": "FCX",
        "account": "schwab_main",
        "held": True,
        "verdict": "SELL",
        "confidence": 0.8,
        "mom_axis": "bearish",
        "news_axis": "bearish",
        "factors_json": "{}",
        "flags": "",
        "shadow_mode": True,
    }
    repository.insert_advice(row)

    # Set acted so it qualifies for backfill
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("UPDATE advice SET acted = 1 WHERE advice_id = 'outcome-test'")
    conn.commit()
    conn.close()

    repository.update_advice_outcomes("outcome-test", outcome_price_30d=35.0)
    repository.update_advice_outcomes("outcome-test", outcome_price_30d=99.0)  # should not overwrite

    conn = sqlite3.connect(tmp_path / "test.db")
    row_out = conn.execute(
        "SELECT outcome_price_30d FROM advice WHERE advice_id = 'outcome-test'"
    ).fetchone()
    conn.close()
    assert row_out[0] == 35.0  # original value preserved
