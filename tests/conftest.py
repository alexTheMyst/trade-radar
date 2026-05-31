"""
conftest.py — set dummy env vars before any signal_system module is imported.

These module-level assignments run at collection time, before any test module
is imported, so config.py's _require() calls see values and don't raise.

We can't use os.environ.setdefault(): the host environment often has the keys
present-but-empty (e.g. ANTHROPIC_API_KEY=''), which setdefault treats as
"already set" and won't replace. config._require() strips and rejects empty
strings, so we overwrite any unset OR empty var with a test value.
"""

import os

# Must run before any signal_system import (fixture timing is too late)
_TEST_ENV = {
    "FINNHUB_API_KEY": "test_key",
    "HEALTHCHECKS_UUID": "test-uuid-1234",
    "TELEGRAM_BOT_TOKEN": "123456:test_token",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "ANTHROPIC_API_KEY": "test_anthropic_key",
    "ANTHROPIC_MODEL": "claude-sonnet-4-6",
}
for _key, _value in _TEST_ENV.items():
    if not os.environ.get(_key):  # unset or empty string
        os.environ[_key] = _value

