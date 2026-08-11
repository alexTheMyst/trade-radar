"""Secret redaction for anything that reaches a human — reports, logs, alerts.

Third-party clients put credentials in request URLs: finnhub-python sets
`session.params["token"]`, so the API key ends up inside every requests /
urllib3 exception message. Those messages reach the operator two ways:

  1. Interpolated into a preflight report line.
  2. Logged by tenacity's `before_sleep_log` retry hook on every transient
     failure, in every scheduled job.

Both are pasted into issues, CI logs and chat. This module is the single place
that scrubs them.

Deliberately imports nothing from `signal_system` — it must be usable before
`config` is importable (a broken .env is exactly when preflight needs it).
"""
from __future__ import annotations

import logging
import os
import re

REDACTED = "***REDACTED***"

# Env vars whose literal values must never be printed. ANTHROPIC_MODEL is
# deliberately absent: it is not a secret and preflight prints it on purpose,
# so redacting it would corrupt a legitimate diagnostic.
SECRET_ENV_VARS: tuple[str, ...] = (
    "FINNHUB_API_KEY",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "HEALTHCHECKS_UUID",
)

# Substring matching below this length is too collision-prone to be safe.
_MIN_LITERAL_LEN = 8

_QUERY_SECRET_RE = re.compile(
    r"((?:token|api[_-]?key|apikey|secret|password|access[_-]?token)=)[^&\s'\"]+",
    re.IGNORECASE,
)

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)

# healthchecks.io pings put the check UUID in the path, not a query parameter.
_HC_UUID_RE = re.compile(
    r"(hc-ping\.com/)[0-9a-fA-F-]{8,}",
)


def redact(text: str) -> str:
    """Return *text* with query-string secrets, bearer tokens and known
    literal secret values replaced by REDACTED.

    Non-secret context is preserved so the message stays diagnosable.
    """
    if not text:
        return text
    out = _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", text)
    out = _BEARER_RE.sub(rf"\1{REDACTED}", out)
    out = _HC_UUID_RE.sub(rf"\1{REDACTED}", out)
    for name in SECRET_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= _MIN_LITERAL_LEN:
            out = out.replace(value, REDACTED)
    return out


class RedactingFilter(logging.Filter):
    """Scrub secrets from log records before a handler emits them.

    Attach to a *handler*, not a logger: a filter on a logger only sees records
    logged directly to it, while a handler filter sees everything that reaches
    the handler — including records propagated up from child loggers such as
    `signal_system.data.finnhub_client`, which is where the retry hook logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the fully-formatted message rather than msg/args separately.
        # Args are frequently non-str — tenacity's retry hook and this project's
        # own `logger.error("...: %s", exc)` calls pass exception objects, whose
        # str() carries the leaky URL. Formatting first catches every arg type;
        # per-arg redaction silently missed exceptions.
        try:
            record.msg = redact(record.getMessage())
        except Exception:
            # A malformed format string must not break logging. Fall back to
            # scrubbing the raw template and dropping args entirely, which is
            # lossy but never leaks.
            record.msg = redact(str(record.msg))
        record.args = ()

        # Tracebacks are rendered from exc_info at format time; scrub both the
        # cached text and the exception instance's own message.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            scrubbed = tuple(
                redact(arg) if isinstance(arg, str) else arg for arg in exc.args
            )
            if scrubbed != exc.args:
                exc.args = scrubbed
        return True


def install_redaction_filter(logger: logging.Logger | None = None) -> None:
    """Attach RedactingFilter to every handler on *logger* (default: root).

    Idempotent — calling twice does not double-filter.
    """
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
