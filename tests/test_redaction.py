"""Tests for secret redaction in reports and logs.

The live failure this guards against: finnhub-python sets the API key as a
`token` query parameter, requests embeds the full URL in its exception
messages, and tenacity's before_sleep_log hook logs that message on every
transient failure — writing the key into the job log on any network blip.
"""

from __future__ import annotations

import logging

import pytest

from signal_system import redaction

_KEY = "LIVEKEY_deadbeef1234"

_LEAKY_URL_ERROR = (
    "ProxyError: HTTPSConnectionPool(host='api.finnhub.io', port=443): "
    f"Max retries exceeded with url: /api/v1/quote?token={_KEY}&symbol=SPY"
)


def test_redact_strips_token_query_param():
    out = redaction.redact(_LEAKY_URL_ERROR)
    assert _KEY not in out
    assert redaction.REDACTED in out
    # Diagnostic context survives.
    assert "symbol=SPY" in out
    assert "api.finnhub.io" in out


@pytest.mark.parametrize(
    "raw",
    [
        f"url?token={_KEY}",
        f"url?api_key={_KEY}",
        f"url?apiKey={_KEY}",
        f"url?access_token={_KEY}",
        f"url?secret={_KEY}",
        f"url?password={_KEY}",
        f"Authorization: Bearer {_KEY}",
    ],
)
def test_redact_covers_common_secret_shapes(raw):
    assert _KEY not in redaction.redact(raw)


def test_redact_strips_healthchecks_uuid_from_path():
    out = redaction.redact("POST https://hc-ping.com/0d1e2f3a-4b5c-6d7e-8f90-a1b2c3d4e5f6/start")
    assert "0d1e2f3a" not in out
    assert redaction.REDACTED in out


def test_redact_strips_literal_env_secret(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", _KEY)
    out = redaction.redact(f"client blew up somewhere with {_KEY} inline")
    assert _KEY not in out
    assert redaction.REDACTED in out


def test_redact_ignores_short_env_values(monkeypatch):
    """Short values are too collision-prone to substring-match safely."""
    monkeypatch.setenv("FINNHUB_API_KEY", "abc")
    assert redaction.redact("abc is a common substring") == "abc is a common substring"


def test_redact_preserves_anthropic_model(monkeypatch):
    """ANTHROPIC_MODEL is not a secret and preflight prints it deliberately."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    out = redaction.redact("model 'claude-sonnet-4-6' rejected params")
    assert "claude-sonnet-4-6" in out


def test_redact_handles_empty_and_none_safe():
    assert redaction.redact("") == ""


# --- logging filter ----------------------------------------------------------


@pytest.fixture
def captured_logger():
    """A logger wired to a root handler carrying the redaction filter."""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    root = logging.getLogger()
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    redaction.install_redaction_filter(root)
    child = logging.getLogger("signal_system.data.finnhub_client")
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield child, records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_filter_scrubs_message(captured_logger):
    child, records = captured_logger
    child.warning(_LEAKY_URL_ERROR)
    assert records
    assert _KEY not in records[0]
    assert redaction.REDACTED in records[0]


def test_filter_scrubs_lazy_format_args(captured_logger):
    """tenacity's before_sleep_log logs with %s args, not a formatted string."""
    child, records = captured_logger
    child.warning("Retrying %s as it raised %s", "fetch_spy_close", _LEAKY_URL_ERROR)
    assert records
    assert _KEY not in records[0]
    assert "fetch_spy_close" in records[0]


def test_filter_scrubs_exception_object_arg(captured_logger):
    """The real leak: `logger.error("...: %s", exc)` with a non-str arg.

    Per-arg redaction missed this because an exception is not a str, so the
    key survived until %s-formatting at emit time.
    """
    child, records = captured_logger
    exc = ConnectionError(_LEAKY_URL_ERROR)
    child.error("Giving up on general news after exhausted retries: %s", exc)
    assert records
    assert _KEY not in records[0]
    assert "Giving up on general news" in records[0]


def test_filter_scrubs_exception_text(captured_logger):
    child, records = captured_logger
    try:
        raise ConnectionError(_LEAKY_URL_ERROR)
    except ConnectionError:
        child.exception("giving up")
    assert records
    assert _KEY not in records[0]


def test_filter_survives_malformed_format_string(captured_logger):
    """A bad template must not break logging, and must not leak."""
    child, records = captured_logger
    child.warning("missing placeholder %s %s", _LEAKY_URL_ERROR)
    assert records
    assert _KEY not in records[0]


def test_filter_leaves_clean_records_untouched(captured_logger):
    child, records = captured_logger
    child.warning("scanned %d tickers, %d alerts", 16, 4)
    assert records[0] == "scanned 16 tickers, 4 alerts"


def test_install_is_idempotent():
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        redaction.install_redaction_filter(root)
        redaction.install_redaction_filter(root)
        filters = [f for f in handler.filters if isinstance(f, redaction.RedactingFilter)]
        assert len(filters) == 1
    finally:
        root.removeHandler(handler)
