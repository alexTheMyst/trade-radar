from __future__ import annotations

from dataclasses import dataclass

from signal_system.classifier.news_classifier import NEWS_CLASSIFIER_AGENT
from signal_system.models import Signal
from signal_system.router import BUDGET_AR, BUDGET_INFO
from signal_system.state import repository

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


def persist_routed_signals(routed_signals: list[RoutedSignal]) -> PersistenceSummary:
    """Persist routed tuples and return to-be-delivered signals plus normalized counts.

    Signals that should be DELIVERED are persisted with routing_status='PENDING'
    so a failed Telegram send does not leave phantom DELIVERED rows in the DB.
    Caller must call confirm_delivered_signals() after a successful send to flip
    PENDING → DELIVERED.
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


def render_digest(
    *,
    job_name: str,
    scanned_tickers: int,
    routed_signals: list[RoutedSignal],
    held_tickers: set[str],
    score_deltas: dict[str, str],
    status_counts: dict[str, int],
    monitoring_breakdown: dict[str, int] | None = None,
) -> DigestPayload:
    """Render the plain-text digest required by the live jobs."""
    delivered = [
        (s, dmf) for s, rs, dmf, _reason in routed_signals if rs == "DELIVERED"
    ]
    suppressed = [
        (s, dmf, reason) for s, rs, dmf, reason in routed_signals if rs == "SUPPRESSED"
    ]
    ar_winner = next(
        (s for s, _dmf in delivered if s.severity == "ACTION_REQUIRED"), None
    )

    delivered_count = status_counts.get("DELIVERED", len(delivered))
    suppressed_count = status_counts.get("SUPPRESSED", len(suppressed))
    monitoring_count = status_counts.get("MONITORING", 0)
    title = job_name.replace("-", " ").title()
    alert_label = "alert" if delivered_count == 1 else "alerts"

    lines = [
        f"Scanned {scanned_tickers} tickers, {delivered_count} {alert_label}",
        f"Suppressed: {suppressed_count}",
        _monitoring_line(monitoring_count, monitoring_breakdown),
    ]

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

    subject = f"{title} Digest — {delivered_count} {alert_label}"
    return DigestPayload(
        subject=subject,
        body="\n".join(lines).strip(),
        status_counts={
            "DELIVERED": delivered_count,
            "SUPPRESSED": suppressed_count,
            "MONITORING": monitoring_count,
        },
    )


_MONITORING_CATEGORY_LABELS: tuple[tuple[str, str], ...] = (
    ("off_thesis", "off-thesis"),
    ("parse_failure", "parse-fail"),
    ("volume_cap", "volume-cap"),
    ("reconciled", "reconciled"),
)


def _monitoring_line(
    monitoring_count: int, breakdown: dict[str, int] | None
) -> str:
    """Render the Monitoring count line, with per-category breakdown when provided.

    Zero categories are omitted; an empty/None breakdown renders the bare count.
    The category labels are the title-prefix taxonomy used across the jobs
    ([off_thesis], [parse_failure], [volume_cap]) plus reconciled losers.
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
    header = f"{signal.ticker or 'UNKNOWN'} — {signal.severity}"
    if demoted_from is not None:
        if ar_winner is not None and ar_winner.alert_id != signal.alert_id:
            # Reachable with News Classifier confidences: a batch is never mixed
            # across agents, but news-morning can route two ACTION_REQUIRED
            # news_classifier signals in one run — one wins AR, the other is
            # delivered demoted and names the winner's score here.
            winner_score = format_score(ar_winner.agent, ar_winner.score)
            header += (
                f" (demoted from {demoted_from}, outscored by "
                f"{ar_winner.ticker or 'UNKNOWN'} {winner_score})"
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
    ticker = signal.ticker or "UNKNOWN"
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
    expected_counts: dict[str, int],
    delivered_signals: list[Signal],
) -> None:
    """Fail closed if digest content diverges from the persisted run outcomes."""
    normalized_expected = {
        "DELIVERED": expected_counts.get("DELIVERED", 0),
        "SUPPRESSED": expected_counts.get("SUPPRESSED", 0),
        "MONITORING": expected_counts.get("MONITORING", 0),
    }
    if payload.status_counts != normalized_expected:
        raise RuntimeError(
            "Digest counts do not match persisted routing outcomes: "
            f"expected={normalized_expected} got={payload.status_counts}"
        )

    if not payload.subject.strip() or not payload.body.strip():
        raise RuntimeError("Digest subject/body must not be empty")

    zero_alert_line = f"Scanned {scanned_tickers} tickers, 0 alerts"
    if normalized_expected["DELIVERED"] == 0 and zero_alert_line not in payload.body:
        raise RuntimeError("Digest missing zero-alert confirmation")

    for signal in delivered_signals:
        if signal.title not in payload.body:
            raise RuntimeError(f"Digest missing delivered alert detail for {signal.alert_id}")
