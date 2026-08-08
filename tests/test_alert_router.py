"""Tests for the Alert Router — route_signals() covering ROUT-01..ROUT-05.

Tests T-AR-01 through T-AR-10 per D-19 test scenarios and purity guard.
"""
from datetime import datetime, timedelta, timezone

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.router import route_signals
from signal_system.router.alert_router import BUDGET_AR, BUDGET_INFO
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


def test_budget_constants_are_public():
    """BUDGET_AR/BUDGET_INFO are public so jobs/common.py can render cap fractions."""
    assert BUDGET_AR == 1
    assert BUDGET_INFO == 3


def test_competition_score_scales_news_confidence_to_hundreds():
    """News confidences are 0-1; competition scale converts to 0-100 for cross-agent ties."""
    from signal_system.models import competition_score

    news = _sig(ticker="AAPL", score=0.92, severity="ACTION_REQUIRED", agent="news_classifier")
    discovery = _sig(ticker="MSFT", score=88.0, severity="ACTION_REQUIRED", agent="discovery_agent")

    assert competition_score(news) == pytest.approx(92.0)
    assert competition_score(discovery) == pytest.approx(88.0)
    assert competition_score(news) > competition_score(discovery)


def test_cross_agent_ar_competition_uses_competition_scale(monkeypatch):
    """news 0.92 (→92) beats discovery 88 on the unified scale for the single AR slot."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    news = _sig(ticker="AAPL", score=0.92, severity="ACTION_REQUIRED", agent="news_classifier")
    discovery = _sig(ticker="MSFT", score=88.0, severity="ACTION_REQUIRED", agent="discovery_agent")

    results = route_signals([discovery, news])
    delivered = [t for t in results if t[1] == "DELIVERED"]
    assert delivered[0][0].ticker == "AAPL"  # news wins AR
    assert delivered[0][0].severity == "ACTION_REQUIRED"
    assert delivered[0][2] is None
    # discovery demoted to INFO, delivered with demoted_from
    assert delivered[1][0].ticker == "MSFT"
    assert delivered[1][0].severity == "INFORMATIONAL"
    assert delivered[1][2] == "ACTION_REQUIRED"


def test_cross_agent_ar_tie_breaks_by_ticker(monkeypatch):
    """Equal competition scores (news 0.80 vs discovery 80) tiebreak alphabetically."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    news = _sig(ticker="ZZZ", score=0.80, severity="ACTION_REQUIRED", agent="news_classifier")
    discovery = _sig(ticker="AAA", score=80.0, severity="ACTION_REQUIRED", agent="discovery_agent")

    results = route_signals([news, discovery])
    delivered = [t for t in results if t[1] == "DELIVERED"]
    assert delivered[0][0].ticker == "AAA"  # alphabetically first wins AR


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
# T-AR-08: DELIVERED signals always have demoted_from=None and suppression_reason=None
# ---------------------------------------------------------------------------

def test_delivered_demoted_from_and_suppression_reason_are_none(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    sig = _sig(ticker="AAPL", score=80.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "DELIVERED"
    assert dmf is None
    assert reason is None


# ---------------------------------------------------------------------------
# T-AR-10: route_signals is pure and does not write to the DB
# ---------------------------------------------------------------------------

def test_route_signals_is_pure_no_db_writes(monkeypatch):
    """route_signals() must not call insert_signal or perform any DB writes."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})

    def fail_insert(*args, **kwargs):
        raise AssertionError("route_signals() must not call insert_signal()")

    monkeypatch.setattr(repository, "insert_signal", fail_insert)

    sig = _sig(ticker="AAPL", score=80.0, severity="INFORMATIONAL")
    results = route_signals([sig])

    assert results == [(sig, "DELIVERED", None, None)]


# ---------------------------------------------------------------------------
# T-AR-01: 5 AR signals → 1 DELIVERED (highest score), 4 SUPPRESSED (outscored)
# ---------------------------------------------------------------------------

def test_ar_budget_one_winner(monkeypatch):
    """AR cap=1: highest score wins AR, next 3 demoted to INFO (compete for INFO slots), lowest suppressed."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    signals = [
        _sig(ticker=f"T{i}", score=float(score), severity="ACTION_REQUIRED")
        for i, score in enumerate([50, 60, 70, 80, 90])
    ]
    results = route_signals(signals)
    assert len(results) == 5

    delivered = [t for t in results if t[1] == "DELIVERED"]
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert len(delivered) == 4  # 1 AR + 3 demoted-to-INFO
    assert delivered[0][0].score == 90.0  # AR winner
    assert delivered[0][0].severity == "ACTION_REQUIRED"
    assert delivered[0][2] is None  # demoted_from
    assert delivered[0][3] is None  # suppression_reason
    # Top 3 demoted AR signals (80, 70, 60) win INFO slots
    info_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "INFORMATIONAL"]
    assert len(info_delivered) == 3
    assert sorted(s.score for s in info_delivered) == [60.0, 70.0, 80.0]
    assert all(
        dmf == "ACTION_REQUIRED" and reason is None
        for _, _, dmf, reason in delivered
        if dmf is not None
    )
    assert len(suppressed) == 1
    assert suppressed[0][0].score == 50.0
    # demoted_from survives suppression: this signal genuinely was demoted from
    # ACTION_REQUIRED before losing the INFO competition, and the digest must
    # surface that rather than render it as a plain INFORMATIONAL miss.
    assert suppressed[0][2] == "ACTION_REQUIRED"  # demoted_from
    # All 4 non-winning AR signals (80/70/60/50) get demoted into the INFO pool
    # (ar_remaining=1 > 0, so the cap_exhausted branch never fires here). The
    # lowest of those 4 then loses the 3-slot INFO competition to the other 3 —
    # a genuine head-to-head loss against real, contested INFO slots, so it's
    # "outscored", not "cap_exhausted" (same principle as test_mixed_batch_allocation).
    assert suppressed[0][3] == "outscored"


# ---------------------------------------------------------------------------
# T-AR-04: equal scores → alphabetical tiebreak is deterministic
# ---------------------------------------------------------------------------

def test_tiebreak_alphabetical(monkeypatch):
    """Equal-score AR signals: alphabetically first (AAPL) wins AR, MSFT demoted to INFO."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    msft = _sig(ticker="MSFT", score=75.0, severity="ACTION_REQUIRED", agent="a1")
    aapl = Signal(
        ticker="AAPL", score=75.0, severity="ACTION_REQUIRED", agent="a2",
        timestamp=_TS,
        alert_id=compute_alert_id("AAPL", DATE_ISO, "test_rule", "a2"),
        title="AAPL: test signal",
    )
    results = route_signals([msft, aapl])
    delivered = [t for t in results if t[1] == "DELIVERED"]

    assert len(delivered) == 2  # AAPL AR + MSFT demoted to INFO
    # AAPL wins AR (alphabetical)
    assert delivered[0][0].ticker == "AAPL"
    assert delivered[0][0].severity == "ACTION_REQUIRED"
    assert delivered[0][2] is None
    # MSFT demoted to INFO
    assert delivered[1][0].ticker == "MSFT"
    assert delivered[1][0].severity == "INFORMATIONAL"
    assert delivered[1][2] == "ACTION_REQUIRED"
    assert delivered[1][3] is None


# ---------------------------------------------------------------------------
# T-AR-02: mixed batch — 2 AR + 5 INFO → 1 AR + 3 INFO DELIVERED, 3 SUPPRESSED
# ---------------------------------------------------------------------------

def test_mixed_batch_allocation(monkeypatch):
    """Mixed severity batch: 1 AR + 3 INFO slots; demoted AR enters INFO pool."""
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

    delivered = [t for t in results if t[1] == "DELIVERED"]
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert len(delivered) == 4  # 1 AR (AR1) + 3 INFO (I0:95, I1:88, AR2:75)
    assert len(suppressed) == 3  # I2:70, I3:60, I4:50

    ar_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "ACTION_REQUIRED"]
    info_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "INFORMATIONAL"]
    assert len(ar_delivered) == 1
    assert ar_delivered[0].score == 85.0
    assert len(info_delivered) == 3
    assert sorted(s.score for s in info_delivered) == [75.0, 88.0, 95.0]

    # AR2 got demoted to INFO and delivered with demoted_from, no suppression_reason
    demoted_delivered = [t for t in delivered if t[2] == "ACTION_REQUIRED"]
    assert len(demoted_delivered) == 1
    assert demoted_delivered[0][0].ticker == "AR2"
    assert demoted_delivered[0][0].score == 75.0
    assert demoted_delivered[0][3] is None

    # Suppressed INFO signals lost the head-to-head competition for the 3 INFO
    # slots that DID exist this batch — that's "outscored", not "cap_exhausted".
    suppressed_info = [t for t in suppressed if t[0].severity == "INFORMATIONAL"]
    assert all(dmf is None for _, _, dmf, _reason in suppressed_info)
    assert all(reason == "outscored" for _, _, _dmf, reason in suppressed_info)
    assert sorted(s.score for s, _, _, _ in suppressed_info) == [50.0, 60.0, 70.0]


# ---------------------------------------------------------------------------
# T-AR-03: cross-run — DB already has 1 AR DELIVERED → new AR → demote-then-compete
# ---------------------------------------------------------------------------

def test_cross_run_ar_full(db, monkeypatch):
    """AR budget already consumed today: new AR is demoted to INFO, not suppressed outright.

    Demote-before-suppress (suppressed-signals-spec): an AR that can't get an AR
    slot is still worth delivering as INFO if INFO has room. With INFO room free,
    the demoted AR is DELIVERED as INFORMATIONAL carrying demoted_from.
    """
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
    _, rs, dmf, reason = results[0]
    assert rs == "DELIVERED"
    assert dmf == "ACTION_REQUIRED"
    assert reason is None
    assert results[0][0].severity == "INFORMATIONAL"


def test_cross_run_ar_full_and_info_full_demoted_then_suppressed(db, monkeypatch):
    """Both budgets exhausted today: demoted AR competes for INFO, loses, carries both fields."""
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York"))
    new_sig = Signal(
        ticker="NEW", score=95.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=today_et,
        alert_id=compute_alert_id("NEW", today_et.date().isoformat(), "r2", "ag"),
        title="new",
    )
    # Both budgets already fully consumed today via count_delivered_today
    monkeypatch.setattr(
        repository, "count_delivered_today",
        lambda: {"ACTION_REQUIRED": 1, "INFORMATIONAL": BUDGET_INFO},
    )
    results = route_signals([new_sig])
    assert len(results) == 1
    sig, rs, dmf, reason = results[0]
    assert rs == "SUPPRESSED"
    assert dmf == "ACTION_REQUIRED"
    assert reason == "cap_exhausted"
    assert sig.severity == "INFORMATIONAL"


# ---------------------------------------------------------------------------
# T-AR-05: cross-run — INFO budget full → cap_exhausted
# ---------------------------------------------------------------------------

def test_cross_run_info_full(monkeypatch):
    """If DB has 3 INFO DELIVERED, new INFO gets cap_exhausted."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {"INFORMATIONAL": 3})
    sig = _sig(ticker="LATE", score=99.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "SUPPRESSED"
    assert dmf is None
    assert reason == "cap_exhausted"


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
    _, rs, dmf, reason = results[0]
    assert rs == "DELIVERED", f"Expected DELIVERED but got {rs} (dmf={dmf}, reason={reason})"
    assert dmf is None
    assert reason is None


# ---------------------------------------------------------------------------
# demoted_from and suppression_reason are NOT mutually exclusive: an AR signal
# that loses the AR slot and then also loses the INFO competition must retain
# the record of having been ACTION_REQUIRED.
# ---------------------------------------------------------------------------

def test_demoted_then_suppressed_carries_both_fields(monkeypatch):
    """AR loser demoted into a cap-exhausted INFO pool keeps demoted_from."""
    # INFO budget already fully consumed earlier today; one AR slot free.
    monkeypatch.setattr(
        repository, "count_delivered_today", lambda: {"INFORMATIONAL": BUDGET_INFO}
    )
    winner = _sig(ticker="SCHD", score=90.0, severity="ACTION_REQUIRED")
    loser = _sig(ticker="AAPL", score=81.0, severity="ACTION_REQUIRED")

    results = route_signals([winner, loser])

    delivered = [t for t in results if t[1] == "DELIVERED"]
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert [t[0].ticker for t in delivered] == ["SCHD"]
    assert len(suppressed) == 1
    sig, _rs, dmf, reason = suppressed[0]
    assert sig.ticker == "AAPL"
    assert sig.severity == "INFORMATIONAL"       # demoted out of AR
    assert dmf == "ACTION_REQUIRED"              # ...and that fact survives
    assert reason == "cap_exhausted"             # ...alongside why it was cut


def test_demoted_then_outscored_carries_both_fields(monkeypatch):
    """Same guarantee when the INFO loss is a head-to-head 'outscored'."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    signals = [
        _sig(ticker=f"T{i}", score=float(score), severity="ACTION_REQUIRED")
        for i, score in enumerate([55, 65, 75, 85, 95])
    ]

    results = route_signals(signals)
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert len(suppressed) == 1
    sig, _rs, dmf, reason = suppressed[0]
    assert sig.score == 55.0
    assert dmf == "ACTION_REQUIRED"
    assert reason == "outscored"
