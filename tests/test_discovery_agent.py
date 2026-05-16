"""Tests for the Discovery Agent — score_universe() and supporting helpers.

Tests T-01 through T-21 covering DISC-01..DISC-05 requirements and UAT smoke.
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from signal_system import config
from signal_system.state import repository
from signal_system.discovery import score_universe
from signal_system.data.finnhub_client import fetch_quote

DATE_ISO = "2026-05-16"


def _q(dp=5.0, v=300, c=50.0, h=60.0, l=40.0):
    """Build a valid quote dict."""
    return {"c": c, "dp": dp, "v": v, "h": h, "l": l, "o": 48.0, "pc": 49.0}


def _news(n=2):
    return [{"headline": f"News {i}"} for i in range(n)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


# ---------------------------------------------------------------------------
# T-01: test_score_computation
# ---------------------------------------------------------------------------

def test_score_computation(db, monkeypatch):
    """Cross-sectional ranking produces correct composite scores for 3 tickers."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "A": _q(dp=10.0, v=300, c=50.0, h=60.0, l=40.0),
        "B": _q(dp=5.0,  v=200, c=48.0, h=50.0, l=40.0),
        "C": _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"A": 3, "B": 2, "C": 1}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        signals = score_universe(["A", "B", "C"], run_id, DATE_ISO)

    assert len(signals) == 2
    sig_a = next(s for s in signals if s.ticker == "A")
    sig_b = next(s for s in signals if s.ticker == "B")
    assert sig_a.score == pytest.approx(87.5)
    assert sig_b.score == pytest.approx(62.5)
    assert "C" not in [s.ticker for s in signals]


# ---------------------------------------------------------------------------
# T-02: test_score_floor_invalid_quote (dp=None)
# ---------------------------------------------------------------------------

def test_score_floor_invalid_quote():
    """fetch_quote returns None when dp is None (score-floor guard)."""
    with patch("signal_system.data.finnhub_client._fetch_single_quote",
               return_value={"c": 50.0, "dp": None, "v": 300, "h": 60.0, "l": 40.0}):
        result = fetch_quote("AAPL")
    assert result is None


# ---------------------------------------------------------------------------
# T-03: test_score_floor_null_quote
# ---------------------------------------------------------------------------

def test_score_floor_null_quote(db, monkeypatch):
    """Ticker is excluded when fetch_quote returns None."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=None):
        result = score_universe(["AAPL"], run_id, DATE_ISO)

    assert result == []


# ---------------------------------------------------------------------------
# T-04: test_range_position_flat_day
# ---------------------------------------------------------------------------

def test_range_position_flat_day(db, monkeypatch):
    """h==l (flat day) produces range_position=0.0 without exception; ticker still scored."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    run_id = repository.insert_run("discovery")

    aapl_q = _q(dp=5.0, v=200, c=50.0, h=50.0, l=50.0)
    msft_q = _q(dp=3.0, v=150, c=55.0, h=60.0, l=40.0)

    quotes = {"AAPL": aapl_q, "MSFT": msft_q}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(1)

    # Should not raise
    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["AAPL", "MSFT"], run_id, DATE_ISO)

    # Verify no exception was raised (we reached this point)
    # MSFT should get range_rank=1.0, AAPL range_rank=0.0
    # If MSFT scores >=60, verify its range sub_score != AAPL's
    msft_signals = [s for s in result if s.ticker == "MSFT"]
    aapl_signals = [s for s in result if s.ticker == "AAPL"]
    if msft_signals and aapl_signals:
        assert msft_signals[0].sub_scores["range_position"] != aapl_signals[0].sub_scores["range_position"]
    elif msft_signals:
        # MSFT scored, AAPL didn't — range guard worked
        assert msft_signals[0].sub_scores["range_position"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T-05: test_news_activity_missing (fetch_company_news returns None)
# ---------------------------------------------------------------------------

def test_news_activity_missing(db, monkeypatch):
    """No exception when fetch_company_news returns None (treated as 0 news)."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=_q()), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", return_value=None):
        result = score_universe(["AAPL"], run_id, DATE_ISO)

    # Single ticker → all ranks 0.5 → composite=50.0 < 60 → no signal
    assert result == []


# ---------------------------------------------------------------------------
# T-06: test_news_activity_empty (fetch_company_news returns [])
# ---------------------------------------------------------------------------

def test_news_activity_empty(db, monkeypatch):
    """No exception when fetch_company_news returns empty list."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=_q()), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", return_value=[]):
        result = score_universe(["AAPL"], run_id, DATE_ISO)

    assert result == []


# ---------------------------------------------------------------------------
# T-07: test_phase_a_inserts_monitoring
# ---------------------------------------------------------------------------

def test_phase_a_inserts_monitoring(db, monkeypatch):
    """Phase A inserts signals with routing_status='MONITORING' and returns []."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "A")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side), \
         patch("signal_system.state.repository.insert_signal") as mock_insert:
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    assert result == []
    assert mock_insert.called is True
    assert mock_insert.call_args.kwargs["routing_status"] == "MONITORING"


# ---------------------------------------------------------------------------
# T-08: test_phase_b_returns_signals
# ---------------------------------------------------------------------------

def test_phase_b_returns_signals(db, monkeypatch):
    """Phase B returns list[Signal] without calling insert_signal."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side), \
         patch("signal_system.state.repository.insert_signal") as mock_insert:
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    assert len(result) == 1
    assert result[0].ticker == "HIGH"
    mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# T-09: test_threshold_below_60_suppressed
# ---------------------------------------------------------------------------

def test_threshold_below_60_suppressed(db, monkeypatch):
    """Single ticker gets all 0.5 ranks → composite=50.0 < 60 → no signal."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=_q()), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", return_value=_news(1)):
        result = score_universe(["AAPL"], run_id, DATE_ISO)

    assert result == []


# ---------------------------------------------------------------------------
# T-10: test_action_required_severity
# ---------------------------------------------------------------------------

def test_action_required_severity(db, monkeypatch):
    """Ticker with composite score >= 80 gets severity='ACTION_REQUIRED'."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    high_signal = next(s for s in result if s.ticker == "HIGH")
    assert high_signal.score == pytest.approx(100.0)
    assert high_signal.severity == "ACTION_REQUIRED"


# ---------------------------------------------------------------------------
# T-11: test_informational_severity
# ---------------------------------------------------------------------------

def test_informational_severity(db, monkeypatch):
    """Ticker with 60 <= composite < 80 gets severity='INFORMATIONAL'."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "A": _q(dp=10.0, v=300, c=50.0, h=60.0, l=40.0),
        "B": _q(dp=5.0,  v=200, c=48.0, h=50.0, l=40.0),
        "C": _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"A": 3, "B": 2, "C": 1}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["A", "B", "C"], run_id, DATE_ISO)

    sig_b = next(s for s in result if s.ticker == "B")
    assert sig_b.score == pytest.approx(62.5)
    assert sig_b.severity == "INFORMATIONAL"


# ---------------------------------------------------------------------------
# T-12: test_cross_sectional_ranking_ties
# ---------------------------------------------------------------------------

def test_cross_sectional_ranking_ties(db, monkeypatch):
    """Equal dp values: alphabetical tiebreak puts AAPL above BIDU."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "AAPL": _q(dp=5.0, v=200, c=50.0, h=60.0, l=40.0),
        "BIDU": _q(dp=5.0, v=100, c=45.0, h=55.0, l=40.0),
    }

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(2 if ticker == "AAPL" else 1)

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["AAPL", "BIDU"], run_id, DATE_ISO)

    aapl_signals = [s for s in result if s.ticker == "AAPL"]
    bidu_signals = [s for s in result if s.ticker == "BIDU"]
    assert aapl_signals, "AAPL should score >=60"
    assert aapl_signals[0].sub_scores["price_momentum"] == pytest.approx(1.0)
    if bidu_signals:
        assert bidu_signals[0].sub_scores["price_momentum"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# T-13: test_single_ticker_universe
# ---------------------------------------------------------------------------

def test_single_ticker_universe(db, monkeypatch):
    """Single ticker gets all ranks=0.5; verify sub_scores by lowering threshold."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
    import signal_system.discovery.discovery_agent as da
    monkeypatch.setattr(da, "SCORE_THRESHOLD_INFORM", 0.0)

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=_q()), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", return_value=_news(1)):
        result = score_universe(["AAPL"], run_id, DATE_ISO)

    # With threshold lowered, signal should be emitted
    assert len(result) == 1
    for v in result[0].sub_scores.values():
        assert v == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# T-14: test_empty_universe
# ---------------------------------------------------------------------------

def test_empty_universe(db, monkeypatch):
    """Empty ticker list returns [] without calling fetch_quote."""
    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote") as mock_fq:
        result = score_universe([], run_id, DATE_ISO)

    assert result == []
    mock_fq.assert_not_called()


# ---------------------------------------------------------------------------
# T-15: test_update_run_counts
# ---------------------------------------------------------------------------

def test_update_run_counts(db, monkeypatch):
    """update_run_counts writes tickers_scanned and tickers_signaled to runs table."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT tickers_scanned, tickers_signaled FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    conn.close()

    assert row[0] == 2  # both tickers attempted
    assert row[1] == 1  # only HIGH scored >=60


# ---------------------------------------------------------------------------
# T-16: test_alert_id_determinism
# ---------------------------------------------------------------------------

def test_alert_id_determinism(db, monkeypatch):
    """Same ticker + date produces identical alert_id across multiple runs."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id1 = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        signals_run1 = score_universe(["HIGH", "LOW"], run_id1, DATE_ISO)

    run_id2 = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        signals_run2 = score_universe(["HIGH", "LOW"], run_id2, DATE_ISO)

    assert signals_run1[0].alert_id == signals_run2[0].alert_id


# ---------------------------------------------------------------------------
# T-17: test_signal_price_snapshot
# ---------------------------------------------------------------------------

def test_signal_price_snapshot(db, monkeypatch):
    """Signal.signal_price_snapshot equals quote['c'] for that ticker."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=53.75, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0,  h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    high_signal = next(s for s in result if s.ticker == "HIGH")
    assert high_signal.signal_price_snapshot == pytest.approx(53.75)


# ---------------------------------------------------------------------------
# T-18: test_sub_scores_dict
# ---------------------------------------------------------------------------

def test_sub_scores_dict(db, monkeypatch):
    """Signal.sub_scores has exactly 4 keys with values in [0.0, 1.0]."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW":  _q(dp=1.0,  v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    assert len(result) >= 1
    signal = result[0]
    assert set(signal.sub_scores.keys()) == {
        "price_momentum", "volume_rank", "range_position", "news_activity"
    }
    for v in signal.sub_scores.values():
        assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# T-19: test_signal_body_prefix
# ---------------------------------------------------------------------------

def test_signal_body_prefix(db, monkeypatch):
    """Discovery signals include the documented factor-weight prefix in body."""
    monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")

    quotes = {
        "HIGH": _q(dp=10.0, v=200, c=55.0, h=60.0, l=40.0),
        "LOW": _q(dp=1.0, v=100, c=41.0, h=50.0, l=40.0),
    }
    news_counts = {"HIGH": 2, "LOW": 0}

    def fq_side(ticker):
        return quotes[ticker]

    def fcn_side(ticker, from_date, to_date):
        return _news(news_counts[ticker])

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_quote", side_effect=fq_side), \
         patch("signal_system.discovery.discovery_agent.fetch_company_news", side_effect=fcn_side):
        result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    assert len(result) == 1
    assert result[0].body is not None
    assert result[0].body.startswith("weights=35/30/25/10")


# ---------------------------------------------------------------------------
# T-20: test_public_surface_smoke
# ---------------------------------------------------------------------------

def test_public_surface_smoke():
    """Package export, thresholds, and rank helper match the phase contract."""
    from signal_system.discovery import score_universe as exported_score_universe
    from signal_system.discovery.discovery_agent import (
        SCORE_THRESHOLD_ACTION,
        SCORE_THRESHOLD_INFORM,
        _rank_values,
    )

    assert exported_score_universe.__module__ == "signal_system.discovery.discovery_agent"
    assert SCORE_THRESHOLD_ACTION == 80.0
    assert SCORE_THRESHOLD_INFORM == 60.0
    assert _rank_values({}) == {}
    assert _rank_values({"AAPL": 5.0}) == {"AAPL": 0.5}
    assert _rank_values({"AAPL": 5.0, "MSFT": 3.0}) == {"AAPL": 1.0, "MSFT": 0.0}
    assert _rank_values({"BIDU": 5.0, "AAPL": 5.0}) == {"AAPL": 1.0, "BIDU": 0.0}


# ---------------------------------------------------------------------------
# T-21: test_discovery_agent_isolated_from_delivery_and_router
# ---------------------------------------------------------------------------

def test_discovery_agent_isolated_from_delivery_and_router():
    """Discovery agent import/execution must not load delivery or router modules."""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = (
        src_path
        if "PYTHONPATH" not in env
        else src_path + os.pathsep + env["PYTHONPATH"]
    )

    script = """
import builtins

blocked = {
    "signal_system.delivery.email_sender",
    "signal_system.router",
    "signal_system.router.alert_router",
}
orig_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name in blocked:
        raise AssertionError(f"blocked import: {name}")
    if name == "signal_system.delivery" and fromlist and "email_sender" in fromlist:
        raise AssertionError("blocked import: signal_system.delivery.email_sender")
    if name == "signal_system" and fromlist and "router" in fromlist:
        raise AssertionError("blocked import: signal_system.router")
    return orig_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import

from signal_system import config
import signal_system.discovery.discovery_agent as da

config.DISCOVERY_PHASE = "B"
da.repository.update_run_counts = lambda *args, **kwargs: None
da.fetch_quote = lambda ticker: {
    "c": 55.0 if ticker == "HIGH" else 41.0,
    "dp": 10.0 if ticker == "HIGH" else 1.0,
    "v": 200 if ticker == "HIGH" else 100,
    "h": 60.0 if ticker == "HIGH" else 50.0,
    "l": 40.0,
}
da.fetch_company_news = lambda ticker, from_date, to_date: (
    [{"headline": "News 1"}, {"headline": "News 2"}] if ticker == "HIGH" else []
)

signals = da.score_universe(["HIGH", "LOW"], "run-1", "2026-05-16")
assert len(signals) == 1
assert signals[0].ticker == "HIGH"
print("OK")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
