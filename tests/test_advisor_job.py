"""Tests for the scheduled advisor job and on-demand advise_ticker command."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from signal_system.data.holdings import Holding


def _make_df(n_rows: int = 250, base_price: float = 100.0) -> pd.DataFrame:
    closes = [base_price + i * 0.2 for i in range(n_rows)]
    return pd.DataFrame({
        "Close": closes,
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
    })


@contextmanager
def _noop_heartbeat():
    yield


_FAKE_ROW = {
    "advice_id": "deadbeef",
    "run_id": "run-1",
    "timestamp": "2026-06-01T10:00:00-04:00",
    "ticker": "FCX",
    "account": "schwab_main",
    "held": True,
    "verdict": "HOLD",
    "confidence": 0.45,
    "mom_axis": "bullish",
    "news_axis": "neutral",
    "factors_json": "{}",
    "flags": "",
    "rationale": "FCX HOLD: trend bullish.",
    "rationale_source": "template",
    "model_version": "claude-sonnet-4-6",
    "thesis_version_hash": "abc123",
    "signal_price_snapshot": 110.0,
    "shadow_mode": True,
}


def test_advisor_run_persists_advice_rows_and_sends_telegram(tmp_path, monkeypatch):
    from signal_system.state import repository
    from signal_system.jobs import advisor as advisor_job

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    monkeypatch.setattr(
        advisor_job, "require_non_empty_holdings",
        lambda: [Holding(ticker="FCX", shares=40, cost_basis=38.10, account="schwab_main")],
    )
    monkeypatch.setattr(
        advisor_job, "load_thesis",
        lambda path: (MagicMock(pillars=[], review_due=date(2027, 1, 1)), "abc123"),
    )
    monkeypatch.setattr(
        advisor_job, "fetch_history",
        lambda tickers, days: {t: _make_df(250) for t in tickers},
    )
    monkeypatch.setattr(advisor_job, "_finnhub_close", lambda t: 110.0)
    mock_send = MagicMock()
    monkeypatch.setattr(advisor_job, "send_message", mock_send)
    monkeypatch.setattr(advisor_job, "produce_advice", lambda **kw: [_FAKE_ROW])
    monkeypatch.setattr(advisor_job, "heartbeat", _noop_heartbeat)

    advisor_job.run()

    conn = sqlite3.connect(tmp_path / "test.db")
    rows = conn.execute("SELECT advice_id, verdict FROM advice").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0] == ("deadbeef", "HOLD")
    mock_send.assert_called_once()


def test_advisor_run_fails_loudly_on_empty_holdings(tmp_path, monkeypatch):
    from signal_system.state import repository
    from signal_system.jobs import advisor as advisor_job
    from signal_system.data.holdings import EmptyHoldingsError

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    monkeypatch.setattr(
        advisor_job, "load_thesis",
        lambda path: (MagicMock(pillars=[], review_due=date(2027, 1, 1)), "abc123"),
    )

    def _raise():
        raise EmptyHoldingsError("No holdings.")

    monkeypatch.setattr(advisor_job, "require_non_empty_holdings", _raise)
    monkeypatch.setattr(advisor_job, "heartbeat", _noop_heartbeat)

    with pytest.raises(EmptyHoldingsError):
        advisor_job.run()


def test_render_digest_shadow_mode_header(monkeypatch):
    from signal_system.jobs import advisor as advisor_job

    rows = [
        {"ticker": "FCX", "account": "schwab_main", "held": True, "verdict": "HOLD",
         "confidence": 0.5, "flags": "", "rationale": "OK"},
    ]
    digest = advisor_job._render_digest(rows, shadow_mode=True)
    assert "SHADOW MODE" in digest


def test_render_digest_no_shadow_header_when_disabled(monkeypatch):
    from signal_system.jobs import advisor as advisor_job

    rows = [
        {"ticker": "FCX", "account": "schwab_main", "held": True, "verdict": "HOLD",
         "confidence": 0.5, "flags": "", "rationale": "OK"},
    ]
    digest = advisor_job._render_digest(rows, shadow_mode=False)
    assert "SHADOW MODE" not in digest


def test_advise_ticker_prints_to_stdout_and_does_not_write_advice(tmp_path, monkeypatch, capsys):
    from signal_system.state import repository
    from signal_system.jobs import advisor as advisor_job
    from signal_system.data.holdings import EmptyHoldingsError
    from signal_system.advisor import rationale as rat_mod

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    monkeypatch.setattr(
        advisor_job, "load_thesis",
        lambda path: (MagicMock(pillars=[], review_due=date(2027, 1, 1)), "abc123"),
    )

    def _raise():
        raise EmptyHoldingsError("no holdings")

    monkeypatch.setattr(advisor_job, "require_non_empty_holdings", _raise)
    monkeypatch.setattr(
        advisor_job, "fetch_history",
        lambda tickers, days: {t: _make_df(250) for t in tickers},
    )
    monkeypatch.setattr(advisor_job, "_finnhub_close", lambda t: 110.0)
    monkeypatch.setattr(repository, "get_recent_signals", lambda t, s, agent=None: [])
    monkeypatch.setattr(
        rat_mod,
        "generate_rationale",
        lambda **kw: (f"{kw['ticker']} {kw['verdict']}", "template"),
    )

    advisor_job.advise_ticker("FCX")

    captured = capsys.readouterr()
    assert "FCX" in captured.out

    conn = sqlite3.connect(tmp_path / "test.db")
    count = conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0]
    conn.close()
    assert count == 0


def test_main_advisor_job_registered():
    from signal_system import __main__
    assert "advisor" in __main__.JOBS


def test_main_advise_subcommand_not_in_jobs():
    """advise TICKER is handled separately, not as a JOBS entry."""
    from signal_system import __main__
    assert "advise" not in __main__.JOBS
