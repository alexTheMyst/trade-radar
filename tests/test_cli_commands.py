"""Tests for the manual operator CLI commands (list-signals, log-action).

These refactor the argv handling in __main__.py into _cmd_* functions so tests
can call them directly without subprocess/argv patching.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from signal_system import __main__
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _delivered_sig(ticker: str = "AAPL", ts: datetime | None = None) -> Signal:
    ts = ts or datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)
    return Signal(
        ticker=ticker,
        score=0.8,
        severity="INFORMATIONAL",
        agent="news_classifier",
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), "r", "news_classifier"),
        title=f"{ticker}: update",
    )


def _insert(sig: Signal, routing_status: str = "DELIVERED") -> None:
    repository.insert_signal(sig, routing_status=routing_status)


def test_cmd_list_signals_prints_delivered(capsys, db):
    _insert(_delivered_sig())
    __main__._cmd_list_signals(days=14, now=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc))
    out = capsys.readouterr().out
    assert "AAPL" in out


def test_cmd_list_signals_respects_days_window(capsys, db):
    old = _delivered_sig(ts=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    _insert(old)
    __main__._cmd_list_signals(days=14, now=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc))
    out = capsys.readouterr().out
    assert "no delivered signals" in out.lower() or "AAPL" not in out


def test_cmd_log_action_acted_sets_row(capsys, db):
    sig = _delivered_sig()
    _insert(sig)
    prefix = sig.alert_id[:8]

    __main__._cmd_log_action(prefix, acted=True, user_note="bought 10")

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT acted, user_note FROM signals WHERE alert_id=?", (sig.alert_id,)).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == "bought 10"


def test_cmd_log_action_passed_sets_zero(capsys, db):
    sig = _delivered_sig()
    _insert(sig)
    __main__._cmd_log_action(sig.alert_id[:8], acted=False, user_note=None)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT acted FROM signals WHERE alert_id=?", (sig.alert_id,)).fetchone()
    conn.close()
    assert row[0] == 0


def test_cmd_log_action_no_match_exits_two(capsys, db):
    with pytest.raises(SystemExit) as exc:
        __main__._cmd_log_action("deadbeef", acted=True, user_note=None)
    assert exc.value.code == 2
    assert "no signals" in capsys.readouterr().out.lower()


def test_cmd_log_action_ambiguous_exits_two(capsys, db):
    a = _delivered_sig()
    b = _delivered_sig(ticker="MSFT")
    # force both into the same prefix by re-keying the second
    b = Signal(
        ticker=b.ticker, score=b.score, severity=b.severity, agent=b.agent,
        timestamp=b.timestamp,
        alert_id=a.alert_id[:8] + "ffffffffffffffff",
        title=b.title,
    )
    _insert(a)
    _insert(b)

    with pytest.raises(SystemExit) as exc:
        __main__._cmd_log_action(a.alert_id[:8], acted=True, user_note=None)
    assert exc.value.code == 2
    assert "ambiguous" in capsys.readouterr().out.lower()


def test_cmd_log_action_invalid_prefix_exits_two(capsys, db):
    with pytest.raises(SystemExit) as exc:
        __main__._cmd_log_action("xyz", acted=True, user_note=None)
    assert exc.value.code == 2


def test_review_report_job_is_registered():
    assert "review-report" in __main__.JOBS


def test_weekly_synthesis_job_is_registered():
    assert "weekly-synthesis" in __main__.JOBS


def test_cmd_build_universe_dry_run_prints_diff(capsys, tmp_path, monkeypatch, db):
    import signal_system.data.universe_builder as ub

    op_path = tmp_path / "universe.csv"
    op_path.write_text(
        "ticker,core_holding,weight_pct,k1_etf,industry\n"
        "AAPL,1,12.5,0,\n"
    )
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", {
        "sp500": tmp_path / "sp500.csv",
    })
    (tmp_path / "sp500.csv").write_text(
        "ticker,name,sector,industry\nMSFT,Microsoft,Technology,Software\n"
    )
    monkeypatch.setattr(ub, "_enrich_with_yfinance", lambda ticker: (None, None))

    __main__._cmd_build_universe([])
    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert "+ MSFT" in out
    # dry-run must not write the file
    assert "MSFT" not in op_path.read_text()


def test_cmd_build_universe_apply_writes(capsys, tmp_path, monkeypatch, db):
    import signal_system.data.universe_builder as ub

    op_path = tmp_path / "universe.csv"
    op_path.write_text("ticker,core_holding,weight_pct,k1_etf,industry\n")
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", {
        "sp500": tmp_path / "sp500.csv",
    })
    (tmp_path / "sp500.csv").write_text(
        "ticker,name,sector,industry\nMSFT,Microsoft,Technology,Software\n"
    )

    __main__._cmd_build_universe(["--apply"])
    out = capsys.readouterr().out
    assert "Applied" in out
    assert "MSFT" in op_path.read_text()
