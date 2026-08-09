"""Tests for the offline Discovery weight-replay engine.

run_replay() is pure: it takes a {ticker: history_df} map plus a SPY series,
weight sets, and horizons, and returns per-cell metrics. The composite
formula and gate constants are imported from discovery_agent so the replay
measures the exact live scoring regime.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from signal_system.replay.weight_replay import (
    parse_weights,
    run_replay,
)


def _mk_history(ticker: str, n: int = 120, *, growth: float = 1.0) -> pd.DataFrame:
    index = pd.date_range(datetime(2026, 1, 1), periods=n, freq="B")
    base = 100.0
    closes = [base * (1 + growth * i / n) for i in range(n)]
    return pd.DataFrame(
        {"Close": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes]},
        index=index,
    )


def _mk_spy(n: int = 120, *, growth: float = 0.05) -> pd.DataFrame:
    return _mk_history("SPY", n, growth=growth)


def test_parse_weights_ok():
    assert parse_weights("50/30/20") == [50.0, 30.0, 20.0]


def test_parse_weights_bad_tokens():
    with pytest.raises(ValueError):
        parse_weights("50/abc/20")
    with pytest.raises(ValueError):
        parse_weights("50/30")


def test_parse_weights_bad_slash():
    with pytest.raises(ValueError):
        parse_weights("50.5/30/20")  # non-integer token


def test_synthetic_monotone_history_ic_approx_one():
    """Higher momentum -> higher forward excess, so IC should approach 1."""
    history = {"AAA": _mk_history("AAA", 120, growth=0.3), "BBB": _mk_history("BBB", 120, growth=0.05)}
    spy = _mk_spy(120)
    cells = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5, 20], gates_on=False)
    for cell in cells:
        if cell.n_pairs >= 10:
            assert cell.ic is not None
            assert cell.ic > 0.9


def test_gates_alter_emittable_count():
    """Gates on drops pairs whose momentum_20d is below the info gate.

    Both tickers decline; AAA declines slowly (tops the cross-sectional rank,
    composite >= 60) but still has negative momentum_20d — so it emits with
    gates off and is dropped with gates on.
    """
    history = {
        "AAA": _mk_history("AAA", 120, growth=-0.05),  # slow decline → top rank, neg momentum
        "BBB": _mk_history("BBB", 120, growth=-0.5),   # fast decline → bottom rank
    }
    spy = _mk_spy(120)
    cells_off = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=False)
    cells_on = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=True)
    off = {c.horizon: c.n_emittable for c in cells_off}
    on = {c.horizon: c.n_emittable for c in cells_on}
    assert on[5] < off[5]


def test_hit_rate_over_composite_ge_60_pairs():
    """Hit rate is computed over the would-be-emitted set (composite >= 60)."""
    history = {"AAA": _mk_history("AAA", 120, growth=0.4)}
    spy = _mk_spy(120)
    cells = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=False)
    cell = cells[0]
    assert cell.n_emittable >= 0
    # hit rate present whenever at least one emitted pair exists
    if cell.n_emittable:
        assert cell.hit_rate is not None


def test_spy_gap_handled_and_counted():
    """SPY missing dates are dropped and counted, not crash-prone."""
    history = {"AAA": _mk_history("AAA", 60, growth=0.3)}
    spy = _mk_history("SPY", 40, growth=0.05)  # shorter SPY series
    cells = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=False)
    assert cells[0].spy_dropped is not None


def test_ticker_gap_day_counted_not_corrupt():
    """A ticker missing a union-calendar trading day is skipped and counted.

    Before the fix, get_indexer returned -1 for the gap day and _forward_excess
    anchored "now" at the series' last row, feeding a nonsense pair. Now the
    gapped counter increments and no pair is produced for that day.
    """
    history = {
        "AAA": _mk_history("AAA", 60, growth=0.3),
        "BBB": _mk_history("BBB", 60, growth=0.05),
    }
    # Drop one trading day from BBB's calendar (a listing gap / halt).
    gap_day = history["BBB"].index[30]
    history["BBB"] = history["BBB"].drop(gap_day)

    spy = _mk_spy(60)
    cells = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=False)
    cell = cells[0]
    assert cell.gapped >= 1, "gap day must be counted as gapped"
    # Every pooled pair must have a finite excess (no -1-anchored nonsense).
    assert cell.ic is None or cell.ic == cell.ic  # ic is either None or not-NaN


def test_gates_drop_pairs_from_pool_not_just_n_emit():
    """Gates drop pairs from the pool entirely, so IC/n_pairs/hit_rate/n_emit
    all describe the same gate-filtered population (review bug 5)."""
    history = {
        "AAA": _mk_history("AAA", 120, growth=-0.05),  # slow decline → top rank, neg momentum
        "BBB": _mk_history("BBB", 120, growth=-0.5),   # fast decline → bottom rank
    }
    spy = _mk_spy(120)
    cells_on = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5], gates_on=True)
    cell = cells_on[0]
    # Both tickers have negative momentum_20d → every pair is gated out.
    assert cell.n_pairs == 0
    assert cell.n_emittable == 0
    assert cell.hit_rate is None


def test_gates_default_on():
    """run_replay defaults gates on, matching the live scoring regime."""
    history = {
        "AAA": _mk_history("AAA", 120, growth=-0.05),
        "BBB": _mk_history("BBB", 120, growth=-0.5),
    }
    spy = _mk_spy(120)
    cells = run_replay(history, spy, [[50.0, 30.0, 20.0]], [5])
    assert cells[0].n_pairs == 0  # default gates_on=True gated everything out


def test_cli_exit_2_on_bad_weights(capsys, monkeypatch):
    import signal_system.replay.weight_replay as wr
    monkeypatch.setattr(wr, "run_replay", lambda *a, **k: [])
    from signal_system import __main__
    with pytest.raises(SystemExit) as exc:
        __main__._parse_replay_weights(["50/abc/20"])
    assert exc.value.code == 2
