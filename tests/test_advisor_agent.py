"""Integration tests for advisor_agent.produce_advice.

All external I/O is injected -- no network, no LLM, no real DB paths.
"""
from __future__ import annotations

import pandas as pd
import pytest
from datetime import date

from signal_system.data.holdings import Holding
from signal_system.advisor.verdict_engine import NEWS_BULLISH_THRESHOLD, NEWS_BEARISH_THRESHOLD


def _make_df(n_rows: int = 250, base_price: float = 100.0, trend: str = "up") -> pd.DataFrame:
    """Build synthetic OHLCV DataFrame with enough rows for 200d SMA."""
    if trend == "up":
        closes = [base_price + i * 0.2 for i in range(n_rows)]
    else:
        closes = [base_price - i * 0.2 for i in range(n_rows)]
    return pd.DataFrame({
        "Close": closes,
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
    })


_FCX = Holding(ticker="FCX", shares=40, cost_basis=38.10, account="schwab_main")


def _run(
    holdings, history_map, quotes, news_map, candidates, monkeypatch, tmp_path
) -> list[dict]:
    from signal_system.state import repository
    from signal_system.advisor import advisor_agent
    from signal_system.advisor import rationale as rat_mod

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    # Stub rationale to avoid any Claude call
    monkeypatch.setattr(
        rat_mod,
        "generate_rationale",
        lambda **kw: (f"{kw['ticker']} {kw['verdict']}", "template"),
    )

    return advisor_agent.produce_advice(
        holdings=holdings,
        fetch_history=lambda tickers, days: {t: history_map[t] for t in tickers if t in history_map},
        fetch_quote=lambda t: quotes.get(t),
        get_recent_signals=lambda t, since: news_map.get(t, []),
        get_discovery_candidates=lambda since, excl: [c for c in candidates if c["ticker"] not in excl],
        thesis_text="test thesis",
        thesis_version_hash="abc123",
        run_id="run-001",
        shadow_mode=True,
        today=date(2026, 6, 1),
    )


def test_held_position_produces_one_advice_row(tmp_path, monkeypatch):
    rows = _run(
        holdings=[_FCX],
        history_map={"FCX": _make_df(250)},
        quotes={"FCX": 110.0},
        news_map={"FCX": [(1.0, 0.8)]},
        candidates=[],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert len(rows) == 1
    assert rows[0]["ticker"] == "FCX"
    assert rows[0]["held"] is True
    assert rows[0]["account"] == "schwab_main"
    assert rows[0]["shadow_mode"] is True
    assert rows[0]["verdict"] in ("BUY", "HOLD", "SELL")


def test_no_data_verdict_when_history_has_fewer_than_201_rows(tmp_path, monkeypatch):
    rows = _run(
        holdings=[_FCX],
        history_map={"FCX": _make_df(n_rows=100)},
        quotes={},
        news_map={},
        candidates=[],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert len(rows) == 1
    assert rows[0]["verdict"] == "NO_DATA"
    assert "no_data" in rows[0]["flags"]


def test_no_data_does_not_abort_run_for_other_holdings(tmp_path, monkeypatch):
    """One NO_DATA ticker must not prevent other holdings from being evaluated."""
    msft = Holding(ticker="MSFT", shares=10, cost_basis=300.0, account="roth_ira")
    rows = _run(
        holdings=[_FCX, msft],
        history_map={
            "FCX": _make_df(250),
            "MSFT": _make_df(50),  # insufficient
        },
        quotes={"FCX": 110.0},
        news_map={},
        candidates=[],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert len(rows) == 2
    msft_row = next(r for r in rows if r["ticker"] == "MSFT")
    assert msft_row["verdict"] == "NO_DATA"


def test_thesis_break_produces_sell_for_held_position(tmp_path, monkeypatch):
    # direction=-1.0, confidence=0.9 -> thesis_break fires
    rows = _run(
        holdings=[_FCX],
        history_map={"FCX": _make_df(250, trend="up")},
        quotes={"FCX": 110.0},
        news_map={"FCX": [(-1.0, 0.9)]},  # single high-confidence negative hit
        candidates=[],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert rows[0]["verdict"] == "SELL"
    assert "thesis_break" in rows[0]["flags"]


def test_new_buy_candidates_appear_when_trend_bullish(tmp_path, monkeypatch):
    rows = _run(
        holdings=[],
        history_map={"NVDA": _make_df(250, base_price=100.0)},
        quotes={"NVDA": 150.0},
        news_map={},
        candidates=[{"ticker": "NVDA", "score": 0.9}],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    buy_rows = [r for r in rows if r["ticker"] == "NVDA" and r["verdict"] == "BUY"]
    assert len(buy_rows) == 1
    assert buy_rows[0]["held"] is False
    assert buy_rows[0]["account"] is None


def test_new_buy_candidates_capped_at_five(tmp_path, monkeypatch):
    tickers = ["A", "B", "C", "D", "E", "F"]
    rows = _run(
        holdings=[],
        history_map={t: _make_df(250) for t in tickers},
        quotes={t: 120.0 for t in tickers},
        news_map={},
        candidates=[{"ticker": t, "score": float(7 - i)} for i, t in enumerate(tickers)],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert len([r for r in rows if r["verdict"] == "BUY"]) <= 5


def test_held_ticker_excluded_from_candidates(tmp_path, monkeypatch):
    # FCX is held AND in candidates -- should only appear as a held row
    rows = _run(
        holdings=[_FCX],
        history_map={"FCX": _make_df(250)},
        quotes={"FCX": 110.0},
        news_map={},
        candidates=[{"ticker": "FCX", "score": 0.95}],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    fcx_rows = [r for r in rows if r["ticker"] == "FCX"]
    assert len(fcx_rows) == 1
    assert fcx_rows[0]["held"] is True


def test_factors_json_is_valid_json(tmp_path, monkeypatch):
    import json
    rows = _run(
        holdings=[_FCX],
        history_map={"FCX": _make_df(250)},
        quotes={"FCX": 110.0},
        news_map={},
        candidates=[],
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    parsed = json.loads(rows[0]["factors_json"])
    assert "price" in parsed
    assert "sma50" in parsed
    assert "sma200" in parsed
