"""Plan 12e — Telegram action buttons, polling, notes, and watchdog visibility."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from signal_system import config
from signal_system.delivery import telegram_sender
from signal_system.jobs import common
from signal_system.jobs import telegram_poll
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

ET = ZoneInfo("America/New_York")
REAL_SEND_ALERT_WITH_BUTTONS = telegram_sender.send_alert_with_buttons


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


@contextmanager
def _noop_heartbeat(*_a, **_k):
    yield


def _sig(ticker: str = "NVDA") -> Signal:
    ts = datetime.now(ET)
    return Signal(
        ticker=ticker,
        score=0.9,
        severity="INFORMATIONAL",
        agent="news_classifier",
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), "r", "news_classifier"),
        title="ai_semi: Nvidia lands a big order",
    )


def _insert(sig: Signal, routing_status: str = "DELIVERED") -> None:
    repository.insert_signal(sig, routing_status=routing_status)


def test_send_alert_with_buttons_callback_data_is_short(monkeypatch):
    monkeypatch.setattr(telegram_sender, "send_alert_with_buttons", REAL_SEND_ALERT_WITH_BUTTONS)
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        telegram_sender, "_call", lambda method, payload, timeout=10: captured.append((method, payload)) or {"message_id": 7}
    )
    sig = _sig()
    message_id = telegram_sender.send_alert_with_buttons(sig, held=True)

    assert message_id == 7
    method, payload = captured[0]
    assert method == "sendMessage"
    assert "NVDA · news_classifier · INFORMATIONAL · HELD" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Acted", "⏭ Passed"]
    for button in buttons:
        assert len(button["callback_data"].encode()) <= 64


def test_button_send_failure_does_not_fail_job_and_watchdog_flags(db, monkeypatch):
    sig = _sig()
    _insert(sig)

    def _boom(*_a, **_k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram_sender, "send_alert_with_buttons", _boom)
    common.send_alert_buttons([sig], held_tickers={"NVDA"})

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT kind, status FROM telegram_messages WHERE alert_id = ?", (sig.alert_id,)
    ).fetchone()
    conn.close()
    assert row == ("alert", "send_failed")

    from signal_system.jobs.watchdog import check_jobs

    failing, _stale = check_jobs(now=datetime.now(ET))
    assert any(job == "telegram-poll" and "failed to send" in detail for job, detail in failing)


def test_unauthorized_sender_never_writes(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 999},
            "message": {"message_id": 5, "chat": {"id": -1001234567890}},
            "data": "a:1:deadbeef",
        },
    }
    header = _sig()
    _insert(header)

    telegram_poll._process_update(update)

    conn = sqlite3.connect(db)
    acted = conn.execute("SELECT acted FROM signals WHERE alert_id=?", (header.alert_id,)).fetchone()[0]
    conn.close()
    assert acted is None


def test_callback_for_non_delivered_row_logs_not_found(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    answers: list[str] = []
    monkeypatch.setattr(
        telegram_sender, "answer_callback_query", lambda _id, text: answers.append(text)
    )
    header = _sig()
    _insert(header, routing_status="PENDING")

    telegram_poll._handle_callback(
        {
            "id": "cb1",
            "data": f"a:1:{header.alert_id[:24]}",
            "from": {"id": -1001234567890},
            "message": {"message_id": 1, "chat": {"id": -1001234567890}},
        }
    )

    assert answers == ["Not found"]
    conn = sqlite3.connect(db)
    acted = conn.execute("SELECT acted FROM signals WHERE alert_id=?", (header.alert_id,)).fetchone()[0]
    conn.close()
    assert acted is None


def test_acted_then_passed_overwrites(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    answers: list[str] = []
    monkeypatch.setattr(
        telegram_sender, "answer_callback_query", lambda _id, text: answers.append(text)
    )
    monkeypatch.setattr(
        telegram_sender, "edit_message_reply_markup", lambda *a, **k: None
    )
    header = _sig()
    _insert(header)
    payload = {
        "id": "cb1",
        "from": {"id": -1001234567890},
        "message": {"message_id": 1, "chat": {"id": -1001234567890}},
    }

    telegram_poll._handle_callback(
        {**payload, "data": f"a:1:{header.alert_id[:24]}"}
    )
    first = sqlite3.connect(db).execute(
        "SELECT acted, acted_at FROM signals WHERE alert_id=?", (header.alert_id,)
    ).fetchone()
    telegram_poll._handle_callback(
        {**payload, "data": f"a:0:{header.alert_id[:24]}"}
    )
    second = sqlite3.connect(db).execute(
        "SELECT acted, acted_at FROM signals WHERE alert_id=?", (header.alert_id,)
    ).fetchone()

    assert first[0] == 1
    assert second[0] == 0
    assert second[1] is not None
    assert answers == ["Logged: Acted", "Logged: Passed"]


def test_expired_callback_still_updates_and_edits_keyboard(db, monkeypatch):
    """Telegram rejects late answers; the write and keyboard edit must still run."""
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    header = _sig()
    _insert(header)
    edits: list[tuple] = []

    def _expired(*_a, **_k):
        raise RuntimeError("Query is too old and response timeout expired")

    monkeypatch.setattr(telegram_sender, "answer_callback_query", _expired)
    monkeypatch.setattr(
        telegram_sender,
        "edit_message_reply_markup",
        lambda chat_id, message_id, markup: edits.append((chat_id, message_id, markup)),
    )

    telegram_poll._handle_callback(
        {
            "id": "cb1",
            "data": f"a:1:{header.alert_id[:24]}",
            "message": {"message_id": 9, "chat": {"id": -1001234567890}},
        }
    )

    conn = sqlite3.connect(db)
    acted = conn.execute(
        "SELECT acted FROM signals WHERE alert_id=?", (header.alert_id,)
    ).fetchone()[0]
    conn.close()
    assert acted == 1
    assert len(edits) == 1
    assert edits[0][2]["inline_keyboard"][0][0]["text"] == "↩ Change to Passed"


def test_expired_not_found_callback_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        telegram_sender,
        "answer_callback_query",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("expired")),
    )
    telegram_poll._handle_callback({"id": "cb1", "data": "not-a-callback"})


def test_reply_to_note_sets_prefixed_capped_note(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    header = _sig()
    _insert(header)
    repository.insert_telegram_message(
        chat_id="-1001234567890",
        message_id=42,
        alert_id=header.alert_id,
        kind="alert",
        status="sent",
    )

    telegram_poll._handle_message(
        {
            "message_id": 43,
            "chat": {"id": -1001234567890},
            "text": "bought 10 shares\x07 " + "x" * 600,
            "reply_to_message": {"message_id": 42},
        }
    )

    conn = sqlite3.connect(db)
    note = conn.execute(
        "SELECT user_note FROM signals WHERE alert_id=?", (header.alert_id,)
    ).fetchone()[0]
    conn.close()
    assert note.startswith("tg: bought 10 shares ")
    assert len(note) <= 500
    assert "\x07" not in note


def test_poll_offset_advances_and_replay_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    header = _sig()
    _insert(header)
    monkeypatch.setattr(telegram_poll.heartbeat, "heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        telegram_sender, "answer_callback_query", lambda *a, **k: None
    )
    monkeypatch.setattr(
        telegram_sender, "edit_message_reply_markup", lambda *a, **k: None
    )
    batch = [
        {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "from": {"id": -1001234567890},
                "message": {"message_id": 1, "chat": {"id": -1001234567890}},
                "data": f"a:1:{header.alert_id[:24]}",
            },
        }
    ]
    monkeypatch.setattr(
        telegram_sender, "get_updates", lambda offset, **kwargs: batch
    )

    telegram_poll.run()
    telegram_poll.run()

    assert repository.get_app_state("telegram_update_offset") == "11"
    conn = sqlite3.connect(db)
    acted = conn.execute("SELECT acted FROM signals WHERE alert_id=?", (header.alert_id,)).fetchone()[0]
    conn.close()
    assert acted == 1


def test_poll_db_error_fails_run_and_does_not_advance_offset(db, monkeypatch):
    """A transient DB write failure must retry the update, not drop the tap."""
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1001234567890")
    header = _sig()
    _insert(header)
    monkeypatch.setattr(telegram_poll.heartbeat, "heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        telegram_sender, "answer_callback_query", lambda *a, **k: None
    )
    monkeypatch.setattr(
        telegram_sender, "edit_message_reply_markup", lambda *a, **k: None
    )
    batch = [
        {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "from": {"id": -1001234567890},
                "message": {"message_id": 1, "chat": {"id": -1001234567890}},
                "data": f"a:1:{header.alert_id[:24]}",
            },
        }
    ]
    monkeypatch.setattr(
        telegram_sender, "get_updates", lambda offset, **kwargs: batch
    )

    def _locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository, "update_signal_acted", _locked)

    with pytest.raises(sqlite3.OperationalError):
        telegram_poll.run()

    # The offset must not have advanced, so the tap is retried next poll.
    assert repository.get_app_state("telegram_update_offset") is None


def test_preflight_webhook_check(monkeypatch):
    from signal_system import preflight

    monkeypatch.setattr(
        preflight, "_config_or_none", lambda: object()
    )
    monkeypatch.setattr(
        telegram_sender, "get_webhook_info", lambda: {"url": ""}
    )
    assert preflight.check_telegram_webhook().status == preflight.PASS

    monkeypatch.setattr(
        telegram_sender, "get_webhook_info", lambda: {"url": "https://example/webhook"}
    )
    check = preflight.check_telegram_webhook()
    assert check.status == preflight.FAIL
    assert "409" in check.message
