"""
conftest.py — set dummy env vars before any signal_system module is imported.

Module-level setdefault() calls run at collection time, before any test
module is imported, so config.py's _require() calls see values and don't raise.
"""

import os

# Must run before any signal_system import (fixture timing is too late)
os.environ.setdefault("FINNHUB_API_KEY", "test_key")
os.environ.setdefault("HEALTHCHECKS_UUID", "test-uuid-1234")
os.environ.setdefault("GMAIL_USERNAME", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test_password")
os.environ.setdefault("ALERT_RECIPIENT_EMAIL", "recipient@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _set_dummy_env():
    """Explicit session fixture for documentation clarity; env is already set above."""
    pass
