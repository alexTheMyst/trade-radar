from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.data.universe import (
    get_core_holdings,
    get_industries,
    get_todays_universe,
    require_non_empty_universe,
)
from signal_system.delivery import telegram_sender
from signal_system.discovery.discovery_agent import (
    score_universe,
    industry_rollup,
)
from signal_system.jobs.common import (
    PersistenceSummary,
    compute_score_deltas,
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


def _render_industry_rollup_section(composite_by_ticker: dict[str, float]) -> str | None:
    """Return the industry momentum digest section, or None when not available."""
    industries = get_industries()
    if not industries:
        return None  # universe.csv lacks industry columns (pre-4.3)
    rollups = industry_rollup(composite_by_ticker, industries)
    if not rollups:
        return None
    lines = [f"Industry momentum (median composite, n>=3)"]
    for r in rollups:
        lines.append(f"  {r.industry}: {r.median_composite:.0f} (n={r.member_count})")
    return "\n".join(lines)


def run() -> None:
    run_id = repository.insert_run("discovery")
    try:
        with heartbeat.heartbeat(job="discovery"):
            now_et = _now_et()
            tickers = require_non_empty_universe(get_todays_universe(), job="discovery")
            score_result = score_universe(tickers, run_id, now_et.date().isoformat())

            routed = route_signals(score_result.signals)
            # Deltas MUST be computed before persistence — persisting first lets
            # get_previous_score() find this run's own rows instead of a prior scan.
            score_deltas = compute_score_deltas(routed)
            persistence_summary: PersistenceSummary = persist_routed_signals(routed)
            # Discovery scans a rotating universe wider than core holdings, so the
            # HELD set is a separate lookup — not derivable from `tickers`.
            held_tickers = set(get_core_holdings())
            digest = render_digest(
                job_name="discovery",
                scanned_tickers=len(tickers),
                routed_signals=routed,
                held_tickers=held_tickers,
                score_deltas=score_deltas,
                status_counts=persistence_summary.status_counts,
            )
            industry_section = _render_industry_rollup_section(score_result.composite_by_ticker)
            body = f"{digest.body}\n\n{industry_section}" if industry_section else digest.body
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                expected_counts=persistence_summary.status_counts,
                delivered_signals=persistence_summary.delivered_signals,
            )
            _send_digest_once(subject=digest.subject, body=body)
            confirm_delivered_signals(persistence_summary.delivered_signals)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
