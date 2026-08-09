"""Tests for the pure exit-sentinel trigger engine.

compute_exit_triggers/score_breaches/severity_for/build_signal are pure —
no I/O. Threshold boundaries are >= (breach at exactly the threshold), scores
are 100 * min(1, max(magnitude/severe_norm)), severity is rule-determined.
primary comes from score_breaches(); compute_exit_triggers() returns raw
magnitudes only.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from signal_system.sentinel.exit_sentinel import (
    BELOW_COST_INFO,
    BELOW_COST_SCORE_NORM,
    DAY_DROP_INFO,
    DAY_DROP_SEVERE,
    DRAWDOWN_INFO,
    DRAWDOWN_SEVERE,
    EXIT_SENTINEL_AGENT,
    compute_exit_triggers,
    build_signal,
    dedupe_holdings,
    is_cooldown,
    prior_alert_ids,
    score_breaches,
    severity_for,
)
from signal_system.data.holdings import Holding
from signal_system.models import compute_alert_id

_ET = ZoneInfo("America/New_York")


def _df(closes: list[float], *, base=datetime(2026, 5, 1)) -> pd.DataFrame:
    index = pd.date_range(base, periods=len(closes), freq="B")
    return pd.DataFrame(
        {"Close": closes, "High": closes, "Low": closes},
        index=index,
    )


def test_drawdown_from_20d_high_raw_magnitude():
    """Drawdown is measured from the 20-day high, not a fixed base."""
    closes = [100.0] * 15 + [90.0, 85.0]
    triggers = compute_exit_triggers(_df(closes), cost_basis=50.0)
    assert triggers["drawdown_20d"] == pytest.approx(0.15)


def test_day_drop_1d_magnitude():
    closes = [100.0] * 19 + [95.0, 87.0]
    triggers = compute_exit_triggers(_df(closes), cost_basis=50.0)
    assert triggers["day_drop_1d"] == pytest.approx((95.0 - 87.0) / 95.0)


def test_below_cost_magnitude():
    closes = [100.0] * 21
    triggers = compute_exit_triggers(_df(closes), cost_basis=110.0)
    assert triggers["below_cost"] == pytest.approx((110.0 - 100.0) / 110.0)


def test_tie_priority_drawdown_over_day_drop():
    """On a crash day drawdown dominates: its severe-ratio is larger."""
    closes = [100.0] * 19 + [50.0, 47.0]
    triggers = compute_exit_triggers(_df(closes), cost_basis=10.0)
    score, primary, breached = score_breaches(triggers)
    assert primary == "drawdown_20d"
    assert score == pytest.approx(100.0)


def test_short_history_returns_none():
    assert compute_exit_triggers(_df([100.0] * 4), cost_basis=50.0) is None


def test_exact_thresholds_breach():
    """Breach is >=: exactly at the INFO threshold scores > 0."""
    closes = [100.0] * 20 + [90.0]
    triggers = compute_exit_triggers(_df(closes), cost_basis=50.0)
    assert triggers["drawdown_20d"] == pytest.approx(DRAWDOWN_INFO)
    score, primary, breached = score_breaches(triggers)
    assert breached["drawdown_20d"]
    assert score > 0


def test_score_pins():
    """12% drawdown → 80; 5% day-drop → 62.5; severe → 100 cap."""
    dd = {"drawdown_20d": 0.12, "day_drop_1d": 0.0, "below_cost": 0.0}
    score, primary, _ = score_breaches(dd)
    assert primary == "drawdown_20d"
    assert score == pytest.approx(80.0)

    ddrop = {"drawdown_20d": 0.0, "day_drop_1d": 0.05, "below_cost": 0.0}
    score, primary, _ = score_breaches(ddrop)
    assert primary == "day_drop_1d"
    assert score == pytest.approx(100.0 * 0.05 / DAY_DROP_SEVERE)

    severe = {"drawdown_20d": 0.30, "day_drop_1d": 0.0, "below_cost": 0.0}
    score, primary, _ = score_breaches(severe)
    assert primary == "drawdown_20d"
    assert score == pytest.approx(100.0)

    bc = {"drawdown_20d": 0.0, "day_drop_1d": 0.0, "below_cost": BELOW_COST_INFO}
    score, primary, _ = score_breaches(bc)
    assert primary == "below_cost"
    assert score == pytest.approx(100.0 * BELOW_COST_INFO / BELOW_COST_SCORE_NORM)


def test_no_breach_returns_none():
    triggers = {"drawdown_20d": 0.03, "day_drop_1d": 0.01, "below_cost": 0.02}
    assert score_breaches(triggers) is None


def test_severity_mapping():
    """AR iff drawdown >= 0.15 or day_drop >= 0.08; else INFORMATIONAL."""
    info = {"drawdown_20d": 0.149, "day_drop_1d": 0.0, "below_cost": 0.0}
    assert severity_for(info) == "INFORMATIONAL"
    ar = {"drawdown_20d": 0.15, "day_drop_1d": 0.0, "below_cost": 0.0}
    assert severity_for(ar) == "ACTION_REQUIRED"
    ar_drop = {"drawdown_20d": 0.0, "day_drop_1d": 0.08, "below_cost": 0.0}
    assert severity_for(ar_drop) == "ACTION_REQUIRED"
    # below-cost never reaches AR
    bc = {"drawdown_20d": 0.0, "day_drop_1d": 0.0, "below_cost": 0.30}
    assert severity_for(bc) == "INFORMATIONAL"


def test_build_signal_one_per_ticker_with_snapshot():
    triggers = {"drawdown_20d": 0.12, "day_drop_1d": 0.05, "below_cost": 0.0}
    signal = build_signal(
        ticker="AAPL",
        triggers=triggers,
        cost_basis=100.0,
        date_iso="2026-06-01",
        snapshot=88.0,
    )
    assert signal.agent == EXIT_SENTINEL_AGENT
    assert signal.direction == "negative"
    assert signal.severity == "INFORMATIONAL"
    assert signal.signal_price_snapshot == 88.0
    assert signal.weight_version == "sentinel-v1"
    assert signal.ticker == "AAPL"
    assert signal.score == pytest.approx(80.0)
    assert "drawdown" in signal.title.lower()


def test_build_signal_secondary_breaches_ride_in_body():
    """The primary breach names the alert_id; secondary breaches are in the body."""
    triggers = {"drawdown_20d": 0.12, "day_drop_1d": 0.06, "below_cost": 0.0}
    signal = build_signal(
        ticker="MSFT",
        triggers=triggers,
        cost_basis=100.0,
        date_iso="2026-06-01",
        snapshot=88.0,
    )
    assert "day_drop" in signal.body
    expected_id = compute_alert_id("MSFT", "2026-06-01", "exit:drawdown_20d", EXIT_SENTINEL_AGENT)
    assert signal.alert_id == expected_id


def test_build_signal_timestamp_is_et_aware():
    """Signal timestamps must be ET-aware — the DB date-prefix logic depends on it."""
    triggers = {"drawdown_20d": 0.12, "day_drop_1d": 0.0, "below_cost": 0.0}
    signal = build_signal(
        ticker="AAPL",
        triggers=triggers,
        cost_basis=100.0,
        date_iso="2026-06-01",
        snapshot=88.0,
    )
    assert signal.timestamp.tzinfo is not None
    assert signal.timestamp.utcoffset() is not None


def test_dedupe_holdings_weighted_basis():
    holdings = [
        Holding(ticker="AAPL", shares=100, cost_basis=90.0, account="schwab_main"),
        Holding(ticker="AAPL", shares=100, cost_basis=110.0, account="roth_ira"),
    ]
    merged = dedupe_holdings(holdings)
    assert len(merged) == 1
    assert merged[0].shares == 200
    assert merged[0].cost_basis == pytest.approx(100.0)


def test_dedupe_holdings_multiple_tickers():
    holdings = [
        Holding(ticker="AAPL", shares=100, cost_basis=90.0, account="schwab_main"),
        Holding(ticker="MSFT", shares=50, cost_basis=200.0, account="schwab_main"),
    ]
    merged = dedupe_holdings(holdings)
    assert sorted(h.ticker for h in merged) == ["AAPL", "MSFT"]


def test_prior_alert_ids_uses_trading_dates_only():
    """Cooldown uses the ticker's own trading dates, not calendar dates."""
    trading_dates = ["2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
    ids = prior_alert_ids("AAPL", "drawdown_20d", "2026-06-01", trading_dates)
    assert len(ids) == 5
    for d, alert_id in zip(trading_dates, ids):
        assert alert_id == compute_alert_id(
            "AAPL", d, "exit:drawdown_20d", EXIT_SENTINEL_AGENT
        )


def test_prior_alert_ids_caps_at_cooldown_window():
    trading_dates = [f"2026-05-{d:02d}" for d in range(1, 20)]
    ids = prior_alert_ids("MSFT", "day_drop_1d", "2026-05-20", trading_dates)
    assert len(ids) == 5  # newest 5 trading dates only


def test_is_cooldown_false_with_no_prior():
    assert is_cooldown([], "INFORMATIONAL") is False


def test_is_cooldown_true_on_prior_info():
    prior = [{"severity": "INFORMATIONAL", "demoted_from": None}]
    assert is_cooldown(prior, "INFORMATIONAL") is True


def test_is_cooldown_escalation_bypasses():
    """New AR with only prior INFO rows must get through."""
    prior = [{"severity": "INFORMATIONAL", "demoted_from": None}]
    assert is_cooldown(prior, "ACTION_REQUIRED") is False


def test_is_cooldown_true_when_prior_was_severe():
    prior = [{"severity": "ACTION_REQUIRED", "demoted_from": None}]
    assert is_cooldown(prior, "INFORMATIONAL") is True


def test_is_cooldown_true_when_prior_was_demoted_severe():
    prior = [{"severity": "INFORMATIONAL", "demoted_from": "ACTION_REQUIRED"}]
    assert is_cooldown(prior, "ACTION_REQUIRED") is True
