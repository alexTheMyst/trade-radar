"""
conftest.py — set dummy env vars before any signal_system module is imported.

Module-level setdefault() calls run at collection time, before any test
module is imported, so config.py's _require() calls see values and don't raise.
"""

import os

# Must run before any signal_system import (fixture timing is too late)
os.environ.setdefault("FINNHUB_API_KEY", "test_key")
os.environ.setdefault("HEALTHCHECKS_UUID", "test-uuid-1234")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1001234567890")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("ANTHROPIC_MODEL", "claude-sonnet-4-6")

