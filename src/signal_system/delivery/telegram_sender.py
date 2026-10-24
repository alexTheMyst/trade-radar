import json
import logging
import urllib.error
import urllib.request
from typing import Any

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from signal_system import config
from signal_system.models import Signal

logger = logging.getLogger(__name__)


TELEGRAM_MESSAGE_MAX_LENGTH = 4096
_API_BASE = "https://api.telegram.org/bot"


def _split_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MESSAGE_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MESSAGE_MAX_LENGTH:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_MAX_LENGTH + 1)
        if split_at <= 0:
            split_at = TELEGRAM_MESSAGE_MAX_LENGTH

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]

    return chunks


def _call(method: str, payload: dict[str, Any], *, timeout: int = 10) -> Any:
    """POST one Telegram Bot API method and return its ``result`` payload."""
    url = f"{_API_BASE}{config.TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read() if hasattr(response, "read") else b""
    parsed = json.loads(body) if body else {}
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        raise RuntimeError(f"Telegram {method} failed: {parsed.get('description')}")
    return parsed.get("result") if isinstance(parsed, dict) else parsed


def _is_transient_send_error(exc: BaseException) -> bool:
    """True for failures worth retrying: rate limit, server error, network drop.

    A 4xx other than 429 (bad token, bad chat id, malformed payload) is
    permanent — retrying only delays the failure that should page.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


# A transient Telegram failure used to leave the run's alerts stuck PENDING
# until a sweep re-sent them. Retrying the send itself keeps most blips from
# ever reaching that state. Retries are per call (per chunk), so a retried
# chunk can at worst arrive twice — a duplicate is preferable to a gap.
_SEND_RETRY = retry(
    retry=retry_if_exception(_is_transient_send_error),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


@_SEND_RETRY
def _send(payload: dict[str, Any]) -> Any:
    return _call("sendMessage", payload)


def send_message(text: str) -> None:
    for chunk in _split_message(text):
        _send({"chat_id": config.TELEGRAM_CHAT_ID, "text": chunk})


def _alert_text(signal: Signal, *, held: bool, prefix: str = "") -> str:
    ticker = signal.ticker or "MACRO"
    header = f"{ticker} · {signal.agent} · {signal.severity}"
    if held:
        header += " · HELD"
    return f"{prefix}{header}\n{signal.title}"


def _action_keyboard(alert_id: str) -> dict[str, Any]:
    # callback_data is "a:<0|1>:<24 hex chars>" — 28 bytes, well under
    # Telegram's 64-byte limit, and resolved against DELIVERED rows only.
    prefix = alert_id[:24]
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Acted", "callback_data": f"a:1:{prefix}"},
                {"text": "⏭ Passed", "callback_data": f"a:0:{prefix}"},
            ]
        ]
    }


def send_alert_with_buttons(
    signal: Signal, *, held: bool = False, prefix: str = ""
) -> int | None:
    """Send one alert with Acted/Passed buttons; return the Telegram message_id."""
    result = _send(
        {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": _alert_text(signal, held=held, prefix=prefix),
            "reply_markup": _action_keyboard(signal.alert_id),
        }
    )
    if isinstance(result, dict):
        return result.get("message_id")
    return None


def answer_callback_query(callback_query_id: str, text: str) -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def edit_message_reply_markup(
    chat_id: str, message_id: int, reply_markup: dict[str, Any] | None
) -> None:
    _call(
        "editMessageReplyMarkup",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        },
    )


def get_updates(
    offset: int, *, timeout: int = 0, allowed_updates: list[str] | None = None
) -> list[dict]:
    payload: dict[str, Any] = {"offset": offset, "timeout": timeout}
    if allowed_updates is not None:
        payload["allowed_updates"] = allowed_updates
    result = _call("getUpdates", payload, timeout=timeout + 10)
    return result if isinstance(result, list) else []


def get_webhook_info() -> dict:
    result = _call("getWebhookInfo", {})
    return result if isinstance(result, dict) else {}
