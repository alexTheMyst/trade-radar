"""Exit Sentinel — pure mechanical exit-trigger engine for held positions.

Computes three raw magnitudes from a ticker's recent candle history and cost
basis, maps breaches to scores and severity, and builds one Signal per ticker
per run. All functions are pure (no I/O) so the trigger logic is unit-testable
and the job wrapper owns data fetching and delivery.

Trigger taxonomy (priority order):
    drawdown_20d — drawdown of current close from the 20-day high
    day_drop_1d  — one-day close-to-close drop
    below_cost   — current close below the position's cost basis

Severity is rule-determined (not score-driven): ACTION_REQUIRED iff drawdown
>= DRAWDOWN_SEVERE or day_drop >= DAY_DROP_SEVERE; everything else that
breaches an INFO threshold is INFORMATIONAL. The weight amplifier is
deliberately unused — a drawdown is a drawdown regardless of position weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from signal_system.data.holdings import Holding
from signal_system.models import (
    EXIT_SENTINEL_AGENT,
    Severity,
    Signal,
    compute_alert_id,
)

# --- Tunable thresholds (initial guesses — quarterly review) ---
DRAWDOWN_INFO: float = 0.10
DRAWDOWN_SEVERE: float = 0.15
DAY_DROP_INFO: float = 0.05
DAY_DROP_SEVERE: float = 0.08
BELOW_COST_INFO: float = 0.05
BELOW_COST_SCORE_NORM: float = 0.15  # 100 * below_cost / 0.15 (a 15% below-cost hits 100)

WEIGHT_VERSION: str = "sentinel-v1"
HISTORY_DAYS: int = 26
COOLDOWN_TRADING_DAYS: int = 5

# Priority order for tie-breaks when magnitudes tie at equal score contribution.
TRIGGER_PRIORITY: tuple[str, str, str] = ("drawdown_20d", "day_drop_1d", "below_cost")

_INFO_THRESHOLDS: dict[str, float] = {
    "drawdown_20d": DRAWDOWN_INFO,
    "day_drop_1d": DAY_DROP_INFO,
    "below_cost": BELOW_COST_INFO,
}
_SEVERE_NORMS: dict[str, float] = {
    "drawdown_20d": DRAWDOWN_SEVERE,
    "day_drop_1d": DAY_DROP_SEVERE,
    "below_cost": BELOW_COST_SCORE_NORM,
}
_SEVERE_FLOORS: dict[str, float] = {
    "drawdown_20d": DRAWDOWN_SEVERE,
    "day_drop_1d": DAY_DROP_SEVERE,
    "below_cost": float("inf"),  # below-cost never reaches ACTION_REQUIRED
}

_MIN_TRADING_DAYS: int = 5
_TAIL_ROWS: int = 21


def compute_exit_triggers(
    df: pd.DataFrame, cost_basis: float
) -> dict[str, float] | None:
    """Compute raw trigger magnitudes from a candle DataFrame.

    Uses ``df.tail(21)`` so drawdown_20d is measured from the 20-day high and
    day_drop_1d is the last close-to-close move. Returns None when the frame
    has fewer than *_MIN_TRADING_DAYS* rows.

    Magnitudes are raw fractions (0.05 = 5%); thresholds live in the callers.
    """
    if df is None or len(df) < _MIN_TRADING_DAYS:
        return None

    df = df.tail(_TAIL_ROWS)
    closes = df["Close"].values
    highs = df["High"].values

    current_close = float(closes[-1])
    high_20d = float(highs.max())

    drawdown_20d = (high_20d - current_close) / high_20d if high_20d > 0 else 0.0

    prev_close = float(closes[-2])
    day_drop_1d = (prev_close - current_close) / prev_close if prev_close > 0 else 0.0

    if cost_basis and cost_basis > 0 and current_close < cost_basis:
        below_cost = (cost_basis - current_close) / cost_basis
    else:
        below_cost = 0.0

    return {
        "drawdown_20d": drawdown_20d,
        "day_drop_1d": day_drop_1d,
        "below_cost": below_cost,
    }


def score_breaches(
    triggers: dict[str, float],
) -> tuple[float, str, dict[str, float]] | None:
    """Score the breached triggers on a unified 0-100 scale.

    Returns ``(score, primary, breached)`` where *breached* maps trigger name
    to magnitude for every trigger at or above its INFO threshold, *primary*
    is the highest-scoring breach (priority tiebreak), and *score* is
    100 * min(1, max(magnitude/severe_norm over breached)) — a severe breach
    pins at 100. Returns None when nothing breaches its INFO threshold.
    """
    breached = {
        name: mag
        for name, mag in triggers.items()
        if name in _INFO_THRESHOLDS and mag >= _INFO_THRESHOLDS[name]
    }
    if not breached:
        return None

    primary = max(
        breached,
        key=lambda name: (breached[name] / _SEVERE_NORMS[name], -TRIGGER_PRIORITY.index(name)),
    )
    score = 100.0 * min(1.0, max(breached[name] / _SEVERE_NORMS[name] for name in breached))
    return score, primary, breached


def severity_for(triggers: dict[str, float]) -> Severity:
    """Map trigger magnitudes to severity.

    ACTION_REQUIRED iff drawdown >= DRAWDOWN_SEVERE or day_drop >=
    DAY_DROP_SEVERE; otherwise INFORMATIONAL (the caller only reaches this
    function for at-least-INFO breaches). The weight amplifier is deliberately
    unused — severe breaches are rule-determined, not position-weighted.
    """
    for name in TRIGGER_PRIORITY:
        if name == "below_cost":
            continue
        if triggers.get(name, 0.0) >= _SEVERE_FLOORS[name]:
            return "ACTION_REQUIRED"
    return "INFORMATIONAL"


def build_signal(
    *,
    ticker: str,
    triggers: dict[str, float],
    cost_basis: float,
    date_iso: str,
    snapshot: float,
) -> Signal:
    """Build the single sentinel Signal for one ticker.

    One Signal per ticker per run — multiple triggers on a crash day ride in
    the body rather than triple-billing the shared INFO budget. The snapshot
    MUST be non-null: rows without one are invisible to the review report's
    snapshot filter and can never be measured.
    """
    if not snapshot or snapshot <= 0:
        raise ValueError(f"exit sentinel requires a non-null price snapshot for {ticker}")

    scored = score_breaches(triggers)
    if scored is None:
        return _no_breach_signal(ticker, triggers, cost_basis, date_iso, snapshot)

    score, primary, breached = scored
    severity = severity_for(triggers)

    secondary = [
        f"{name}={mag:.1%}"
        for name, mag in breached.items()
        if name != primary
    ]
    body = f"cost_basis={cost_basis:.2f}"
    if secondary:
        body += " | " + ", ".join(secondary)

    alert_id = compute_alert_id(ticker, date_iso, f"exit:{primary}", EXIT_SENTINEL_AGENT)
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=EXIT_SENTINEL_AGENT,
        timestamp=datetime.now(),
        alert_id=alert_id,
        title=f"{ticker}: {primary} breach {breached[primary]:.1%}",
        body=body,
        signal_price_snapshot=snapshot,
        direction="negative",
        weight_version=WEIGHT_VERSION,
    )


def _no_breach_signal(
    ticker: str,
    triggers: dict[str, float],
    cost_basis: float,
    date_iso: str,
    snapshot: float,
) -> Signal:
    """Build a MONITORING-eligible fallback for a trigger-less frame.

    Keeps build_signal total: a ticker that clears all thresholds still needs
    a deterministic return. The job normally filters these out before routing.
    """
    primary = next(
        (name for name in TRIGGER_PRIORITY if triggers.get(name, 0.0) > 0),
        "drawdown_20d",
    )
    alert_id = compute_alert_id(ticker, date_iso, f"exit:{primary}", EXIT_SENTINEL_AGENT)
    return Signal(
        ticker=ticker,
        score=0.0,
        severity="MONITORING",
        agent=EXIT_SENTINEL_AGENT,
        timestamp=datetime.now(),
        alert_id=alert_id,
        title=f"[no_breach] {ticker}",
        body=f"cost_basis={cost_basis:.2f}",
        signal_price_snapshot=snapshot,
        direction="negative",
        weight_version=WEIGHT_VERSION,
    )


def prior_alert_ids(
    ticker: str,
    primary: str,
    date_iso: str,
    trading_dates: list[str],
    days: int = COOLDOWN_TRADING_DAYS,
) -> list[str]:
    """Reconstruct sentinel alert_ids for the *days* trading dates before date_iso.

    The cooldown looks at prior trading dates from the ticker's own candle
    index, not calendar dates — a Monday run skips the weekend because Friday
    is the only prior trading day. Reconstructing compute_alert_id per date x
    the same ``exit:{primary}`` rule lets the job query by deterministic ID
    instead of by score-threshold fuzzy matching.
    """
    prior = [d for d in trading_dates if d < date_iso][-days:]
    return [
        compute_alert_id(ticker, d, f"exit:{primary}", EXIT_SENTINEL_AGENT)
        for d in prior
    ]


def is_cooldown(prior_meta: list[dict], new_severity: str) -> bool:
    """Decide whether a fresh trigger is downgraded by the cooldown.

    Any routable prior sentinel row in the window downgrades the new trigger
    to a ``[cooldown]`` MONITORING row — UNLESS the new severity is
    ACTION_REQUIRED and no prior row was itself severe (severity or
    demoted_from ACTION_REQUIRED). Escalation always gets through: a position
    that was merely informational five days ago and now breaches the severe
    threshold must alert.

    MONITORING rows are excluded by get_signal_meta_by_alert_ids, so prior
    cooldowns never re-suppress — a fail-open re-alert after a prior cooldown
    is accepted (documented tradeoff: we'd rather over-alert on a falling
    position than silently go quiet).
    """
    if not prior_meta:
        return False
    prior_was_severe = any(
        m.get("severity") == "ACTION_REQUIRED"
        or m.get("demoted_from") == "ACTION_REQUIRED"
        for m in prior_meta
    )
    if new_severity == "ACTION_REQUIRED" and not prior_was_severe:
        return False
    return True


def dedupe_holdings(holdings: list[Holding]) -> list[Holding]:
    """Merge per-account rows into per-ticker positions with weighted cost basis.

    The same ticker held across the 4 Schwab accounts produces multiple rows;
    the sentinel evaluates the position, not the account. Cost basis is
    share-weighted across accounts (documented choice: an alert is about the
    blended position, and the operator decides which account to act in).
    """
    by_ticker: dict[str, list[Holding]] = {}
    for holding in holdings:
        by_ticker.setdefault(holding.ticker, []).append(holding)

    merged: list[Holding] = []
    for ticker, rows in by_ticker.items():
        total_shares = sum(h.shares for h in rows)
        if total_shares <= 0:
            continue
        weighted_basis = sum(h.shares * h.cost_basis for h in rows) / total_shares
        merged.append(
            Holding(
                ticker=ticker,
                shares=total_shares,
                cost_basis=weighted_basis,
                account=",".join(sorted({h.account for h in rows})),
            )
        )
    return merged
