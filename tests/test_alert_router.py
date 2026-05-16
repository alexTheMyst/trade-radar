"""Tests for the Alert Router — route_signals() covering ROUT-01..ROUT-05.

Tests T-AR-01 through T-AR-09 per D-19 test scenarios.
"""
from datetime import datetime, timedelta, timezone

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.router import route_signals
from signal_system.state import repository

DATE_ISO = "2026-05-16"
_TS = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc)


def _sig(
    ticker: str = "AAPL",
    score: float = 75.0,
    severity: str = "INFORMATIONAL",
    agent: str = "test_agent",
) -> Signal:
    """Build a minimal valid Signal for routing tests."""
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=_TS,
        alert_id=compute_alert_id(ticker, DATE_ISO, "test_rule", agent),
        title=f"{ticker}: test signal",
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


# ---------------------------------------------------------------------------
# T-AR-07: empty input
# ---------------------------------------------------------------------------

def test_empty_input(monkeypatch):
    """route_signals([]) returns [] without touching the DB."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    assert route_signals([]) == []


# ---------------------------------------------------------------------------
# T-AR-06: MONITORING raises ValueError
# ---------------------------------------------------------------------------

def test_monitoring_raises():
    """MONITORING signals must never enter the router."""
    sig = _sig(severity="MONITORING")
    with pytest.raises(ValueError, match="MONITORING"):
        route_signals([sig])


# ---------------------------------------------------------------------------
# T-AR-08: DELIVERED signals always have demoted_from=None
# ---------------------------------------------------------------------------

def test_delivered_demoted_from_is_none(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    sig = _sig(ticker="AAPL", score=80.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf = results[0]
    assert rs == "DELIVERED"
    assert dmf is None


# ---------------------------------------------------------------------------
# T-AR-01: 5 AR signals → 1 DELIVERED (highest score), 4 SUPPRESSED (outscored)
# ---------------------------------------------------------------------------

def test_ar_budget_one_winner(monkeypatch):
    """AR cap=1: highest score wins, all others are 'outscored'."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    signals = [
        _sig(ticker=f"T{i}", score=float(score), severity="ACTION_REQUIRED")
        for i, score in enumerate([50, 60, 70, 80, 90])
    ]
    results = route_signals(signals)
    assert len(results) == 5

    delivered = [(s, rs, dmf) for s, rs, dmf in results if rs == "DELIVERED"]
    suppressed = [(s, rs, dmf) for s, rs, dmf in results if rs == "SUPPRESSED"]

    assert len(delivered) == 1
    assert delivered[0][0].score == 90.0
    assert all(dmf == "outscored" for _, _, dmf in suppressed)


# ---------------------------------------------------------------------------
# T-AR-04: equal scores → alphabetical tiebreak is deterministic
# ---------------------------------------------------------------------------

def test_tiebreak_alphabetical(monkeypatch):
    """Equal-score AR signals: alphabetically first ticker wins."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    msft = _sig(ticker="MSFT", score=75.0, severity="ACTION_REQUIRED", agent="a1")
    aapl = Signal(
        ticker="AAPL", score=75.0, severity="ACTION_REQUIRED", agent="a2",
        timestamp=_TS,
        alert_id=compute_alert_id("AAPL", DATE_ISO, "test_rule", "a2"),
        title="AAPL: test signal",
    )
    results = route_signals([msft, aapl])
    delivered = [(s, rs, dmf) for s, rs, dmf in results if rs == "DELIVERED"]
    suppressed = [(s, rs, dmf) for s, rs, dmf in results if rs == "SUPPRESSED"]

    assert len(delivered) == 1
    assert delivered[0][0].ticker == "AAPL"
    assert suppressed[0][0].ticker == "MSFT"
    assert suppressed[0][2] == "outscored"


# ---------------------------------------------------------------------------
# T-AR-02: mixed batch — 2 AR + 5 INFO → 1 AR + 3 INFO DELIVERED, 3 SUPPRESSED
# ---------------------------------------------------------------------------

def test_mixed_batch_allocation(monkeypatch):
    """Mixed severity batch: 1 AR slot + 3 INFO slots filled; rest suppressed."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})

    ar_signals = [
        _sig(ticker="AR1", score=85.0, severity="ACTION_REQUIRED", agent="ag1"),
        Signal(
            ticker="AR2", score=75.0, severity="ACTION_REQUIRED", agent="ag2",
            timestamp=_TS,
            alert_id=compute_alert_id("AR2", DATE_ISO, "test_rule", "ag2"),
            title="AR2: test",
        ),
    ]
    info_signals = [
        Signal(
            ticker=f"I{i}", score=float(s), severity="INFORMATIONAL", agent="ag3",
            timestamp=_TS,
            alert_id=compute_alert_id(f"I{i}", DATE_ISO, "test_rule", "ag3"),
            title=f"I{i}: test",
        )
        for i, s in enumerate([95, 88, 70, 60, 50])
    ]
    results = route_signals(ar_signals + info_signals)
    assert len(results) == 7

    delivered = [(s, rs, dmf) for s, rs, dmf in results if rs == "DELIVERED"]
    suppressed = [(s, rs, dmf) for s, rs, dmf in results if rs == "SUPPRESSED"]

    assert len(delivered) == 4  # 1 AR + 3 INFO
    assert len(suppressed) == 3

    ar_delivered = [s for s, rs, _ in delivered if s.severity == "ACTION_REQUIRED"]
    info_delivered = [s for s, rs, _ in delivered if s.severity == "INFORMATIONAL"]
    assert len(ar_delivered) == 1
    assert ar_delivered[0].score == 85.0
    assert len(info_delivered) == 3
    assert sorted(s.score for s in info_delivered) == [70.0, 88.0, 95.0]

    # Suppressed INFO had available slots but lost to higher-scored peers (not budget_cap_info)
    suppressed_info = [(s, rs, dmf) for s, rs, dmf in suppressed if s.severity == "INFORMATIONAL"]
    assert all(dmf == "outscored" for _, _, dmf in suppressed_info)


# ---------------------------------------------------------------------------
# T-AR-03: cross-run — DB already has 1 AR DELIVERED → new AR → budget_cap_ar
# ---------------------------------------------------------------------------

def test_cross_run_ar_full(db, monkeypatch):
    """No eviction: if DB has 1 AR DELIVERED today, new AR gets budget_cap_ar."""
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York"))
    prior_sig = Signal(
        ticker="PRIOR", score=90.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=today_et,
        alert_id=compute_alert_id("PRIOR", today_et.date().isoformat(), "r", "ag"),
        title="prior",
    )
    repository.insert_signal(prior_sig, routing_status="DELIVERED")

    new_sig = Signal(
        ticker="NEW", score=95.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=today_et,
        alert_id=compute_alert_id("NEW", today_et.date().isoformat(), "r2", "ag"),
        title="new",
    )
    results = route_signals([new_sig])
    assert len(results) == 1
    _, rs, dmf = results[0]
    assert rs == "SUPPRESSED"
    assert dmf == "budget_cap_ar"


# ---------------------------------------------------------------------------
# T-AR-05: cross-run — INFO budget full → budget_cap_info
# ---------------------------------------------------------------------------

def test_cross_run_info_full(monkeypatch):
    """If DB has 3 INFO DELIVERED, new INFO gets budget_cap_info."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {"INFORMATIONAL": 3})
    sig = _sig(ticker="LATE", score=99.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf = results[0]
    assert rs == "SUPPRESSED"
    assert dmf == "budget_cap_info"


# ---------------------------------------------------------------------------
# T-AR-09: ET midnight reset — yesterday's DELIVERED signals don't count today
# ---------------------------------------------------------------------------

def test_et_midnight_reset(db):
    """Signals from yesterday's ET date don't count against today's budget (ROUT-04)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    yesterday_et = datetime.now(et) - timedelta(days=1)

    yesterday_sig = Signal(
        ticker="YEST", score=90.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=yesterday_et,
        alert_id=compute_alert_id("YEST", yesterday_et.date().isoformat(), "r", "ag"),
        title="yesterday",
    )
    repository.insert_signal(yesterday_sig, routing_status="DELIVERED")

    # count_delivered_today() uses today's ET date — yesterday's signal should NOT count
    today_counts = repository.count_delivered_today()
    assert today_counts.get("ACTION_REQUIRED", 0) == 0, (
        f"Yesterday's signal leaked into today's budget: {today_counts}"
    )

    # Therefore a new AR signal today should be DELIVERED
    today_sig = Signal(
        ticker="TODAY", score=85.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=datetime.now(et),
        alert_id=compute_alert_id("TODAY", datetime.now(et).date().isoformat(), "r2", "ag"),
        title="today",
    )
    results = route_signals([today_sig])
    assert len(results) == 1
    _, rs, dmf = results[0]
    assert rs == "DELIVERED", f"Expected DELIVERED but got {rs} (dmf={dmf})"
    assert dmf is None
