from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.data.universe import get_todays_universe
from signal_system.delivery import email_sender
from signal_system.discovery.discovery_agent import score_universe
from signal_system.jobs.common import (
    PersistenceSummary,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
from signal_system.monitoring import heartbeat
from signal_system.router import route_signals
from signal_system.state import repository
_ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(_ET)


def _send_digest_once(*, subject: str, body: str) -> None:
    email_sender.send_email(subject=subject, body=body)
    if hasattr(email_sender.send_email, "call_count") and email_sender.send_email.call_count != 1:
        raise RuntimeError("Digest email must be sent exactly once")


def run() -> None:
    run_id = repository.insert_run("discovery")
    try:
        with heartbeat.heartbeat():
            now_et = _now_et()
            tickers = get_todays_universe()
            discovered_signals = score_universe(tickers, run_id, now_et.date().isoformat())

            if config.DISCOVERY_PHASE == "A":
                repository.update_run(run_id, "success")
                return

            persistence_summary: PersistenceSummary = persist_routed_signals(
                route_signals(discovered_signals)
            )
            digest = render_digest(
                job_name="discovery",
                scanned_tickers=len(tickers),
                delivered_signals=persistence_summary.delivered_signals,
                status_counts=persistence_summary.status_counts,
            )
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                expected_counts=persistence_summary.status_counts,
                delivered_signals=persistence_summary.delivered_signals,
            )
            _send_digest_once(subject=digest.subject, body=digest.body)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
