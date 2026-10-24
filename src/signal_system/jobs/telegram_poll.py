"""telegram-poll — ingest Acted/Passed taps and reply-to notes (Plan 12e3).

Telegram stores undelivered updates for 24 hours; running every 15 minutes
between 07:00 and 23:00 ET leaves wide margin. All handlers are idempotent, and
the getUpdates offset is persisted after each update (at-least-once delivery).

Authorization is mandatory: an update is processed only when both the chat id
and the sender id equal TELEGRAM_CHAT_ID (a private operator chat). Anything
else is ignored and logged with the sender id only.

After the updates, each run sweeps alerts stuck PENDING (a failed send) and
re-sends them — this job is the single owner of that sweep, so a stuck alert
reaches the operator within one poll interval (see common.resend_unconfirmed).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.data.holdings import held_ticker_set
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import resend_unconfirmed
from signal_system.monitoring import heartbeat
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_OFFSET_KEY = "telegram_update_offset"
_CALLBACK_RE = re.compile(r"^a:([01]):([0-9a-f]{4,24})$")
_NOTE_PREFIX = "tg: "
_NOTE_MAX_CHARS = 500
_ALLOWED_UPDATES = ["callback_query", "message"]


def _chat_and_sender(update: dict) -> tuple[str | None, str | None]:
    callback = update.get("callback_query")
    message = update.get("message")
    payload = callback or message or {}
    chat = (payload.get("message") or {}).get("chat") if callback else payload.get("chat")
    sender = payload.get("from") or {}
    chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else None
    sender_id = (
        str(sender.get("id")) if isinstance(sender, dict) and sender.get("id") is not None else None
    )
    return chat_id, sender_id


def _is_authorized(update: dict) -> bool:
    chat_id, sender_id = _chat_and_sender(update)
    return (
        chat_id is not None
        and sender_id is not None
        and chat_id == str(config.TELEGRAM_CHAT_ID)
        and sender_id == str(config.TELEGRAM_CHAT_ID)
    )


def _sanitize_note(text: object) -> str:
    raw = str(text or "")
    cleaned = "".join(
        ch for ch in raw if unicodedata.category(ch)[0] != "C" or ch in ("\n", "\t")
    )
    cleaned = " ".join(cleaned.split())
    return (_NOTE_PREFIX + cleaned)[:_NOTE_MAX_CHARS]


def _answer_callback(callback_id: str, text: str) -> None:
    """Best-effort callback acknowledgement.

    Telegram rejects answers to taps older than its short callback window. The
    acted/passed write already happened, so a failed acknowledgement must not
    abort the handler — otherwise the follow-up keyboard edit never runs and
    every poll logs an exception for an already-processed tap.
    """
    try:
        telegram_sender.answer_callback_query(callback_id, text)
    except Exception:
        logger.warning(
            "telegram-poll: could not answer callback %s (likely expired)",
            callback_id[:16],
            exc_info=True,
        )


def _handle_callback(callback: dict) -> None:
    data = str(callback.get("data") or callback.get("callback_data") or "")
    callback_id = str(callback.get("id") or "")
    match = _CALLBACK_RE.match(data)
    if not match:
        _answer_callback(callback_id, "Not found")
        logger.warning("telegram-poll: unrecognised callback data %r", data[:40])
        return

    acted = match.group(1) == "1"
    prefix = match.group(2)
    matches = repository.find_signals_by_alert_prefix(prefix)
    if len(matches) != 1:
        _answer_callback(callback_id, "Not found")
        logger.warning(
            "telegram-poll: callback prefix %r resolved to %d DELIVERED rows",
            prefix,
            len(matches),
        )
        return

    alert_id = matches[0]["alert_id"]
    repository.update_signal_acted(alert_id, acted=acted)
    repository.mark_telegram_message_answered(
        chat_id=str(config.TELEGRAM_CHAT_ID), alert_id=alert_id, kind="alert"
    )
    _answer_callback(callback_id, "Logged: Acted" if acted else "Logged: Passed")

    message = callback.get("message") or {}
    message_id = message.get("message_id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if message_id is not None and chat_id is not None:
        # Offer the opposite action so a mis-tap is one more tap to correct.
        next_acted = not acted
        label = "↩ Change to Passed" if next_acted is False else "↩ Change to Acted"
        try:
            telegram_sender.edit_message_reply_markup(
                str(chat_id),
                int(message_id),
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": label,
                                "callback_data": f"a:{1 if next_acted else 0}:{prefix}",
                            }
                        ]
                    ]
                },
            )
        except Exception:
            logger.warning(
                "telegram-poll: could not edit keyboard for alert %s",
                alert_id[:12],
                exc_info=True,
            )


def _handle_message(message: dict) -> None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return  # only reply-to-note is supported; no free-text commands
    message_id = reply.get("message_id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if message_id is None or chat_id is None:
        return

    row = repository.get_telegram_message_by_id(str(chat_id), int(message_id))
    if row is None:
        logger.warning(
            "telegram-poll: reply to unknown message_id %r", message_id
        )
        return

    note = _sanitize_note(message.get("text"))
    repository.update_signal_user_note(row["alert_id"], note)
    repository.mark_telegram_message_answered(
        chat_id=str(config.TELEGRAM_CHAT_ID),
        alert_id=row["alert_id"],
        kind=row["kind"],
    )


def _process_update(update: dict) -> None:
    if not _is_authorized(update):
        _chat, sender_id = _chat_and_sender(update)
        logger.warning(
            "telegram-poll: ignored unauthorized update (sender id %s)", sender_id
        )
        return
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _handle_callback(callback)
        return
    message = update.get("message")
    if isinstance(message, dict):
        _handle_message(message)


def run() -> None:
    run_id = repository.insert_run("telegram-poll")
    try:
        with heartbeat.heartbeat(job="telegram-poll"):
            offset = int(repository.get_app_state(_OFFSET_KEY) or 0)
            updates = telegram_sender.get_updates(
                offset, timeout=0, allowed_updates=_ALLOWED_UPDATES
            )
            for update in updates:
                update_id = update.get("update_id")
                try:
                    _process_update(update)
                except sqlite3.Error:
                    # A DB write failed (e.g. a lock timeout while recording an
                    # Acted/Passed tap). Do NOT advance the offset: the update
                    # must be retried next poll or the operator's decision is
                    # lost. The run fails and the heartbeat pages.
                    raise
                except Exception:
                    # A malformed update must not wedge the poller forever.
                    logger.exception(
                        "telegram-poll: failed to process update %r", update_id
                    )
                if update_id is not None:
                    offset = max(offset, int(update_id) + 1)
                    repository.set_app_state(_OFFSET_KEY, str(offset))
            # Taps are recorded first, so a Telegram outage during the sweep
            # never costs an operator decision. A failed re-send fails the run.
            resend_unconfirmed(
                datetime.now(_ET), held_tickers=held_ticker_set(fallback_to_core=True)
            )
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
