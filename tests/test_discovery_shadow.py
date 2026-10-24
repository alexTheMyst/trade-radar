"""Plan 12d — Discovery shadow mode is explicit, measured, and never delivered."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from signal_system.discovery.discovery_agent import ScoreResult
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

ET = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


@contextmanager
def _noop_heartbeat(*_a, **_k):
    yield


def _sig(ticker: str, score: float) -> Signal:
    ts = datetime(2026, 10, 23, 16, 15, tzinfo=ET)
    return Signal(
        ticker=ticker,
        score=score,
        severity="INFORMATIONAL",
        agent="discovery_agent",
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), "discovery", "discovery_agent"),
        title=f"{ticker}: Discovery score {score:.0f}",
    )


def test_shadow_mode_persists_suppressed_shadow_rows_and_never_stages(db):
    from signal_system.jobs import discovery

    signals = [_sig("RTX", 99.6), _sig("MU", 88.0), _sig("CDE", 74.0)]

    with patch("signal_system.jobs.discovery.config.DISCOVERY_MODE", "shadow"), \
         patch("signal_system.jobs.discovery._now_et", return_value=datetime(2026, 10, 23, 16, 15, tzinfo=ET)), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["RTX", "MU", "CDE"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=["MU"]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=ScoreResult(signals=signals)), \
         patch("signal_system.jobs.discovery.stage_signals") as mock_stage, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        discovery.run()

    mock_stage.assert_not_called()
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, routing_status, suppression_reason FROM signals ORDER BY ticker"
    ).fetchall()
    conn.close()
    assert rows == [
        ("CDE", "SUPPRESSED", "shadow"),
        ("MU", "SUPPRESSED", "shadow"),
        ("RTX", "SUPPRESSED", "shadow"),
    ]
    body = mock_send.call_args.args[0]
    assert "Shadow mode: scanned 3, signaled 3, 0 eligible for delivery." in body
    assert "Top 3: RTX 100, MU 88, CDE 74" in body
    assert repository.count_shadow_signals_for_date("2026-10-23") == 3


def test_shadow_rows_are_not_outscored(db):
    from signal_system.jobs import discovery

    with patch("signal_system.jobs.discovery.config.DISCOVERY_MODE", "shadow"), \
         patch("signal_system.jobs.discovery._now_et", return_value=datetime(2026, 10, 23, 16, 15, tzinfo=ET)), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["RTX"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=ScoreResult(signals=[_sig("RTX", 99.6)])), \
         patch("signal_system.jobs.discovery.telegram_sender.send_message"):
        discovery.run()

    conn = sqlite3.connect(db)
    reasons = conn.execute(
        "SELECT DISTINCT suppression_reason FROM signals WHERE agent='discovery_agent'"
    ).fetchall()
    conn.close()
    assert reasons == [("shadow",)]
