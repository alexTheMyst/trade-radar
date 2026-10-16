"""dispatch job — evening cross-agent INFO arbitration (Plan 06, Option D).

Agent jobs (news-morning 09:00, discovery 10:30, sentinel 17:00 ET) route
ACTION_REQUIRED immediately and stage every INFORMATIONAL signal as a
CANDIDATE. This job (weekdays 17:40 ET) pools the day's candidates from every
agent, routes them ONCE through route_signals() on the unified
competition_score() scale, and sends a single digest. The delivered set is
therefore a function of score, not of which job happened to run first.

Candidates left over from an earlier day (dispatch did not run) are expired
to SUPPRESSED with suppression_reason='expired' — stale INFO is not worth
sending, but the rows stay measurable by outcome-backfill — and the digest
says so loudly.

Same send discipline as the other jobs: persist outcomes with DELIVERED
written as PENDING, read counts back from the database, validate, send, then
flip PENDING → DELIVERED.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.data.universe import get_core_holdings
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import (
    RoutedSignal,
    confirm_delivered_signals,
    format_score,
)
from signal_system.models import (
    DISCOVERY_AGENT,
    EXIT_SENTINEL_AGENT,
    NEWS_CLASSIFIER_AGENT,
    Signal,
    competition_score,
)
from signal_system.monitoring import heartbeat
from signal_system.router import BUDGET_INFO, dispatch_candidates
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Every agent is always listed in the digest, even at zero, so "contributed
# nothing" and "contributed 68 that all lost" look different on the page.
AGENTS: tuple[str, ...] = (NEWS_CLASSIFIER_AGENT, DISCOVERY_AGENT, EXIT_SENTINEL_AGENT)

# Suppressed candidates listed in the digest body (the rest are counted only).
MAX_SUPPRESSED_LISTED = 10

EXPIRED_REASON = "expired"


def _now_et() -> datetime:
    return datetime.now(_ET)


def _agent_counts(signals: list[Signal]) -> str:
    counts = Counter(s.agent for s in signals)
    return ", ".join(f"{agent} {counts.get(agent, 0)}" for agent in AGENTS)


def _line(signal: Signal, demoted_from: str | None, held: set[str]) -> str:
    ticker = signal.ticker or "MACRO"
    severity = signal.severity
    if demoted_from:
        severity += f" (demoted from {demoted_from})"
    line = (
        f"{ticker} — {format_score(signal.agent, signal.score)} "
        f"[{competition_score(signal):.0f}] — {signal.agent} — {severity}"
    )
    if ticker in held:
        line += " — HELD"
    return line


def render_dispatch_digest(
    *,
    routed: list[RoutedSignal],
    expired: list[Signal],
    held_tickers: set[str],
) -> tuple[str, str]:
    """Return (subject, body) for the evening dispatch digest."""
    pooled = [s for s, _rs, _dmf, _r in routed]
    delivered = [(s, dmf) for s, rs, dmf, _r in routed if rs == "DELIVERED"]
    suppressed = [(s, dmf, r) for s, rs, dmf, r in routed if rs == "SUPPRESSED"]
    reasons = Counter(r or "unknown" for _s, _dmf, r in suppressed)

    alert_label = "alert" if len(delivered) == 1 else "alerts"
    lines = [
        f"Candidates pooled: {len(pooled)} ({_agent_counts(pooled)})",
        f"Delivered: {len(delivered)} ({_agent_counts([s for s, _ in delivered])})",
        f"Suppressed: {len(suppressed)} "
        f"(outscored {reasons.get('outscored', 0)}, "
        f"cap_exhausted {reasons.get('cap_exhausted', 0)})",
    ]
    if expired:
        lines.append(
            f"⚠️ Expired undispatched candidates from earlier days: {len(expired)} "
            f"({_agent_counts(expired)}) — a dispatch run was missed"
        )

    if delivered:
        lines += ["", "Delivered Alerts", "----------------"]
        for signal, demoted_from in delivered:
            lines.append(_line(signal, demoted_from, held_tickers))
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines += ["", f"Pooled {len(pooled)} candidates, 0 alerts"]

    if suppressed:
        ranked = sorted(
            suppressed, key=lambda item: (-competition_score(item[0]), item[0].ticker or "")
        )
        header = f"Top suppressed ({min(len(ranked), MAX_SUPPRESSED_LISTED)} of {len(ranked)})"
        lines += ["", header, "-" * len(header)]
        for signal, demoted_from, reason in ranked[:MAX_SUPPRESSED_LISTED]:
            lines.append(f"{_line(signal, demoted_from, held_tickers)} — {reason}")

    subject = f"Dispatch Digest — {len(delivered)} {alert_label}"
    return subject, "\n".join(lines).strip()


def validate_dispatch(
    *,
    routed: list[RoutedSignal],
    db_counts: dict[str, int],
    body: str,
) -> None:
    """Fail closed if the digest diverges from what the database now holds."""
    expected = Counter(rs for _s, rs, _dmf, _r in routed)
    for status in ("DELIVERED", "SUPPRESSED"):
        if db_counts.get(status, 0) != expected.get(status, 0):
            raise RuntimeError(
                f"Dispatch {status} count mismatch: db={db_counts} routed={dict(expected)}"
            )
    if db_counts.get("CANDIDATE", 0):
        raise RuntimeError(f"Dispatch left rows in CANDIDATE: db={db_counts}")
    delivered = [s for s, rs, _dmf, _r in routed if rs == "DELIVERED"]
    if len(delivered) > BUDGET_INFO:
        raise RuntimeError(f"Dispatch delivered {len(delivered)} > INFO budget {BUDGET_INFO}")
    if not delivered and f"Pooled {len(routed)} candidates, 0 alerts" not in body:
        raise RuntimeError("Dispatch digest missing zero-alert confirmation")
    for signal in delivered:
        if signal.title not in body:
            raise RuntimeError(f"Dispatch digest missing delivered alert {signal.alert_id}")


def run() -> None:
    run_id = repository.insert_run("dispatch")
    try:
        with heartbeat.heartbeat(job="dispatch"):
            now_et = _now_et()
            date_iso = now_et.date().isoformat()

            expired_pairs = repository.list_candidates_before_date(date_iso)
            expired = [s for s, _ in expired_pairs]
            if expired_pairs:
                updated = repository.mark_candidates_routed(
                    [(s.alert_id, "SUPPRESSED", dmf, EXPIRED_REASON) for s, dmf in expired_pairs]
                )
                if updated != len(expired_pairs):
                    raise RuntimeError(
                        f"Expired {updated} of {len(expired_pairs)} stale candidates"
                    )
                logger.warning("dispatch expired %d stale candidates", len(expired))

            candidates = repository.list_candidates_for_date(date_iso)
            routed = dispatch_candidates(candidates)
            updated = repository.mark_candidates_routed(
                [
                    (s.alert_id, "PENDING" if rs == "DELIVERED" else rs, dmf, reason)
                    for s, rs, dmf, reason in routed
                ]
            )
            if updated != len(routed):
                raise RuntimeError(
                    f"Persisted {updated} of {len(routed)} dispatch outcomes "
                    "(rows left CANDIDATE — a concurrent dispatch?)"
                )

            held_tickers = set(get_core_holdings())
            subject, body = render_dispatch_digest(
                routed=routed, expired=expired, held_tickers=held_tickers
            )
            db_counts = repository.count_routing_status_for_alert_ids(
                [s.alert_id for s, _rs, _dmf, _r in routed]
            )
            validate_dispatch(routed=routed, db_counts=db_counts, body=body)

            telegram_sender.send_message(f"{subject}\n\n{body}")
            confirm_delivered_signals(
                [s for s, rs, _dmf, _r in routed if rs == "DELIVERED"]
            )
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
