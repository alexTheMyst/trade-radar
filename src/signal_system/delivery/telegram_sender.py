import json
import urllib.request
from signal_system import config


TELEGRAM_MESSAGE_MAX_LENGTH = 4096


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


def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in _split_message(text):
        payload = json.dumps(
            {"chat_id": config.TELEGRAM_CHAT_ID, "text": chunk}
        ).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
