"""Plan 06 Option D — immediate AR, deferred cross-agent INFO dispatch.

Covers stage_signals() (agent-job intake), dispatch_candidates() (pooled
routing), the CANDIDATE repository round-trip, the dispatch job end to end
against a real temp DB, digest staging lines/validation, and the watchdog's
stale-candidate check.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.router import (
    ROUTING_REGIME,
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


def _now() -> datetime:
    return datetime.now(ET)


def _sig(
    ticker: str,
    *,
    agent: str = "discovery_agent",
    severity: str = "INFORMATIONAL",
    score: float = 80.0,
    ts: datetime | None = None,
) -> Signal:
    ts = ts or _now()
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), f"r-{severity}", agent),
        title=f"{ticker}: {agent} {severity}",
        body=f"{ticker} body",
    )


# --- stage_signals --------------------------------------------------------


def test_stage_delivers_top_ar_and_stages_everything_else(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    ar_hi = _sig("NVDA", agent="news_classifier", severity="ACTION_REQUIRED", score=0.95)
    ar_lo = _sig("AVGO", agent="news_classifier", severity="ACTION_REQUIRED", score=0.88)
    info = _sig("MSFT", score=70.0)

    out = {s.ticker: (s, rs, dmf, r) for s, rs, dmf, r in stage_signals([info, ar_lo, ar_hi])}

    assert out["NVDA"][1:] == ("DELIVERED", None, None)
    demoted, status, dmf, reason = out["AVGO"]
    assert (status, dmf, reason) == ("CANDIDATE", "ACTION_REQUIRED", None)
    assert demoted.severity == "INFORMATIONAL"
    assert out["MSFT"][1:] == ("CANDIDATE", None, None)


def test_stage_demotes_every_ar_when_ar_budget_spent(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {"ACTION_REQUIRED": 1})
    ar = _sig("CRWV", agent="exit_sentinel", severity="ACTION_REQUIRED", score=100.0)

    [(sig, status, dmf, _r)] = stage_signals([ar])

    assert (sig.severity, status, dmf) == ("INFORMATIONAL", "CANDIDATE", "ACTION_REQUIRED")


def test_stage_never_routes_info_even_with_budget_free(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    staged = stage_signals([_sig(t) for t in ("A", "B", "C", "D")])
    assert {rs for _s, rs, _d, _r in staged} == {"CANDIDATE"}


def test_stage_rejects_monitoring():
    with pytest.raises(ValueError):
        stage_signals([_sig("X", severity="MONITORING")])


# --- dispatch_candidates --------------------------------------------------


def test_dispatch_ranks_by_agent_priority_then_score(monkeypatch):
    """News > sentinel > discovery; score only orders within an agent."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    disc_hi = _sig("RTX", agent="discovery_agent", score=99.6)
    news_lo = _sig("NVDA", agent="news_classifier", score=0.61)
    news_hi = _sig("AVGO", agent="news_classifier", score=0.87)
    sent = _sig("CAN", agent="exit_sentinel", score=100.0)
    disc_lo = _sig("D1", agent="discovery_agent", score=61.0)

    routed = dispatch_candidates([(s, None) for s in [disc_hi, news_lo, sent, disc_lo, news_hi]])
    delivered = [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"]
    suppressed = {s.ticker: r for s, rs, _d, r in routed if rs == "SUPPRESSED"}

    assert delivered == ["AVGO", "NVDA", "CAN"]
    assert suppressed == {"RTX": "outscored", "D1": "outscored"}


def test_dispatch_discovery_fills_leftover_slots(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    news = _sig("NVDA", agent="news_classifier", score=0.7)
    discs = [_sig(f"D{i}", score=70.0 + i) for i in range(4)]
    routed = dispatch_candidates([(s, None) for s in [*discs, news]])
    assert [s.ticker for s, rs, _d, _r in routed if rs == "DELIVERED"] == ["NVDA", "D3", "D2"]


def test_dispatch_cap_exhausted_when_budget_spent_before_dispatch(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {"INFORMATIONAL": 3})
    [(_, status, _d, reason)] = dispatch_candidates([(_sig("A"), None)])
    assert (status, reason) == ("SUPPRESSED", "cap_exhausted")


def test_dispatch_preserves_stored_demotion(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    s = _sig("CRWV", agent="exit_sentinel", score=100.0)
    [(_, status, dmf, _r)] = dispatch_candidates([(s, "ACTION_REQUIRED")])
    assert (status, dmf) == ("DELIVERED", "ACTION_REQUIRED")


def test_dispatch_rejects_non_info_candidate(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda agent=None: {})
    with pytest.raises(ValueError):
        dispatch_candidates([(_sig("X", severity="ACTION_REQUIRED"), None)])


# --- repository round-trip ------------------------------------------------


def test_candidate_roundtrip_and_regime_stamp(db):
    from signal_system.jobs.common import persist_routed_signals

    s = _sig("MU", agent="news_classifier", score=0.7)
    summary = persist_routed_signals([(s, "CANDIDATE", "ACTION_REQUIRED", None)])
    assert summary.status_counts["CANDIDATE"] == 1
    assert summary.delivered_signals == []

    [(back, dmf)] = repository.list_candidates_for_date(s.timestamp.date().isoformat())
    assert back.alert_id == s.alert_id and back.score == 0.7 and dmf == "ACTION_REQUIRED"

    conn = repository._connect()
    try:
        regime = conn.execute(
            "SELECT routing_regime FROM signals WHERE alert_id = ?", (s.alert_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert regime == ROUTING_REGIME


def test_mark_candidates_routed_only_touches_candidates(db):
    s = _sig("MU")
    repository.insert_signal(s, routing_status="CANDIDATE")
    assert repository.mark_candidates_routed([(s.alert_id, "PENDING", None, None)]) == 1
    # Second attempt finds no CANDIDATE row and rolls back.
    assert repository.mark_candidates_routed([(s.alert_id, "SUPPRESSED", None, "x")]) == 0
    assert repository.count_routing_status_for_alert_ids([s.alert_id]) == {"DELIVERED": 1}


# --- digest staging line + validation -------------------------------------


def test_job_digest_lists_staged_and_validates_count():
    from signal_system.jobs.common import render_digest, validate_digest_payload

    staged = _sig("PANW", score=88.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=5,
        routed_signals=[(staged, "CANDIDATE", None, None)],
        held_tickers={"PANW"},
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0, "CANDIDATE": 1},
    )
    assert "Staged for evening dispatch: 1" in payload.body
    assert "PANW — 88 — INFORMATIONAL — HELD" in payload.body
    assert "Scanned 5 tickers, 0 alerts" in payload.body

    validate_digest_payload(
        payload, scanned_tickers=5, db_counts={"CANDIDATE": 1}, delivered_signals=[]
    )
    with pytest.raises(RuntimeError, match="staged count"):
        validate_digest_payload(
            payload, scanned_tickers=5, db_counts={}, delivered_signals=[]
        )


# --- dispatch job end to end ----------------------------------------------


@contextmanager
def _noop_heartbeat(*_a, **_k):
    yield


@pytest.fixture
def dispatch_env(db, monkeypatch):
    from signal_system.jobs import dispatch as dispatch_job

    sent: list[str] = []
    monkeypatch.setattr(dispatch_job.heartbeat, "heartbeat", _noop_heartbeat)
    monkeypatch.setattr(dispatch_job.telegram_sender, "send_message", sent.append)
    monkeypatch.setattr(dispatch_job, "held_ticker_set", lambda **kwargs: {"NVDA"})
    return dispatch_job, sent


def _stage(signals: list[Signal]) -> None:
    from signal_system.jobs.common import persist_routed_signals

    persist_routed_signals(stage_signals(signals))


def test_dispatch_job_pools_agents_and_delivers_by_score(dispatch_env):
    dispatch_job, sent = dispatch_env
    _stage([_sig("NVDA", agent="news_classifier", score=0.87)])
    _stage([_sig(f"D{i}", score=60.0 + i) for i in range(5)] + [_sig("RTX", score=99.6)])
    _stage([_sig("CAN", agent="exit_sentinel", score=100.0)])

    dispatch_job.run()

    assert len(sent) == 1
    msg = sent[0]
    assert msg.startswith("Dispatch Digest — 3 alerts")
    assert "Candidates pooled: 8 (news_classifier 1, discovery_agent 6, exit_sentinel 1)" in msg
    assert "Delivered: 3 (news_classifier 1, discovery_agent 1, exit_sentinel 1)" in msg
    assert "RTX — 100 [100] — discovery_agent — INFORMATIONAL" in msg
    assert "NVDA — 87% [87] — news_classifier — INFORMATIONAL — HELD" in msg

    today = _now().date().isoformat()
    assert repository.list_candidates_for_date(today) == []
    assert repository.count_delivered_today().get("INFORMATIONAL") == 3
    latest = repository.get_latest_run("dispatch")
    assert latest["status"] == "success"


def test_dispatch_job_zero_candidates_still_sends_confirmation(dispatch_env):
    dispatch_job, sent = dispatch_env
    dispatch_job.run()
    assert "Pooled 0 candidates, 0 alerts" in sent[0]
    assert "Candidates pooled: 0 (news_classifier 0, discovery_agent 0, exit_sentinel 0)" in sent[0]


def test_dispatch_job_rerun_is_safe(dispatch_env):
    dispatch_job, sent = dispatch_env
    _stage([_sig("A", score=90.0)])
    dispatch_job.run()
    dispatch_job.run()
    assert "Dispatch Digest — 1 alert" in sent[0]
    assert "Pooled 0 candidates, 0 alerts" in sent[1]


def test_dispatch_job_expires_prior_day_candidates(dispatch_env):
    dispatch_job, sent = dispatch_env
    yesterday = _now() - timedelta(days=1)
    old = _sig("OLD", score=95.0, ts=yesterday)
    repository.insert_signal(old, routing_status="CANDIDATE")

    dispatch_job.run()

    assert "Expired undispatched candidates from earlier days: 1" in sent[0]
    assert repository.count_routing_status_for_alert_ids([old.alert_id]) == {"SUPPRESSED": 1}


def test_dispatch_rerun_after_failed_send_still_validates(dispatch_env, monkeypatch):
    """A failed digest leaves suppressed sentinel rows; the rerun must not fail."""
    dispatch_job, sent = dispatch_env
    for index in range(4):
        sentinel = replace(
            _sig(f"S{index}", agent="exit_sentinel", score=100.0),
            trigger_json='{"drawdown_20d": 0.2}',
        )
        _stage([sentinel])

    def _telegram_down(_message):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(dispatch_job.telegram_sender, "send_message", _telegram_down)
    with pytest.raises(RuntimeError, match="telegram down"):
        dispatch_job.run()

    # One sentinel was suppressed by the first run; it remains SUPPRESSED.
    today = _now().date().isoformat()
    assert repository.count_suppressed_for_date(today, "exit_sentinel") == 1

    monkeypatch.setattr(dispatch_job.telegram_sender, "send_message", sent.append)
    dispatch_job.run()

    assert "Held-position exits not delivered (1)" in sent[0]
    assert repository.get_latest_run("dispatch")["status"] == "success"


def test_dispatch_rerun_leaves_stuck_pending_for_the_sweep(dispatch_env, monkeypatch):
    """Dispatch never re-sends: telegram-poll is the single owner of the sweep.

    Two re-senders could race the same PENDING row at 17:40 and double-send.
    """
    dispatch_job, sent = dispatch_env
    sentinel = replace(
        _sig("MU", agent="exit_sentinel", score=100.0),
        trigger_json='{"drawdown_20d": 0.208}',
    )
    _stage([sentinel])

    def _telegram_down(_message):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(dispatch_job.telegram_sender, "send_message", _telegram_down)
    with pytest.raises(RuntimeError, match="telegram down"):
        dispatch_job.run()

    monkeypatch.setattr(dispatch_job.telegram_sender, "send_message", sent.append)
    dispatch_job.run()

    assert sentinel.title not in sent[0]
    cutoff = _now() - timedelta(days=1)
    assert [s.alert_id for s, _ in repository.list_unconfirmed_since(cutoff)] == [
        sentinel.alert_id
    ]


def test_friday_reprompt_excludes_alerts_delivered_in_same_run(dispatch_env, monkeypatch):
    dispatch_job, sent = dispatch_env
    friday = datetime(2026, 10, 23, 17, 40, tzinfo=ET)
    current = _sig(
        "NVDA",
        agent="news_classifier",
        score=0.90,
        ts=friday.replace(hour=9, minute=0),
    )
    older = _sig(
        "MSFT",
        agent="news_classifier",
        score=0.80,
        ts=friday - timedelta(days=2),
    )
    _stage([current])
    repository.insert_signal(older, routing_status="DELIVERED")

    button_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatch_job.telegram_sender,
        "send_alert_with_buttons",
        lambda signal, **kwargs: button_calls.append(
            (signal.alert_id, kwargs.get("prefix", ""))
        )
        or 1,
    )
    monkeypatch.setattr(dispatch_job, "_now_et", lambda: friday)

    dispatch_job.run()

    assert "Unreviewed this week: 1" in sent[0]
    review_ids = {alert_id for alert_id, prefix in button_calls if prefix == "Review: "}
    assert older.alert_id in review_ids
    assert current.alert_id not in review_ids


def test_dispatch_job_respects_info_already_delivered_today(dispatch_env):
    """Transition day: INFO delivered under the legacy path still consumes budget."""
    dispatch_job, sent = dispatch_env
    for t in ("L1", "L2"):
        repository.insert_signal(_sig(t, score=70.0), routing_status="DELIVERED")
    _stage([_sig("N1", score=90.0), _sig("N2", score=80.0)])

    dispatch_job.run()

    assert sent[0].startswith("Dispatch Digest — 1 alert")
    assert "N2 — 80 [80] — discovery_agent — INFORMATIONAL — outscored" in sent[0]


def test_dispatch_reserved_label_reports_budget_exhausted(dispatch_env):
    """A fresh sentinel with a spent INFO budget must not claim 'no fresh trigger'."""
    dispatch_job, sent = dispatch_env
    for t in ("L1", "L2", "L3"):
        repository.insert_signal(_sig(t, score=70.0), routing_status="DELIVERED")
    fresh = replace(
        _sig("MU", agent="exit_sentinel", score=100.0),
        trigger_json='{"drawdown_20d": 0.2}',
    )
    _stage([fresh])

    dispatch_job.run()

    assert "Sentinel reserved slot: unused (INFO budget exhausted)" in sent[0]


def test_dispatch_reserved_label_reports_no_fresh_trigger(dispatch_env):
    """No fresh sentinel candidate -> the reserved slot is genuinely unused."""
    dispatch_job, sent = dispatch_env
    _stage([_sig("N1", score=90.0)])

    dispatch_job.run()

    assert "Sentinel reserved slot: unused (no fresh trigger)" in sent[0]


# --- watchdog -------------------------------------------------------------


def test_watchdog_flags_stale_candidates(db):
    from signal_system.jobs.watchdog import check_jobs

    old = _sig("STALE", ts=_now() - timedelta(hours=40))
    repository.insert_signal(old, routing_status="CANDIDATE")

    failing, _stale = check_jobs(now=_now())

    assert any(job == "dispatch" and "never dispatched" in d for job, d in failing)


def test_watchdog_ignores_fresh_candidates(db):
    from signal_system.jobs.watchdog import check_jobs

    repository.insert_signal(_sig("FRESH"), routing_status="CANDIDATE")
    failing, _stale = check_jobs(now=_now())
    assert not any("never dispatched" in d for _job, d in failing)



def test_validate_dispatch_fails_when_reserved_slot_not_delivered():
    from signal_system.jobs.dispatch import validate_dispatch

    reserved = replace(
        _sig("MU", agent="exit_sentinel", score=100.0),
        trigger_json='{"drawdown_20d": 0.208}',
    )
    news = _sig("NVDA", agent="news_classifier", score=0.9)
    routed = [
        (news, "DELIVERED", None, None),
        (reserved, "SUPPRESSED", None, "outscored"),
    ]
    body = (
        f"{news.title}\nHeld-position exits not delivered (1)\n"
        "Sentinel reserved slot: MU"
    )
    with pytest.raises(RuntimeError, match="Reserved sentinel slot delivered 0"):
        validate_dispatch(
            routed=routed,
            db_counts={"DELIVERED": 1, "SUPPRESSED": 1},
            body=body,
            sentinel_suppressed_db=1,
            reserved=reserved,
        )
