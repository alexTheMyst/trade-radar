import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    """
    Retrieve a required environment variable.

    Raises RuntimeError if the variable is not set or is empty.
    """
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. Check your .env file."
        )
    return val


def _optional(name: str, default: str) -> str:
    """Retrieve an optional environment variable, returning default if unset or empty."""
    return os.environ.get(name, default).strip() or default


FINNHUB_API_KEY = _require("FINNHUB_API_KEY")
HEALTHCHECKS_UUID = _require("HEALTHCHECKS_UUID")
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

# Phase 1 additions
ANTHROPIC_MODEL: str = _require("ANTHROPIC_MODEL")  # e.g. "claude-sonnet-4-6"
THESIS_PATH: str = _optional("THESIS_PATH", "thesis.yaml")

# Advisor phase -- set ADVISOR_SHADOW_MODE=false after IC review validates the matrix
ADVISOR_SHADOW_MODE: bool = _optional("ADVISOR_SHADOW_MODE", "true").lower() != "false"
