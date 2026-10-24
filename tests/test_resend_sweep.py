"""Unconfirmed-send sweep (telegram-poll) and the sender's transient retry.

A PENDING row is a delivered alert whose Telegram send was never confirmed.
telegram-poll is the single owner of re-sending them: inside RESEND_WINDOW
they are re-sent and confirmed, unless the producing job is still running;
past it they are announced and closed out as SUPPRESSED/'send_failed'.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from signal_system.jobs import common
from signal_system.jobs.common import (
    IN_FLIGHT_MAX_AGE,
    RESEND_WINDOW,
    persist_routed_signals,
    resend_unconfirmed,
)
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

ET = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


@pytest.fixture
def telegram(monkeypatch):
    """Capture button re-sends and plain messages instead of calling Telegram."""
    calls: dict[str, list] = {"buttons": [], "messages": []}

    def _buttons(signal, **kwargs):
        calls["buttons"].append((signal, kwargs))
        return 100 + len(calls["buttons"])

    monkeypatch.setattr(common.telegram_sender, "send_alert_with_buttons", _buttons)
    monkeypatch.setattr(common.telegram_sender, "send_message", calls["messages"].append)
    return calls


def _now() -> datetime:
    return datetime.now(ET)


def _stuck(
    ticker: str,
    *,
    agent: str = "news_classifier",
    severity: str = "ACTION_REQUIRED",
    score: float = 0.95,
    age: timedelta = timedelta(hours=1),
) -> Signal:
    """Persist a delivered signal as PENDING, as a job does before its send fails."""
    ts = _now() - age
    signal = Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.isoformat(), f"r-{severity}", agent),
        title=f"{ticker}: {agent} {severity}",
        body=f"{ticker} body",
    )
    persist_routed_signals([(signal, "DELIVERED", None, None)])
    return signal


def _seed_run(job: str, status: str, *, age: timedelta = timedelta(0)) -> None:
    run_id = repository.insert_run(job)
    conn = sqlite3.connect(str(repository.DB_PATH))
    conn.execute(
        "UPDATE runs SET status = ?, started_at = ? WHERE run_id = ?",
        (status, (_now() - age).isoformat(), run_id),
    )
    conn.commit()
    conn.close()


def _status(alert_id: str) -> tuple[str, str | None]:
    conn = sqlite3.connect(str(repository.DB_PATH))
    row = conn.execute(
        "SELECT routing_status, suppression_reason FROM signals WHERE alert_id = ?",
        (alert_id,),
    ).fetchone()
    conn.close()
    return row


# --- re-send inside the window ----------------------------------------------


def test_stuck_ar_from_failed_job_is_resent_and_confirmed(db, telegram):
    ar = _stuck("NVDA")
    _seed_run("news-morning", "failed", age=timedelta(hours=1))

    result = resend_unconfirmed(_now(), held_tickers={"NVDA"})

    assert [s.alert_id for s in result.resent] == [ar.alert_id]
    signal, kwargs = telegram["buttons"][0]
    assert signal.alert_id == ar.alert_id
    assert kwargs["held"] is True
    assert kwargs["prefix"].startswith("Re-sent (raised ")
    assert _status(ar.alert_id) == ("DELIVERED", None)
    # The AR held its lane the whole time; re-sending neither frees nor spends it.
    assert repository.count_delivered_today(agent="news_classifier") == {"ACTION_REQUIRED": 1}
    # The re-sent message is recorded so a reply-to-note resolves to the alert.
    row = repository.get_telegram_message_by_id(
        str(common.config.TELEGRAM_CHAT_ID), 101
    )
    assert row["alert_id"] == ar.alert_id


def test_stuck_dispatch_info_is_resent(db, telegram):
    info = _stuck("MU", agent="exit_sentinel", severity="INFORMATIONAL", score=100.0)
    _seed_run("dispatch", "failed")

    result = resend_unconfirmed(_now())

    assert [s.alert_id for s in result.resent] == [info.alert_id]
    assert _status(info.alert_id) == ("DELIVERED", None)


def test_previous_day_row_inside_window_is_resent(db, telegram):
    """A failure late yesterday is not lost just because the date rolled over."""
    ar = _stuck("MU", agent="exit_sentinel", score=100.0, age=timedelta(hours=20))

    result = resend_unconfirmed(_now())

    assert [s.alert_id for s in result.resent] == [ar.alert_id]


def test_ar_skipped_while_producing_job_runs_then_resent_next_sweep(db, telegram):
    """The sentinel-still-running race: skipped now, delivered by the next poll."""
    ar = _stuck("MU", agent="exit_sentinel", score=100.0)
    _seed_run("sentinel", "running", age=timedelta(minutes=10))

    first = resend_unconfirmed(_now())

    assert first.resent == []
    assert [s.alert_id for s in first.skipped_in_flight] == [ar.alert_id]
    assert telegram["buttons"] == []
    assert _status(ar.alert_id) == ("PENDING", None)

    _seed_run("sentinel", "failed")  # the run finished without confirming
    second = resend_unconfirmed(_now())

    assert [s.alert_id for s in second.resent] == [ar.alert_id]
    assert _status(ar.alert_id) == ("DELIVERED", None)


def test_info_skipped_while_dispatch_runs(db, telegram):
    info = _stuck("NVDA", severity="INFORMATIONAL", score=0.9)
    _seed_run("dispatch", "running", age=timedelta(minutes=1))

    result = resend_unconfirmed(_now())

    assert [s.alert_id for s in result.skipped_in_flight] == [info.alert_id]
    assert _status(info.alert_id) == ("PENDING", None)


def test_old_running_row_is_a_crash_and_does_not_block(db, telegram):
    ar = _stuck("MU", agent="exit_sentinel", score=100.0)
    _seed_run("sentinel", "running", age=IN_FLIGHT_MAX_AGE + timedelta(minutes=1))

    result = resend_unconfirmed(_now())

    assert [s.alert_id for s in result.resent] == [ar.alert_id]


def test_send_failure_propagates_and_keeps_earlier_confirmations(db, monkeypatch):
    first = _stuck("AAA", age=timedelta(hours=2))
    second = _stuck("BBB", age=timedelta(hours=1))
    sent: list[str] = []

    def _buttons(signal, **_kwargs):
        if signal.alert_id == second.alert_id:
            raise urllib.error.URLError("telegram down")
        sent.append(signal.alert_id)
        return 7

    monkeypatch.setattr(common.telegram_sender, "send_alert_with_buttons", _buttons)

    with pytest.raises(urllib.error.URLError):
        resend_unconfirmed(_now())

    assert sent == [first.alert_id]
    assert _status(first.alert_id) == ("DELIVERED", None)
    assert _status(second.alert_id) == ("PENDING", None)


def test_nothing_pending_sends_nothing(db, telegram):
    result = resend_unconfirmed(_now())
    assert (result.resent, result.closed_out, telegram["messages"]) == ([], [], [])


# --- close-out past the window ------------------------------------------------


def test_rows_past_window_are_announced_then_closed_out(db, telegram):
    old = _stuck("CAN", agent="exit_sentinel", score=100.0, age=RESEND_WINDOW + timedelta(hours=1))
    fresh = _stuck("NVDA")

    result = resend_unconfirmed(_now())

    assert [s.alert_id for s in result.closed_out] == [old.alert_id]
    assert len(telegram["messages"]) == 1
    assert "closed out as send_failed" in telegram["messages"][0]
    assert old.title in telegram["messages"][0]
    assert _status(old.alert_id) == ("SUPPRESSED", "send_failed")
    # The old row is never re-sent as if it were fresh; the in-window one is.
    assert [s.alert_id for s, _ in telegram["buttons"]] == [fresh.alert_id]


def test_close_out_waits_until_the_notice_is_sent(db, monkeypatch):
    """If Telegram is still down, nothing is closed silently."""
    old = _stuck("CAN", agent="exit_sentinel", score=100.0, age=RESEND_WINDOW + timedelta(hours=1))

    def _down(_text):
        raise urllib.error.URLError("telegram down")

    monkeypatch.setattr(common.telegram_sender, "send_message", _down)

    with pytest.raises(urllib.error.URLError):
        resend_unconfirmed(_now())

    assert _status(old.alert_id) == ("PENDING", None)


def test_close_out_never_overwrites_a_confirmed_row(db):
    ar = _stuck("NVDA")
    repository.update_routing_status(ar.alert_id, "DELIVERED")

    assert repository.mark_unconfirmed_send_failed([ar.alert_id]) == 0
    assert _status(ar.alert_id) == ("DELIVERED", None)


# --- telegram-poll wiring -------------------------------------------------------


@contextmanager
def _noop_heartbeat(*_a, **_k):
    yield


def test_poll_records_taps_before_a_failed_sweep(db, monkeypatch):
    """A Telegram outage during the sweep fails the run but never loses a tap."""
    from signal_system.jobs import telegram_poll

    _stuck("NVDA")
    monkeypatch.setattr(telegram_poll.heartbeat, "heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        telegram_poll.telegram_sender,
        "get_updates",
        lambda offset, **_k: [{"update_id": 41, "message": {"chat": {"id": 1}}}],
    )

    def _down(_signal, **_kwargs):
        raise urllib.error.URLError("telegram down")

    monkeypatch.setattr(common.telegram_sender, "send_alert_with_buttons", _down)

    with pytest.raises(urllib.error.URLError):
        telegram_poll.run()

    assert repository.get_app_state("telegram_update_offset") == "42"
    assert repository.get_latest_run("telegram-poll")["status"] == "failed"


def test_poll_sweep_resends_stuck_alert(db, telegram, monkeypatch):
    from signal_system.jobs import telegram_poll

    ar = _stuck("NVDA")
    monkeypatch.setattr(telegram_poll.heartbeat, "heartbeat", _noop_heartbeat)
    monkeypatch.setattr(telegram_poll.telegram_sender, "get_updates", lambda offset, **_k: [])
    monkeypatch.setattr(telegram_poll, "held_ticker_set", lambda **_k: {"NVDA"})

    telegram_poll.run()

    assert [s.alert_id for s, _ in telegram["buttons"]] == [ar.alert_id]
    assert _status(ar.alert_id) == ("DELIVERED", None)
    assert repository.get_latest_run("telegram-poll")["status"] == "success"


# --- sender transient retry ------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes = b'{"ok": true, "result": {"message_id": 5}}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


@pytest.fixture
def sender(monkeypatch):
    import signal_system.delivery.telegram_sender as ts

    monkeypatch.setattr(ts._send.retry, "sleep", lambda _seconds: None)
    return ts


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None),
        urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None),
        urllib.error.URLError("connection reset"),
        TimeoutError("timed out"),
    ],
)
def test_send_retries_transient_errors(sender, monkeypatch, error):
    attempts: list[int] = []

    def _urlopen(req, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise error
        return _FakeResponse()

    monkeypatch.setattr(sender.urllib.request, "urlopen", _urlopen)

    sender.send_message("hello")

    assert len(attempts) == 2


def test_send_does_not_retry_permanent_errors(sender, monkeypatch):
    attempts: list[int] = []

    def _urlopen(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError("url", 400, "Bad Request", {}, None)

    monkeypatch.setattr(sender.urllib.request, "urlopen", _urlopen)

    with pytest.raises(urllib.error.HTTPError):
        sender.send_message("hello")
    assert len(attempts) == 1


def test_send_gives_up_after_three_attempts(sender, monkeypatch):
    attempts: list[int] = []

    def _urlopen(req, timeout):
        attempts.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(sender.urllib.request, "urlopen", _urlopen)

    with pytest.raises(urllib.error.URLError):
        sender.send_message("hello")
    assert len(attempts) == 3
