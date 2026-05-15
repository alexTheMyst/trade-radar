"""
Smoke tests for signal-system.

Three tests:
  1. init_db creates signals and runs tables.
  2. insert_signal returns a valid UUID v4 and persists the row.
  3. daily_close.run() completes and writes the expected signal row.

All external I/O (Finnhub, SMTP, healthchecks.io) is mocked.
"""

import sqlite3
import uuid
from unittest.mock import MagicMock, patch

import pytest

from signal_system.state import repository
from signal_system.jobs import daily_close


def test_init_db_creates_tables(tmp_path, monkeypatch):
    """init_db() must create both 'signals' and 'runs' tables."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    conn = sqlite3.connect(tmp_path / "test.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "signals" in tables
    assert "runs" in tables


def test_insert_signal_returns_uuid(tmp_path, monkeypatch):
    """insert_signal() must return a valid UUID v4 and persist the correct row."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    alert_id = repository.insert_signal(
        agent="TEST",
        ticker="SPY",
        title="test signal",
        score=100.0,
    )

    # Raises ValueError if not a valid UUID
    uuid.UUID(alert_id)

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute(
        "SELECT agent, ticker FROM signals WHERE alert_id=?", (alert_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "TEST"
    assert row[1] == "SPY"


def test_daily_close_smoke(tmp_path, monkeypatch):
    """daily_close.run() must write a DAILY_CLOSE signal row with score == SPY close price."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    with (
        patch(
            "signal_system.data.finnhub_client.fetch_spy_close",
            return_value=591.42,
        ),
        patch("signal_system.delivery.email_sender.send_email"),
        patch(
            "httpx.post",
            return_value=MagicMock(raise_for_status=MagicMock()),
        ),
    ):
        daily_close.run()

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute(
        "SELECT ticker, score FROM signals WHERE agent='DAILY_CLOSE'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "SPY"
    assert abs(row[1] - 591.42) < 0.01
