"""Offline Discovery weight replay — empirical weight tuning without waiting a quarter.

Purely offline: no heartbeat, no runs row, no DB access. Fetches history via
yahoo_client (SPY in the same batch), then replays the live cross-sectional
scoring formula day-by-day across candidate weight sets and horizons, pooling
forward-excess-vs-SPY pairs into per-cell IC / hit-rate metrics.

The composite formula and gate constants are IMPORTED from discovery_agent —
never re-declared — so the replay measures the exact live scoring regime.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from signal_system.data import yahoo_client
from signal_system.data.universe import get_all_tickers
from signal_system.discovery.discovery_agent import (
    ABS_GATE_INFO_MOMENTUM_20D,
    SCORE_THRESHOLD_INFORM,
    _compute_factors,
    _rank_values,
)
from signal_system.review.stats import spearman_ic

# Live composite weights (initial guesses) — the default weight set.
_DEFAULT_WEIGHTS: list[float] = [50.0, 30.0, 20.0]
_LOOKBACK_ROWS: int = 21  # _compute_factors tails to 21 rows; need that many lookback days


@dataclass(frozen=True, slots=True)
class ReplayCell:
    """One (weight set, horizon) cell of the replay table."""

    weights: tuple[float, ...]
    horizon: int
    ic: float | None
    hit_rate: float | None
    n_pairs: int
    n_emittable: int
    spy_dropped: int


def parse_weights(spec: str) -> list[float]:
    """Parse a '50/30/20' weight spec into a list of floats.

    The live composite is exactly three factors (momentum_20d / momentum_5d /
    range_vs_20d), so a spec must be exactly three non-zero integer tokens.
    Raises ValueError otherwise.
    """
    parts = spec.split("/")
    if len(parts) != 3:
        raise ValueError(
            f"weight spec must be exactly 3 factors (e.g. 50/30/20), got {spec!r}"
        )
    weights: list[float] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            raise ValueError(f"invalid weight token {part!r} in {spec!r}") from None
        if value <= 0:
            raise ValueError(f"weights must be positive, got {part!r} in {spec!r}")
        weights.append(float(value))
    return weights


def _close_on_or_after(df: pd.DataFrame, date_iso: str) -> float | None:
    """Return the close for the first stored trading day >= date_iso, or None."""
    if df is None or df.empty:
        return None
    index = df.index
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    mask = index.normalize() >= pd.Timestamp(date_iso)
    matching = df[mask]
    if matching.empty:
        return None
    return float(matching.iloc[0]["Close"])


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with a tz-naive, date-only index."""
    index = df.index
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    out = df.copy()
    out.index = index.normalize()
    return out


def _forward_excess(
    df: pd.DataFrame, spy: pd.DataFrame, i: int, horizon: int
) -> tuple[float | None, bool]:
    """Forward excess of row *i* over *horizon* trading days vs SPY.

    The ticker's forward date is its own i+horizon row's date; SPY return is
    measured on matching calendar dates (first SPY close at/after the signal
    and forward dates — same convention as outcome backfill). Returns
    (excess, spy_dropped).
    """
    index = df.index
    signal_date = index[i].date().isoformat()

    ticker_now = float(df.iloc[i]["Close"])
    ticker_then = float(df.iloc[i + horizon]["Close"])
    forward_date = index[i + horizon].date().isoformat()

    spy_now = _close_on_or_after(spy, signal_date)
    spy_then = _close_on_or_after(spy, forward_date)
    if spy_now is None or spy_then is None or spy_now <= 0:
        return None, True

    ticker_ret = ticker_then / ticker_now - 1.0 if ticker_now > 0 else 0.0
    spy_ret = spy_then / spy_now - 1.0
    return ticker_ret - spy_ret, False


def run_replay(
    history: dict[str, pd.DataFrame],
    spy: pd.DataFrame,
    weight_sets: list[list[float]] | None = None,
    horizons: list[int] | None = None,
    gates_on: bool = False,
) -> list[ReplayCell]:
    """Replay cross-sectional Discovery scoring across weight sets and horizons.

    Iterates the union of trading days. On each day the factors and
    cross-sectional ranks are computed ONCE from that day's window (tail 21)
    and shared across every weight set, exactly like the live score_universe.
    Pairs (composite, forward excess) are pooled across days per cell.

    Per cell:
      - ic: Spearman over all pooled pairs (needs >=10 by spearman_ic).
      - n_emittable / hit_rate: the would-be-emitted set (composite >=
        SCORE_THRESHOLD_INFORM, and momentum_20d >= ABS_GATE_INFO_MOMENTUM_20D
        when gates are on); hit_rate = fraction with positive excess.
      - spy_dropped: pairs skipped because SPY lacked a matching date.

    Missing SPY dates drop the pair and count toward spy_dropped rather than
    being silently excluded.
    """
    if weight_sets is None:
        weight_sets = [_DEFAULT_WEIGHTS]
    if horizons is None:
        horizons = [5, 20]

    weight_keys = [tuple(ws) for ws in weight_sets]
    cells_map: dict[tuple[tuple[float, ...], int], ReplayCell] = {}
    pair_buckets: dict[tuple[tuple[float, ...], int], list[tuple[float, float]]] = {
        (wk, h): [] for wk in weight_keys for h in horizons
    }
    emit_counts: dict[tuple[tuple[float, ...], int], int] = {
        (wk, h): 0 for wk in weight_keys for h in horizons
    }
    spy_dropped: dict[tuple[tuple[float, ...], int], int] = {
        (wk, h): 0 for wk in weight_keys for h in horizons
    }

    spy_norm = _normalize_index(spy)
    tickers = [t for t in history if history[t] is not None and not history[t].empty]
    if not tickers or spy_norm.empty:
        return []

    # Build per-ticker normalized frames once.
    frames: dict[str, pd.DataFrame] = {t: _normalize_index(history[t]) for t in tickers}

    # Union of trading dates across all tickers (and SPY).
    all_dates = sorted({d for t in tickers for d in frames[t].index})
    if not all_dates:
        return []

    for day in all_dates:
        window: dict[str, pd.DataFrame] = {}
        for t in tickers:
            frame = frames[t]
            upto = frame.loc[:day]
            if len(upto) < _LOOKBACK_ROWS:
                continue
            window[t] = upto.tail(_LOOKBACK_ROWS)
        if len(window) < 2:
            continue

        factors_by_ticker: dict[str, dict[str, float]] = {}
        for t, w in window.items():
            factors = _compute_factors(w)
            if factors is not None:
                factors_by_ticker[t] = factors
        if len(factors_by_ticker) < 2:
            continue

        # Ranks computed ONCE per day — shared across every weight set.
        m20_ranks = _rank_values({t: f["momentum_20d"] for t, f in factors_by_ticker.items()})
        m5_ranks = _rank_values({t: f["momentum_5d"] for t, f in factors_by_ticker.items()})
        range_ranks = _rank_values({t: f["range_vs_20d"] for t, f in factors_by_ticker.items()})

        for t, f in factors_by_ticker.items():
            rank_tuple = (m20_ranks[t], m5_ranks[t], range_ranks[t])
            for wk in weight_keys:
                weight_sum = sum(wk)
                composite = 100.0 * sum(w * r for w, r in zip(wk, rank_tuple)) / weight_sum
                for horizon in horizons:
                    frame = frames[t]
                    i = frame.index.get_indexer([day])[0]
                    if i + horizon >= len(frame):
                        continue
                    excess, dropped = _forward_excess(frame, spy_norm, i, horizon)
                    key = (wk, horizon)
                    if dropped:
                        spy_dropped[key] += 1
                        continue
                    pair_buckets[key].append((composite, excess))
                    gated = gates_on and f["momentum_20d"] < ABS_GATE_INFO_MOMENTUM_20D
                    if composite >= SCORE_THRESHOLD_INFORM and not gated:
                        emit_counts[key] += 1

    cells: list[ReplayCell] = []
    for wk in weight_keys:
        for horizon in horizons:
            key = (wk, horizon)
            pairs = pair_buckets[key]
            scores = [p[0] for p in pairs]
            excesses = [p[1] for p in pairs]
            ic = spearman_ic(scores, excesses)
            # Hit rate over the would-be-emitted set (composite >= 60, gate-filtered).
            emittable_excesses = [
                p[1]
                for p in pairs
                if p[0] >= SCORE_THRESHOLD_INFORM
            ]
            hit_rate = (
                sum(1 for e in emittable_excesses if e > 0) / len(emittable_excesses)
                if emittable_excesses
                else None
            )
            cells.append(
                ReplayCell(
                    weights=wk,
                    horizon=horizon,
                    ic=ic,
                    hit_rate=hit_rate,
                    n_pairs=len(pairs),
                    n_emittable=emit_counts[key],
                    spy_dropped=spy_dropped[key],
                )
            )
    return cells


def fetch_replay_inputs(days: int) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Fetch universe + SPY history in one yfinance batch (no DB access)."""
    tickers = get_all_tickers()
    history = yahoo_client.fetch_history(tickers + ["SPY"], days=days)
    spy = history.pop("SPY", pd.DataFrame())
    return history, spy


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _fmt_ic(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def render_replay_table(cells: list[ReplayCell], gates_on: bool) -> str:
    """Render the replay stdout table plus mandatory caveat lines."""
    header = f"{'weights':<14} {'horizon':<8} {'IC':<8} {'hit_rate':<9} {'n_pairs':<8} {'n_emit':<7} {'spy_drop':<9}"
    lines = [header, "-" * len(header)]
    for cell in sorted(cells, key=lambda c: (c.weights, c.horizon)):
        weights_str = "/".join(str(int(w)) for w in cell.weights)
        lines.append(
            f"{weights_str:<14} {cell.horizon:<8} {_fmt_ic(cell.ic):<8} "
            f"{_fmt_pct(cell.hit_rate):<9} {cell.n_pairs:<8} "
            f"{cell.n_emittable:<7} {cell.spy_dropped:<9}"
        )
    lines.extend(
        [
            "",
            "CAVEATS:",
            "- Survivorship bias: this replays TODAY's universe (and only tickers still listed),",
            "  not the universe as it stood each historical day.",
            "- IC is a pooled-pairs correlation across days, not a per-day average.",
            f"- Gates are {'ON' if gates_on else 'OFF'} (absolute momentum_20d >= "
            f"{ABS_GATE_INFO_MOMENTUM_20D:.2f} to emit).",
            "- Do not over-trust a single window; use replay before quarterly weight review only.",
        ]
    )
    return "\n".join(lines)


def run_replay_cli(
    *,
    days: int,
    weight_specs: list[str],
    horizons: list[int],
    gates_on: bool,
) -> int:
    """Replay entry point for the replay-weights operator command. No heartbeat."""
    weight_sets = [parse_weights(spec) for spec in weight_specs]
    history, spy = fetch_replay_inputs(days=days)
    cells = run_replay(history, spy, weight_sets=weight_sets, horizons=horizons, gates_on=gates_on)
    print(render_replay_table(cells, gates_on))
    return 0
