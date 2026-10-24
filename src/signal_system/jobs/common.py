from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from signal_system import config
from signal_system.classifier.news_classifier import NEWS_CLASSIFIER_AGENT
from signal_system.delivery import telegram_sender
from signal_system.models import (
    DISCOVERY_AGENT,
    EXIT_SENTINEL_AGENT,
    Signal,
    competition_score,
)
from signal_system.router import BUDGET_AR, BUDGET_INFO, ROUTING_REGIME
from signal_system.state import repository

logger = logging.getLogger(__name__)

RoutedSignal = tuple[Signal, str, str | None, str | None]


@dataclass(frozen=True, slots=True)
class PersistenceSummary:
    delivered_signals: list[Signal]
    status_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class DigestPayload:
    subject: str
    body: str
    status_counts: dict[str, int]
    monitoring_breakdown: dict[str, int] | None = None
    monitoring_deduped: int = 0
    staged_count: int = 0


def persist_routed_signals(routed_signals: list[RoutedSignal]) -> PersistenceSummary:
    """Persist routed tuples and return to-be-delivered signals plus normalized counts.

    Signals that should be DELIVERED are persisted with routing_status='PENDING'
    so a failed Telegram send does not leave phantom DELIVERED rows in the DB.
    Caller must call confirm_delivered_signals() after a successful send to flip
    PENDING → DELIVERED.

    CANDIDATE rows (INFO signals staged for the evening dispatch) are written
    as-is and counted under "CANDIDATE". Every routed row is stamped with the
    current ROUTING_REGIME.
    """
    status_counts = {"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0}
    pending_signals: list[Signal] = []

    for signal, routing_status, demoted_from, suppression_reason in routed_signals:
        db_status = routing_status
        if routing_status == "DELIVERED":
            db_status = "PENDING"
        inserted = repository.insert_signal(
            signal,
            routing_status=db_status,
            demoted_from=demoted_from,
            suppression_reason=suppression_reason,
            routing_regime=ROUTING_REGIME,
        )
        status_counts[routing_status] = status_counts.get(routing_status, 0) + 1
        if routing_status == "DELIVERED" and inserted:
            pending_signals.append(signal)

    return PersistenceSummary(
        delivered_signals=pending_signals,
        status_counts=status_counts,
    )


def format_score(agent: str, score: float | None) -> str:
    """Render a score on its emitting agent's native scale.

    Discovery's composite score is 0-100 and renders as a bare integer.
    The News Classifier's score is `confidence`, constrained to [0.0, 1.0] —
    a bare `:.0f` would collapse every value to "0" or "1", so it renders as
    a percentage instead.
    """
    if score is None:
        return "N/A"
    if agent == NEWS_CLASSIFIER_AGENT:
        return f"{score * 100:.0f}%"
    return f"{score:.0f}"


def format_score_delta(agent: str, delta: float) -> str:
    """Render a score delta on its emitting agent's native scale.

    Mirrors format_score(): 0-100 scales render as signed integers, News
    Classifier confidences render as signed percentage points ("pp").
    """
    if agent == NEWS_CLASSIFIER_AGENT:
        return f"{delta * 100:+.0f}pp"
    return f"{delta:+.0f}"


def compute_score_deltas(routed_signals: list[RoutedSignal]) -> dict[str, str]:
    """Return {alert_id: '+7' | '-3pp' | 'NEW'} vs each ticker+agent's prior scan.

    "Prior scan" is the most recent signal for the same (ticker, agent) pair
    from an *earlier ET calendar date*, regardless of gap — Discovery only
    rescans non-core tickers every ~3 days, so a strict "yesterday" comparison
    would show NEW most of the time.

    MUST be called before persist_routed_signals(): persisting first would let
    get_previous_score() see this same run's own rows.
    """
    deltas: dict[str, str] = {}
    for signal, _routing_status, _demoted_from, _reason in routed_signals:
        if signal.score is None or not signal.ticker:
            continue
        previous = repository.get_previous_score(signal.ticker, signal.agent, signal.timestamp)
        if previous is None:
            deltas[signal.alert_id] = "NEW"
        else:
            deltas[signal.alert_id] = format_score_delta(signal.agent, signal.score - previous)
    return deltas


def confirm_delivered_signals(signals: list[Signal]) -> None:
    """Flip PENDING → DELIVERED for successfully sent signals."""
    for sig in signals:
        repository.update_routing_status(sig.alert_id, "DELIVERED")


def send_alert_buttons(
    signals: list[Signal], *, held_tickers: set[str] | None = None
) -> None:
    """Send per-alert Telegram action buttons after a successful digest send.

    A button failure must never fail the job: the alert itself was already
    delivered in the digest. It is recorded as ``send_failed`` so the watchdog
    can surface it (Plan 12e1).
    """
    held = held_tickers or set()
    for signal in signals:
        try:
            message_id = telegram_sender.send_alert_with_buttons(
                signal, held=(signal.ticker in held) if signal.ticker else False
            )
        except Exception:
            logger.warning(
                "Telegram button message failed for %s", signal.alert_id[:12],
                exc_info=True,
            )
            repository.insert_telegram_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                message_id=None,
                alert_id=signal.alert_id,
                kind="alert",
                status="send_failed",
            )
        else:
            repository.insert_telegram_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                message_id=message_id,
                alert_id=signal.alert_id,
                kind="alert",
                status="sent" if message_id is not None else "send_failed",
            )


# --- Unconfirmed-send sweep -------------------------------------------------
# A PENDING row is a delivered alert whose Telegram send was never confirmed.
# No job's rerun re-sends its own PENDING rows, so telegram-poll (every 15 min,
# 07:00-23:00 ET) is the single owner of re-sending them. One owner means two
# sweepers can never race the same row.

# Rows younger than this are re-sent; older ones are closed out as
# send_failed — a days-old alert re-sent as if fresh would mislead.
RESEND_WINDOW = timedelta(days=3)

# A 'running' row older than this is a crashed job, not one still sending
# (every producing job finishes in minutes).
IN_FLIGHT_MAX_AGE = timedelta(hours=2)

# Job that persists each agent's ACTION_REQUIRED lane winners as PENDING.
# INFORMATIONAL PENDING rows only ever come from dispatch.
_AR_PRODUCING_JOB: dict[str, str] = {
    NEWS_CLASSIFIER_AGENT: "news-morning",
    EXIT_SENTINEL_AGENT: "sentinel",
    DISCOVERY_AGENT: "discovery",
}


@dataclass
class ResendResult:
    resent: list[Signal] = field(default_factory=list)
    skipped_in_flight: list[Signal] = field(default_factory=list)
    closed_out: list[Signal] = field(default_factory=list)


def _producing_job(signal: Signal) -> str | None:
    if signal.severity == "INFORMATIONAL":
        return "dispatch"
    return _AR_PRODUCING_JOB.get(signal.agent)


def _job_in_flight(job: str, now_et: datetime) -> bool:
    latest = repository.get_latest_run(job)
    return (
        latest is not None
        and latest["status"] == "running"
        and now_et - latest["started_at"] < IN_FLIGHT_MAX_AGE
    )


def _closed_out_notice(signals: list[Signal]) -> str:
    lines = [
        f"⚠️ {len(signals)} alert(s) could not be delivered for "
        f"{RESEND_WINDOW.days}+ days and were closed out as send_failed:"
    ]
    for signal in signals:
        lines.append(
            f"{signal.ticker or 'MACRO'} · {signal.agent} · {signal.severity} · "
            f"raised {signal.timestamp:%Y-%m-%d %H:%M} ET — {signal.title}"
        )
    return "\n".join(lines)


def resend_unconfirmed(
    now_et: datetime, *, held_tickers: set[str] | None = None
) -> ResendResult:
    """Re-send stuck PENDING alerts and close out the ones past the window.

    Rows inside RESEND_WINDOW are re-sent as button messages labelled with when
    they were raised, then flipped to DELIVERED. A row is skipped while its
    producing job has a run in flight — that run may be between persist and
    confirm, and re-sending would duplicate the alert; the next sweep gets it.

    Rows past the window are announced in one notice and then closed out as
    SUPPRESSED/'send_failed'. The notice goes first: if Telegram is still down,
    the rows stay PENDING and the next sweep tries again.

    A send failure propagates (after the sender's own transient retries): an
    unreachable Telegram must fail the run so the heartbeat pages through an
    independent channel. Rows confirmed before the failure stay confirmed.
    """
    held = held_tickers or set()
    result = ResendResult()
    cutoff = now_et - RESEND_WINDOW

    expired = [s for s, _dmf in repository.list_unconfirmed_before(cutoff)]
    if expired:
        telegram_sender.send_message(_closed_out_notice(expired))
        repository.mark_unconfirmed_send_failed([s.alert_id for s in expired])
        result.closed_out = expired
        logger.warning("closed out %d unconfirmed alert(s) as send_failed", len(expired))

    in_flight: dict[str, bool] = {}
    for signal, _dmf in repository.list_unconfirmed_since(cutoff):
        job = _producing_job(signal)
        if job is not None:
            if job not in in_flight:
                in_flight[job] = _job_in_flight(job, now_et)
            if in_flight[job]:
                logger.info(
                    "not re-sending PENDING %s %s yet: %s is running",
                    signal.agent,
                    signal.alert_id[:12],
                    job,
                )
                result.skipped_in_flight.append(signal)
                continue
        message_id = telegram_sender.send_alert_with_buttons(
            signal,
            held=(signal.ticker in held) if signal.ticker else False,
            prefix=f"Re-sent (raised {signal.timestamp:%a %m-%d %H:%M} ET) · ",
        )
        if repository.confirm_unconfirmed_delivered(signal.alert_id):
            repository.insert_telegram_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                message_id=message_id,
                alert_id=signal.alert_id,
                kind="alert",
                status="sent" if message_id is not None else "send_failed",
            )
            result.resent.append(signal)
    if result.resent:
        logger.warning("re-sent %d unconfirmed alert(s)", len(result.resent))
    return result


def render_digest(
    *,
    job_name: str,
    scanned_tickers: int,
    routed_signals: list[RoutedSignal],
    held_tickers: set[str],
    score_deltas: dict[str, str],
    status_counts: dict[str, int],
    monitoring_breakdown: dict[str, int] | None = None,
    monitoring_deduped: int = 0,
) -> DigestPayload:
    """Render the plain-text digest required by the live jobs."""
    delivered = [
        (s, dmf) for s, rs, dmf, _reason in routed_signals if rs == "DELIVERED"
    ]
    suppressed = [
        (s, dmf, reason) for s, rs, dmf, reason in routed_signals if rs == "SUPPRESSED"
    ]
    staged = [(s, dmf) for s, rs, dmf, _reason in routed_signals if rs == "CANDIDATE"]
    ar_winner = next(
        (s for s, _dmf in delivered if s.severity == "ACTION_REQUIRED"), None
    )

    delivered_count = status_counts.get("DELIVERED", len(delivered))
    suppressed_count = status_counts.get("SUPPRESSED", len(suppressed))
    monitoring_count = status_counts.get("MONITORING", 0)
    staged_count = status_counts.get("CANDIDATE", len(staged))
    title = job_name.replace("-", " ").title()
    alert_label = "alert" if delivered_count == 1 else "alerts"

    lines = [
        f"Scanned {scanned_tickers} tickers, {delivered_count} {alert_label}",
        f"Suppressed: {suppressed_count}",
        _monitoring_line(monitoring_count, monitoring_breakdown),
    ]
    if monitoring_deduped > 0:
        lines.append(f"Deduped on write: {monitoring_deduped}")
    if staged_count > 0:
        lines.append(f"Staged for evening dispatch: {staged_count}")

    if delivered:
        lines.append("")
        lines.append("Delivered Alerts")
        lines.append("----------------")
        for signal, demoted_from in delivered:
            lines.append(_delivered_header_line(signal, demoted_from, ar_winner, held_tickers))
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines.append("")
        lines.append(f"Scanned {scanned_tickers} tickers, 0 alerts")

    if suppressed:
        header = f"Suppressed ({len(suppressed)})"
        lines.append("")
        lines.append(header)
        lines.append("-" * len(header))
        for signal, demoted_from, reason in sorted(
            suppressed, key=lambda item: -(item[0].score or 0.0)
        ):
            lines.append(
                _suppressed_line(signal, demoted_from, reason, held_tickers, score_deltas)
            )

    if staged:
        header = f"Staged for evening dispatch ({len(staged)})"
        lines.append("")
        lines.append(header)
        lines.append("-" * len(header))
        for signal, demoted_from in sorted(
            staged, key=lambda item: (-competition_score(item[0]), item[0].ticker or "")
        ):
            lines.append(_staged_line(signal, demoted_from, held_tickers, score_deltas))

    subject = f"{title} Digest — {delivered_count} {alert_label}"
    return DigestPayload(
        subject=subject,
        body="\n".join(lines).strip(),
        status_counts={
            "DELIVERED": delivered_count,
            "SUPPRESSED": suppressed_count,
            "MONITORING": monitoring_count,
        },
        monitoring_breakdown=(
            {k: v for k, v in monitoring_breakdown.items() if v > 0}
            if monitoring_breakdown
            else None
        ),
        monitoring_deduped=monitoring_deduped,
        staged_count=staged_count,
    )


def _staged_line(
    signal: Signal,
    demoted_from: str | None,
    held_tickers: set[str],
    score_deltas: dict[str, str],
) -> str:
    ticker = signal.ticker or "MACRO"
    score_str = format_score(signal.agent, signal.score)
    delta = score_deltas.get(signal.alert_id)
    if delta:
        score_str += f" ({delta})"
    severity_display = signal.severity
    if demoted_from is not None:
        severity_display += f" (demoted from {demoted_from})"
    line = f"{ticker} — {score_str} — {severity_display}"
    if ticker in held_tickers:
        line += " — HELD"
    return line


_MONITORING_CATEGORY_LABELS: tuple[tuple[str, str], ...] = (
    ("off_thesis", "off-thesis"),
    ("volume_cap", "volume-cap"),
    ("below_threshold", "below-threshold"),
    ("parse_failure", "parse-fail"),
    ("reconciled", "reconciled"),
    ("cooldown", "cooldown"),
    ("repeat", "repeat"),
)


def _monitoring_line(
    monitoring_count: int, breakdown: dict[str, int] | None
) -> str:
    """Render the Monitoring count line, with per-category breakdown when provided.

    Zero categories are omitted; an empty/None breakdown renders the bare count.
    The category labels are the title-prefix taxonomy used across the jobs
    ([off_thesis], [parse_failure], [volume_cap], [cooldown]) plus reconciled
    losers.
    """
    line = f"Monitoring: {monitoring_count}"
    if not breakdown:
        return line
    parts = [
        f"{label} {breakdown.get(key, 0)}"
        for key, label in _MONITORING_CATEGORY_LABELS
        if breakdown.get(key, 0) > 0
    ]
    if not parts:
        return line
    return f"{line} ({', '.join(parts)})"


def _delivered_header_line(
    signal: Signal,
    demoted_from: str | None,
    ar_winner: Signal | None,
    held_tickers: set[str],
) -> str:
    header = f"{signal.ticker or 'MACRO'} — {signal.severity}"
    if demoted_from is not None:
        if ar_winner is not None and ar_winner.alert_id != signal.alert_id:
            # Reachable with News Classifier confidences: a batch is never mixed
            # across agents, but news-morning can route two ACTION_REQUIRED
            # news_classifier signals in one run — one wins AR, the other is
            # delivered demoted and names the winner's score here.
            winner_score = format_score(ar_winner.agent, ar_winner.score)
            header += (
                f" (demoted from {demoted_from}, outscored by "
                f"{ar_winner.ticker or 'MACRO'} {winner_score})"
            )
        else:
            header += f" (demoted from {demoted_from})"
    if (signal.ticker or "") in held_tickers:
        header += " — HELD"
    return header


def _suppressed_line(
    signal: Signal,
    demoted_from: str | None,
    reason: str | None,
    held_tickers: set[str],
    score_deltas: dict[str, str],
) -> str:
    ticker = signal.ticker or "MACRO"
    score_str = format_score(signal.agent, signal.score)
    delta = score_deltas.get(signal.alert_id)
    if delta:
        score_str += f" ({delta})"

    # A signal can be BOTH demoted and suppressed: an ACTION_REQUIRED signal that
    # lost the AR slot, then also lost the INFO competition. Surfacing the
    # demotion here is the whole point — otherwise a silently-downgraded AR
    # alert on a held position reads as an ordinary INFORMATIONAL miss.
    severity_display = signal.severity
    if demoted_from is not None:
        severity_display += f" (demoted from {demoted_from})"

    if reason == "cap_exhausted":
        cap_label = "AR" if signal.severity == "ACTION_REQUIRED" else "INFO"
        cap_value = BUDGET_AR if signal.severity == "ACTION_REQUIRED" else BUDGET_INFO
        reason_display = f"cap_exhausted ({cap_label} {cap_value}/{cap_value})"
    else:
        reason_display = reason or "unknown"

    line = f"{ticker} — {score_str} — {severity_display} — {reason_display}"
    if ticker in held_tickers:
        line += " — HELD"
    return line


def validate_digest_payload(
    payload: DigestPayload,
    *,
    scanned_tickers: int,
    db_counts: dict[str, int],
    delivered_signals: list[Signal],
    db_monitoring_breakdown: dict[str, int] | None = None,
) -> None:
    """Fail closed if digest content diverges from the persisted database state.

    *db_counts* and *db_monitoring_breakdown* come from repository reads taken
    after persist_routed_signals() and before the digest is sent — the database
    is the source of truth, never the in-memory counters that rendered it. A
    digest that undercounts is a lie; a job that refuses to send is a page.
    """
    normalized_db = {
        "DELIVERED": db_counts.get("DELIVERED", 0),
        "SUPPRESSED": db_counts.get("SUPPRESSED", 0),
        "MONITORING": db_counts.get("MONITORING", 0),
    }
    if payload.status_counts != normalized_db:
        raise RuntimeError(
            "Digest counts do not match persisted routing outcomes: "
            f"db={normalized_db} got={payload.status_counts}"
        )

    db_staged = db_counts.get("CANDIDATE", 0)
    if payload.staged_count != db_staged:
        raise RuntimeError(
            "Digest staged count does not match persisted CANDIDATE rows: "
            f"db={db_staged} got={payload.staged_count}"
        )

    db_breakdown = {k: v for k, v in (db_monitoring_breakdown or {}).items() if v > 0}
    payload_breakdown = payload.monitoring_breakdown or {}
    if payload_breakdown != db_breakdown:
        raise RuntimeError(
            "Digest monitoring breakdown does not match the database: "
            f"db={db_breakdown} got={payload_breakdown}"
        )

    if sum(payload_breakdown.values()) != normalized_db["MONITORING"]:
        raise RuntimeError(
            "Digest monitoring breakdown does not sum to the monitoring total: "
            f"breakdown={payload_breakdown} total={normalized_db['MONITORING']}"
        )

    if not payload.subject.strip() or not payload.body.strip():
        raise RuntimeError("Digest subject/body must not be empty")

    zero_alert_line = f"Scanned {scanned_tickers} tickers, 0 alerts"
    if normalized_db["DELIVERED"] == 0 and zero_alert_line not in payload.body:
        raise RuntimeError("Digest missing zero-alert confirmation")

    for signal in delivered_signals:
        if signal.title not in payload.body:
            raise RuntimeError(f"Digest missing delivered alert detail for {signal.alert_id}")
