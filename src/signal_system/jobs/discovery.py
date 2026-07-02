from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.data.universe import get_todays_universe, require_non_empty_universe
from signal_system.delivery import telegram_sender
from signal_system.discovery.discovery_agent import score_universe
from signal_system.jobs.common import (
    PersistenceSummary,
    confirm_delivered_signals,
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
    telegram_sender.send_message(f"{subject}\n\n{body}")


def run() -> None:
    run_id = repository.insert_run("discovery")
    try:
        with heartbeat.heartbeat():
            now_et = _now_et()
            tickers = require_non_empty_universe(get_todays_universe(), job="discovery")
            discovered_signals = score_universe(tickers, run_id, now_et.date().isoformat())

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
            confirm_delivered_signals(persistence_summary.delivered_signals)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
