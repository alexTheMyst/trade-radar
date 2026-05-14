"""
Heartbeat context manager for healthchecks.io integration.

Wraps scheduled jobs to detect silent failures via health check pings.
"""

import contextlib
import logging
import httpx

from signal_system import config

logger = logging.getLogger(__name__)
BASE_URL = "https://hc-ping.com"


def _ping(path: str) -> None:
    """
    Send a ping to healthchecks.io.

    Args:
        path: The endpoint path (e.g., "/start", "/fail", or "")

    If the ping fails (network error, timeout, etc.), logs the error
    but does not raise to avoid interfering with job execution.
    """
    url = f"{BASE_URL}/{config.HEALTHCHECKS_UUID}{path}"
    try:
        httpx.post(url, timeout=10)
    except Exception as exc:
        logger.warning("Heartbeat ping failed: %s", exc)


@contextlib.contextmanager
def heartbeat():
    """
    Context manager for job heartbeat monitoring.

    Usage:
        with heartbeat():
            # run job code

    - On entry: sends /start ping
    - On clean exit: sends success ping (empty path)
    - On exception: sends /fail ping and re-raises the exception

    Network failures in pings are logged but do not interfere with job execution.
    """
    _ping("/start")
    try:
        yield
    except Exception:
        _ping("/fail")
        raise
    else:
        _ping("")
