"""Plan 12a / 12c2 / 12f — sentinel lanes, reserved slot, macro cap, regime."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from signal_system.models import ROUTING_REGIME, Signal, compute_alert_id
from signal_system.router import (
    SENTINEL_RESERVED_INFO,
    dispatch_candidates,
    stage_signals,
)
from signal_system.state import repository

ET = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _sig(
    ticker: str | None,
    *,
    agent: str = "discovery_agent",
    severity: str = "INFORMATIONAL",
    score: float = 80.0,
    triggers: dict[str, float] | None = None,
) -> Signal:
    ts = datetime.now(ET)
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), f"r-{severity}", agent),
        title=f"{ticker or 'MACRO'}: {agent} {severity}",
        trigger_json=json.dumps(triggers) if triggers is not None else None,
    )


def test_sentinel_ar_delivers_when_news_lane_already_spent(monkeypatch):
    monkeypatch.setattr(
        repository,
        "count_delivered_today",
        lambda agent=None: {"ACTION_REQUIRED": 1} if agent == "news_classifier" else {},
    )
    sentinel = _sig(
        "CAN", agent="exit_sentinel", severity="ACTION_REQUIRED", score=100.0,
        triggers={"drawdown_20d": 0.483},
    )

    [(sig, status, dmf, _reason)] = stage_signals([sentinel])

    assert (sig.alert_id, status, dmf) == (sentinel.alert_id, "DELIVERED", None)


def test_second_sentinel_ar_same_day_is_demoted(monkeypatch):
    monkeypatch.setattr(
        repository,
        "count_delivered_today",
        lambda agent=None: {"ACTION_REQUIRED": 1} if agent == "exit_sentinel" else {},
    )
    sentinel = _sig(
        "CRWV", agent="exit_sentinel", severity="ACTION_REQUIRED", score=100.0,
        triggers={"drawdown_20d": 0.337},
    )

    [(sig, status, dmf, _reason)] = stage_signals([sentinel])

    assert (sig.severity, status, dmf) == ("INFORMATIONAL", "CANDIDATE", "ACTION_REQUIRED")


def test_sentinel_lane_ranks_by_unclamped_severity_ratio(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    weaker = _sig(
        "AVGO", agent="exit_sentinel", severity="ACTION_REQUIRED", score=100.0,
        triggers={"drawdown_20d": 0.151},
    )
    stronger = _sig(
        "CDE", agent="exit_sentinel", severity="ACTION_REQUIRED", score=100.0,
        triggers={"drawdown_20d": 0.306},
    )

    out = {s.ticker: (s, rs, dmf, r) for s, rs, dmf, r in stage_signals([weaker, stronger])}

    assert out["CDE"][1:] == ("DELIVERED", None, None)
    assert out["AVGO"][1:] == ("CANDIDATE", "ACTION_REQUIRED", None)


def test_macro_news_ar_is_capped_to_candidate(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    macro = _sig(None, agent="news_classifier", severity="ACTION_REQUIRED", score=0.95)
    tickered = _sig("NVDA", agent="news_classifier", severity="ACTION_REQUIRED", score=0.90)

    out = {s.ticker: (s, rs, dmf, r) for s, rs, dmf, r in stage_signals([macro, tickered])}

    assert out[None][1:] == ("CANDIDATE", "macro_cap", None)
    assert out[None][0].severity == "INFORMATIONAL"
    assert out["NVDA"][1:] == ("DELIVERED", None, None)


def test_dispatch_reserves_one_slot_for_fresh_sentinel(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    news = [
        _sig(t, agent="news_classifier", score=0.9 - i * 0.05)
        for i, t in enumerate(["N1", "N2", "N3", "N4", "N5"])
    ]
    sentinel = _sig(
        "MU", agent="exit_sentinel", score=100.0,
        triggers={"drawdown_20d": 0.208},
    )

    routed = dispatch_candidates([(s, None) for s in [*news, sentinel]])
    delivered = [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"]

    assert delivered == ["MU", "N1", "N2"]
    assert sum(1 for s in routed if s[1] == "DELIVERED" and s[0].agent == "exit_sentinel") == SENTINEL_RESERVED_INFO


def test_below_cost_only_sentinel_does_not_reserve(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    news = [
        _sig(f"N{i}", agent="news_classifier", score=0.9 - i * 0.05) for i in range(5)
    ]
    sentinel = _sig(
        "T", agent="exit_sentinel", score=100.0, triggers={"below_cost": 0.276}
    )

    routed = dispatch_candidates([(s, None) for s in [*news, sentinel]])
    delivered = [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"]

    assert delivered == ["N0", "N1", "N2"]


def test_fresh_sentinels_rank_by_unclamped_ratio(monkeypatch):
    monkeypatch.setattr(
        repository, "count_delivered_today", lambda agent=None: {"INFORMATIONAL": 2}
    )
    weaker = _sig(
        "AVGO", agent="exit_sentinel", score=100.0,
        triggers={"drawdown_20d": 0.151},
    )
    stronger = _sig(
        "CDE", agent="exit_sentinel", score=100.0,
        triggers={"drawdown_20d": 0.306},
    )

    routed = dispatch_candidates([(weaker, None), (stronger, None)])
    delivered = [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"]

    assert delivered == ["CDE"]


def test_news_dispatch_orders_held_then_nonheld_then_macro(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    held = _sig("HELD", agent="news_classifier", score=0.80)
    other = _sig("OTHER", agent="news_classifier", score=0.90)
    macro = _sig(None, agent="news_classifier", score=0.95)

    routed = dispatch_candidates(
        [(s, None) for s in [macro, other, held]], held_tickers={"HELD"}
    )
    delivered = [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"]

    assert delivered == ["HELD", "OTHER", None]


def test_validate_dispatch_requires_suppressed_sentinel_section():
    from signal_system.jobs.dispatch import validate_dispatch

    sentinel = _sig(
        "JNUG", agent="exit_sentinel", score=100.0,
        triggers={"drawdown_20d": 0.303},
    )
    routed = [(sentinel, "SUPPRESSED", None, "outscored")]
    with pytest.raises(RuntimeError, match="suppressed sentinel"):
        validate_dispatch(
            routed=routed,
            db_counts={"SUPPRESSED": 1},
            body="Suppressed: 1",
            sentinel_suppressed_db=1,
        )


def test_candidate_readback_includes_trigger_json(db):
    sentinel = _sig(
        "MU", agent="exit_sentinel", severity="INFORMATIONAL", score=100.0,
        triggers={"drawdown_20d": 0.208},
    )
    repository.insert_signal(sentinel, routing_status="CANDIDATE")

    [(back, _dmf)] = repository.list_candidates_for_date(
        sentinel.timestamp.date().isoformat()
    )

    assert back.trigger_json == json.dumps({"drawdown_20d": 0.208})


def test_monitoring_inserts_are_regime_stamped(db):
    sig = _sig("NVDA", agent="news_classifier", severity="MONITORING")
    repository.insert_signal(sig, routing_status="MONITORING")

    conn = repository._connect()
    try:
        regime = conn.execute(
            "SELECT routing_regime FROM signals WHERE alert_id = ?", (sig.alert_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert regime == ROUTING_REGIME
