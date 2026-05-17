# Telegram Delivery — Design Spec

**Date:** 2026-05-17  
**Status:** Approved  

## Summary

Replace Gmail SMTP delivery with Telegram Bot API delivery. Full replacement — no email fallback. Plain text messages for MVP. Stdlib-only (`urllib.request`), no new dependencies.

## Scope

Nine files updated, one deleted, one created.

| Action | File |
|--------|------|
| Delete | `src/signal_system/delivery/email_sender.py` |
| Create | `src/signal_system/delivery/telegram_sender.py` |
| Update | `src/signal_system/config.py` |
| Update | `src/signal_system/jobs/daily_close.py` |
| Update | `src/signal_system/jobs/news_morning.py` |
| Update | `src/signal_system/jobs/discovery.py` |
| Update | `tests/conftest.py` |
| Update | `tests/test_smoke.py` |
| Update | `tests/test_job_orchestration.py` |
| Update | `tests/test_discovery_agent.py` |
| Update | `.env.example` |

`common.py`, the router, repository, classifier, models, and heartbeat are untouched.

## New Module: `delivery/telegram_sender.py`

```python
import json
import urllib.request
from signal_system import config

def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": config.TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as _:
        pass
```

- `urllib.error.HTTPError` on non-2xx propagates to the heartbeat context manager, which pings `/fail` and re-raises. No special handling needed here.
- `timeout=10` prevents a hung API call from blocking the Task Scheduler slot.
- No message truncation for MVP — digests for a small core holdings universe are well under Telegram's 4096-char limit.

## Config Changes

**Remove** from `config.py` and `.env.example`:
- `GMAIL_USERNAME`
- `GMAIL_APP_PASSWORD`
- `ALERT_RECIPIENT_EMAIL`

**Add**:
- `TELEGRAM_BOT_TOKEN` — Bot API token from @BotFather
- `TELEGRAM_CHAT_ID` — target chat/channel ID (string; negative for group chats/channels)

## Call Site Changes

### `daily_close.py`

```python
# before
from signal_system.delivery import email_sender
email_sender.send_email(subject=f"Daily Close — SPY {spy_close:.2f}", body=f"SPY closed at ...")

# after
from signal_system.delivery import telegram_sender
telegram_sender.send_message(f"Daily Close — SPY {spy_close:.2f}\n\nSPY closed at ...")
```

### `news_morning.py` and `discovery.py`

Both have an identical `_send_digest_once` helper. Updated to:

```python
from signal_system.delivery import telegram_sender

def _send_digest_once(*, subject: str, body: str) -> None:
    telegram_sender.send_message(f"{subject}\n\n{body}")
```

The `hasattr(email_sender.send_email, "call_count")` guard is removed — it tested mock implementation details, not real behavior. Single-call-site structure enforces "sent exactly once" structurally.

## Test Changes

### Mock path updates (mechanical find-and-replace)

| Old | New |
|-----|-----|
| `signal_system.delivery.email_sender.send_email` | `signal_system.delivery.telegram_sender.send_message` |
| `signal_system.jobs.news_morning.email_sender.send_email` | `signal_system.jobs.news_morning.telegram_sender.send_message` |
| `signal_system.jobs.discovery.email_sender.send_email` | `signal_system.jobs.discovery.telegram_sender.send_message` |

### `conftest.py`

Remove:
```python
os.environ.setdefault("GMAIL_USERNAME", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test_password")
```

Add:
```python
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1001234567890")
```

`ALERT_RECIPIENT_EMAIL` is removed entirely (no equivalent in Telegram — the chat_id is the recipient).

### `test_smoke.py`

- `test_daily_close_email_failure`: replace `smtplib.SMTPException` injection with `urllib.error.HTTPError`. Drop `import smtplib`, add `import urllib.error`.
- Add two new tests for `telegram_sender`:
  1. Happy path: mock `urllib.request.urlopen`, assert called once with correct URL and JSON payload.
  2. Failure path: mock `urlopen` to raise `urllib.error.HTTPError`, assert it propagates.

### `test_discovery_agent.py`

The import-isolation test that blocks `signal_system.delivery.email_sender` updates its blocked path to `signal_system.delivery.telegram_sender`.

## Error Handling

No changes to error handling architecture. `urllib.error.HTTPError` and `urllib.error.URLError` (network failure) both propagate out of `send_message` like `smtplib.SMTPException` did — the heartbeat context manager catches all exceptions, pings `/fail`, and re-raises.

## Out of Scope (MVP)

- Message formatting (MarkdownV2 / HTML parse mode)
- Message length truncation guard
- Telegram-specific retry logic (tenacity already handles 429s at the Finnhub layer; Telegram rate limits are generous for single-user bots)
- Multiple recipients / channels
