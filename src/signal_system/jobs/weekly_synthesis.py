"""weekly-synthesis job — distills the MONITORING corpus into pillar candidates.

Standard job shape: runs row, heartbeat, print + Telegram delivery. Does NOT
touch the router — this is analysis, not routing.
"""

from __future__ import annotations

import logging

from signal_system.delivery.telegram_sender import send_message
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.state import repository
from signal_system.synthesis.weekly_synthesis import run_weekly_synthesis

logger = logging.getLogger(__name__)


def run() -> None:
    run_id = repository.insert_run("weekly-synthesis")
    try:
        with heartbeat():
            text = run_weekly_synthesis(run_id=run_id)
            logger.info("weekly-synthesis:\n%s", text)
            print(text)
            send_message(text)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
