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


def _ping(path: str, *, job: str | None = None) -> None:
    """
    Send a ping to healthchecks.io.

    Args:
        path: The endpoint path (e.g., "/start", "/fail", or "")
        job: Job name for per-job UUID resolution (None = shared UUID).

    If the ping fails (network error, timeout, etc.), logs the error
    but does not raise to avoid interfering with job execution.
    """
    url = f"{BASE_URL}/{config.healthchecks_uuid_for(job)}{path}"
    try:
        response = httpx.post(url, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Heartbeat ping failed: %s", exc)


@contextlib.contextmanager
def heartbeat(job: str | None = None):
    """
    Context manager for job heartbeat monitoring.

    Args:
        job: Job name resolved to a per-job healthchecks UUID via
            config.healthchecks_uuid_for(); None uses the shared UUID.

    Usage:
        with heartbeat():
            # run job code

    - On entry: sends /start ping
    - On clean exit: sends success ping (empty path)
    - On exception: sends /fail ping and re-raises the exception

    Network failures in pings are logged but do not interfere with job execution.
    """
    _ping("/start", job=job)
    try:
        yield
    except Exception:
        _ping("/fail", job=job)
        raise
    else:
        _ping("", job=job)
