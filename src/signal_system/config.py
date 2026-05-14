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


FINNHUB_API_KEY = _require("FINNHUB_API_KEY")
HEALTHCHECKS_UUID = _require("HEALTHCHECKS_UUID")
GMAIL_USERNAME = _require("GMAIL_USERNAME")
GMAIL_APP_PASSWORD = _require("GMAIL_APP_PASSWORD")
ALERT_RECIPIENT_EMAIL = _require("ALERT_RECIPIENT_EMAIL")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
