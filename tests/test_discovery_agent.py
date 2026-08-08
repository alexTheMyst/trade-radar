"""Tests for the Discovery Agent — multi-day momentum scoring.

Tests cover the new yfinance-based scoring with factors:
momentum_20d (50), momentum_5d (30), range_vs_20d (20).
"""
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from signal_system.state import repository

DATE_ISO = "2026-05-16"


def _make_candle_df(closes: list[float], highs: list[float], lows: list[float]):
    """Build a DataFrame matching yahoo_client.fetch_history() output format."""
    dates = pd.date_range(end="2026-05-16", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes, "High": highs, "Low": lows}, index=dates)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_momentum_scoring_three_tickers(db):
    """Cross-sectional ranking produces correct composite with 20d/5d/range factors."""
    from signal_system.discovery.discovery_agent import score_universe

    strong = _make_candle_df(
        closes=[100 + i for i in range(20)],  # 100..119, last=119
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    medium = _make_candle_df(
        closes=[100 + i * 0.5 for i in range(20)],  # 100..109.5
        highs=[105 + i * 0.5 for i in range(20)],
        lows=[95 + i * 0.5 for i in range(20)],
    )
    weak = _make_candle_df(
        closes=[100 - i * 0.25 for i in range(20)],  # 100..95.25
        highs=[105 - i * 0.2 for i in range(20)],
        lows=[94 - i * 0.3 for i in range(20)],
    )

    history = {"STRONG": strong, "MEDIUM": medium, "WEAK": weak}
    weights = {"STRONG": 10.0, "MEDIUM": 10.0, "WEAK": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["STRONG", "MEDIUM", "WEAK"], run_id, DATE_ISO)

    assert len(signals) >= 1
    tickers = [s.ticker for s in signals]
    assert "STRONG" in tickers
    strong_sig = next(s for s in signals if s.ticker == "STRONG")
    assert strong_sig.score == pytest.approx(100.0)
    assert strong_sig.severity == "ACTION_REQUIRED"
    assert set(strong_sig.sub_scores.keys()) == {"momentum_20d", "momentum_5d", "range_vs_20d"}
    assert strong_sig.weight_version == "xsect-50/30/20-absgate-v3"


def test_ticker_with_fewer_than_5_days_skipped(db):
    """Tickers with fewer than 5 trading days of data are skipped entirely."""
    from signal_system.discovery.discovery_agent import score_universe

    short = _make_candle_df(
        closes=[100, 101, 102, 103],
        highs=[102, 103, 104, 105],
        lows=[98, 99, 100, 101],
    )
    history = {"SHORT": short}
    weights = {"SHORT": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value=None):
        signals = score_universe(["SHORT"], run_id, DATE_ISO)

    assert signals == []


def test_empty_universe(db):
    """Empty ticker list returns [] without calling fetch_history."""
    from signal_system.discovery.discovery_agent import score_universe

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history") as mock_fh:
        signals = score_universe([], run_id, DATE_ISO)

    assert signals == []
    mock_fh.assert_not_called()


def test_all_tickers_no_data(db):
    """When fetch_history returns empty, no signals emitted and run counts updated."""
    from signal_system.discovery.discovery_agent import score_universe

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value={}), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value={}):
        signals = score_universe(["AAPL", "MSFT"], run_id, DATE_ISO)

    assert signals == []
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT tickers_scanned, tickers_signaled FROM runs WHERE run_id=?", (run_id,)).fetchone()
    conn.close()
    assert row == (2, 0)


def test_weight_amplifier_integration(db):
    """High-weight ticker promotes to ACTION_REQUIRED at lower score than low-weight ticker."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"BIG": candles, "SMALL": candles}
    weights = {"BIG": 25.0, "SMALL": 1.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["BIG", "SMALL"], run_id, DATE_ISO)

    big_signals = [s for s in signals if s.ticker == "BIG"]
    assert len(big_signals) == 1
    assert big_signals[0].severity == "ACTION_REQUIRED"


def test_signal_price_snapshot_from_quote(db):
    """Signal.signal_price_snapshot comes from fetch_quote, not from candle data."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"AAPL": candles, "MSFT": candles}
    weights = {"AAPL": 10.0, "MSFT": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 177.50, "dp": 1.0, "h": 180.0, "l": 175.0}):
        signals = score_universe(["AAPL", "MSFT"], run_id, DATE_ISO)

    for sig in signals:
        assert sig.signal_price_snapshot == pytest.approx(177.50)


def test_rank_values_helper():
    """_rank_values produces correct cross-sectional ranks."""
    from signal_system.discovery.discovery_agent import _rank_values

    assert _rank_values({}) == {}
    assert _rank_values({"A": 5.0}) == {"A": 0.5}
    assert _rank_values({"A": 10.0, "B": 5.0}) == {"A": 1.0, "B": 0.0}
    assert _rank_values({"B": 5.0, "A": 5.0}) == {"A": 1.0, "B": 0.0}


def test_update_run_counts(db):
    """score_universe writes tickers_scanned and tickers_signaled to runs table."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"HIGH": candles, "LOW": candles}
    weights = {"HIGH": 10.0, "LOW": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT tickers_scanned, tickers_signaled FROM runs WHERE run_id=?", (run_id,)).fetchone()
    conn.close()
    assert row[0] == 2


def test_alert_id_determinism(db):
    """Same ticker + date produces identical alert_id across multiple runs."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"HIGH": candles, "LOW": candles}
    weights = {"HIGH": 10.0, "LOW": 10.0}

    run_id1 = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals_run1 = score_universe(["HIGH", "LOW"], run_id1, DATE_ISO)

    run_id2 = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals_run2 = score_universe(["HIGH", "LOW"], run_id2, DATE_ISO)

    assert len(signals_run1) == len(signals_run2)
    for s1, s2 in zip(signals_run1, signals_run2):
        assert s1.alert_id == s2.alert_id


def test_sub_scores_contains_momentum_keys(db):
    """Signal.sub_scores has momentum_20d, momentum_5d, range_vs_20d keys."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"AAPL": candles, "MSFT": candles}
    weights = {"AAPL": 10.0, "MSFT": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["AAPL", "MSFT"], run_id, DATE_ISO)

    assert len(signals) >= 1
    signal = signals[0]
    assert set(signal.sub_scores.keys()) == {"momentum_20d", "momentum_5d", "range_vs_20d"}
    for v in signal.sub_scores.values():
        assert 0.0 <= v <= 1.0


def test_signal_body_prefix(db):
    """Discovery signals include the factor-weight prefix in body."""
    from signal_system.discovery.discovery_agent import score_universe

    candles = _make_candle_df(
        closes=[100 + i for i in range(20)],
        highs=[102 + i for i in range(20)],
        lows=[98 + i for i in range(20)],
    )
    history = {"HIGH": candles, "LOW": candles}
    weights = {"HIGH": 10.0, "LOW": 10.0}

    run_id = repository.insert_run("discovery")

    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

    assert len(signals) >= 1
    assert signals[0].body is not None
    assert signals[0].body.startswith("weights=50/30/20")


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


def _momentum_df(start: float, end: float, n: int = 21) -> "pd.DataFrame":
    """Build a candle df with a linear path from *start* to *end* (momentum_20d ≈ end/start - 1)."""
    closes = [start + (end - start) * i / (n - 1) for i in range(n)]
    return _make_candle_df(
        closes=closes,
        highs=[max(c, c + 1.0) for c in closes],
        lows=[min(c, c - 1.0) for c in closes],
    )


def test_negative_momentum_top_ticker_emits_nothing(db):
    """A falling-universe top ranker still emits nothing (absolute gate)."""
    from signal_system.discovery.discovery_agent import score_universe

    # 100 -> 97 over 20d: momentum_20d = -3%. Even though this is the best of
    # a falling batch (rank ~1.0, composite ~100), the gate suppresses it.
    falling = _momentum_df(100.0, 97.0)
    history = {"TOP": falling, "MID": _momentum_df(100.0, 95.0), "BOT": _momentum_df(100.0, 90.0)}
    weights = {"TOP": 10.0, "MID": 10.0, "BOT": 10.0}

    run_id = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["TOP", "MID", "BOT"], run_id, DATE_ISO)

    assert signals == []


def test_positive_but_sub_action_momentum_caps_at_info_with_gate(db):
    """+3% momentum in a weak batch caps the top ranker at INFORMATIONAL with gate marker."""
    from signal_system.discovery.discovery_agent import score_universe

    # TOP momentum_20d = +3% (< 0.05 AR gate), but positive (>= 0.0 INFO gate).
    # In a batch where everything else is flat/negative, TOP ranks ~1.0 so the
    # base severity would be ACTION_REQUIRED — the gate downgrades it to INFO.
    top = _momentum_df(100.0, 103.0)
    history = {"TOP": top, "FLAT": _momentum_df(100.0, 100.0), "NEG": _momentum_df(100.0, 98.0)}
    weights = {"TOP": 10.0, "FLAT": 10.0, "NEG": 10.0}

    run_id = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["TOP", "FLAT", "NEG"], run_id, DATE_ISO)

    top_sigs = [s for s in signals if s.ticker == "TOP"]
    assert len(top_sigs) == 1
    assert top_sigs[0].severity == "INFORMATIONAL"
    assert top_sigs[0].body is not None
    assert "gate=abs_mom_ar" in top_sigs[0].body


def test_strong_momentum_still_fires_action(db):
    """+10% momentum fires ACTION_REQUIRED — gate passes, stamp is v3."""
    from signal_system.discovery.discovery_agent import score_universe

    strong = _momentum_df(100.0, 110.0)
    history = {"STRONG": strong, "WEAK": _momentum_df(100.0, 99.0)}
    weights = {"STRONG": 10.0, "WEAK": 10.0}

    run_id = repository.insert_run("discovery")
    with patch("signal_system.discovery.discovery_agent.fetch_history", return_value=history), \
         patch("signal_system.discovery.discovery_agent.get_position_weights", return_value=weights), \
         patch("signal_system.discovery.discovery_agent.fetch_quote", return_value={"c": 119.0, "dp": 1.0, "h": 121.0, "l": 117.0}):
        signals = score_universe(["STRONG", "WEAK"], run_id, DATE_ISO)

    strong_sigs = [s for s in signals if s.ticker == "STRONG"]
    assert len(strong_sigs) == 1
    assert strong_sigs[0].severity == "ACTION_REQUIRED"
    assert strong_sigs[0].weight_version == "xsect-50/30/20-absgate-v3"
