"""Tests for the scheduled sentinel job.

Patches the job module's data-fetching attrs (fetch_history, fetch_quotes)
and holdings loader; exercises routing, cooldown, escalation, and failure
paths against a tmp DB.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from signal_system.data.holdings import EmptyHoldingsError, Holding
from signal_system.models import Signal
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


@contextmanager
def _noop_heartbeat(*_a, **_k):
    yield


def _df(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"Close": closes, "High": closes, "Low": closes},
        index=index,
    )


def _run_sentinel(monkeypatch, *, holdings, history_map, quotes=None, heartbeat=_noop_heartbeat):
    from signal_system.jobs import sentinel as sentinel_job

    monkeypatch.setattr(sentinel_job, "require_non_empty_holdings", lambda: holdings)
    monkeypatch.setattr(sentinel_job, "fetch_history", lambda tickers, days: history_map)
    monkeypatch.setattr(
        sentinel_job,
        "fetch_quotes",
        lambda tickers: {t: quotes.get(t) if quotes else None for t in tickers},
    )
    monkeypatch.setattr(sentinel_job.heartbeat, "heartbeat", heartbeat)
    sentinel_job.run()


def test_sentinel_happy_path_persists_signal_row(db, monkeypatch):
    """A breached position emits one row with agent/direction/weight_version/snapshot."""
    holding = Holding(ticker="AAPL", shares=100, cost_basis=50.0, account="schwab_main")
    history = {"AAPL": _df([100.0] * 15 + [90.0, 85.0])}  # 15% drawdown

    with patch("signal_system.jobs.sentinel.telegram_sender.send_message"):
        _run_sentinel(monkeypatch, holdings=[holding], history_map=history, quotes={"AAPL": {"c": 85.0}})

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT agent, direction, weight_version, signal_price_snapshot, severity "
        "FROM signals WHERE agent='exit_sentinel'"
    ).fetchall()
    status = conn.execute("SELECT status FROM runs WHERE job='sentinel'").fetchone()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "exit_sentinel"
    assert rows[0][1] == "negative"
    assert rows[0][2] == "sentinel-v1"
    assert rows[0][3] == 85.0
    assert rows[0][4] == "ACTION_REQUIRED"  # 15% drawdown hits the severe floor
    assert status == ("success",)


def test_sentinel_zero_breach_digest_line(db, monkeypatch, capsys):
    """No breaches still sends a zero-alert confirmation digest."""
    holding = Holding(ticker="AAPL", shares=100, cost_basis=50.0, account="schwab_main")
    history = {"AAPL": _df([100.0] * 21)}

    with patch("signal_system.jobs.sentinel.telegram_sender.send_message") as mock_send:
        _run_sentinel(monkeypatch, holdings=[holding], history_map=history, quotes={"AAPL": {"c": 100.0}})

    assert mock_send.call_count == 1
    body = mock_send.call_args.args[0]
    assert "Scanned 1 tickers, 0 alerts" in body
    assert "Sentinel" in body


def test_sentinel_cooldown_downgrades_to_monitoring(db, monkeypatch):
    """A prior informational trigger inside the window demotes the repeat."""
    holding = Holding(ticker="AAPL", shares=100, cost_basis=50.0, account="schwab_main")
    # 12% drawdown with a small one-day move → INFORMATIONAL (not an escalation).
    history = {"AAPL": _df([100.0] * 10 + [95.0] * 5 + [90.0, 88.0])}

    from signal_system.sentinel.exit_sentinel import EXIT_SENTINEL_AGENT, compute_alert_id

    df = _df([100.0] * 10 + [95.0] * 5 + [90.0, 88.0])
    index_dates = [d.strftime("%Y-%m-%d") for d in df.index]
    prior_date = index_dates[-3]
    prior_id = compute_alert_id("AAPL", prior_date, "exit:drawdown_20d", EXIT_SENTINEL_AGENT)
    prior = Signal(
        ticker="AAPL", score=50.0, severity="INFORMATIONAL", agent=EXIT_SENTINEL_AGENT,
        timestamp=datetime(2026, 5, 28, 17, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=prior_id, title="AAPL: drawdown_20d breach 10.0%",
    )
    repository.insert_signal(prior, routing_status="DELIVERED")

    with patch("signal_system.jobs.sentinel.telegram_sender.send_message"):
        _run_sentinel(monkeypatch, holdings=[holding], history_map=history, quotes={"AAPL": {"c": 85.0}})

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT severity, routing_status, title FROM signals WHERE agent='exit_sentinel' "
        "ORDER BY timestamp"
    ).fetchall()
    conn.close()

    # The prior row plus a fresh [cooldown] MONITORING row; nothing routed.
    assert len(rows) == 2
    assert rows[0][0] == "INFORMATIONAL"
    assert rows[1][0] == "MONITORING"
    assert rows[1][1] == "MONITORING"
    assert rows[1][2].startswith("[cooldown]")


def test_sentinel_escalation_bypasses_cooldown(db, monkeypatch):
    """A prior INFO trigger does NOT suppress a new ACTION_REQUIRED escalation."""
    holding = Holding(ticker="AAPL", shares=100, cost_basis=50.0, account="schwab_main")
    history = {"AAPL": _df([100.0] * 15 + [90.0, 85.0])}

    from signal_system.sentinel.exit_sentinel import EXIT_SENTINEL_AGENT, compute_alert_id

    df = _df([100.0] * 15 + [90.0, 85.0])
    index_dates = [d.strftime("%Y-%m-%d") for d in df.index]
    prior_date = index_dates[-3]
    prior_id = compute_alert_id("AAPL", prior_date, "exit:drawdown_20d", EXIT_SENTINEL_AGENT)
    prior = Signal(
        ticker="AAPL", score=50.0, severity="INFORMATIONAL", agent=EXIT_SENTINEL_AGENT,
        timestamp=datetime(2026, 5, 28, 17, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=prior_id, title="AAPL: drawdown_20d breach 10.0%",
    )
    repository.insert_signal(prior, routing_status="DELIVERED")

    with patch("signal_system.jobs.sentinel.telegram_sender.send_message") as mock_send:
        _run_sentinel(monkeypatch, holdings=[holding], history_map=history, quotes={"AAPL": {"c": 85.0}})

    body = mock_send.call_args.args[0]
    # The fresh AR trigger is delivered, not cooldown-downgraded.
    assert "AAPL — ACTION_REQUIRED" in body
    assert "[cooldown]" not in body


def test_sentinel_missing_holdings_fails_loudly(db, monkeypatch):
    """Empty holdings.csv aborts the run with a /fail heartbeat ping."""
    from signal_system.jobs import sentinel as sentinel_job
    from signal_system.monitoring import heartbeat as hb_mod

    pings: list[str] = []
    monkeypatch.setattr(sentinel_job, "require_non_empty_holdings", _raise_empty)
    monkeypatch.setattr(hb_mod, "_ping", lambda path, *, job=None: pings.append(path))
    monkeypatch.setattr(sentinel_job.heartbeat, "heartbeat", hb_mod.heartbeat)

    with pytest.raises(EmptyHoldingsError):
        sentinel_job.run()

    assert "/fail" in pings
    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job='sentinel'").fetchone()
    conn.close()
    assert status == ("failed",)


def _raise_empty():
    raise EmptyHoldingsError("No holdings.")
