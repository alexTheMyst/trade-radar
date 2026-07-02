from __future__ import annotations

from dataclasses import dataclass

from signal_system.models import Signal
from signal_system.state import repository

RoutedSignal = tuple[Signal, str, str | None]


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

    for signal, routing_status, demoted_from in routed_signals:
        db_status = routing_status
        if routing_status == "DELIVERED":
            db_status = "PENDING"
        inserted = repository.insert_signal(
            signal,
            routing_status=db_status,
            demoted_from=demoted_from,
        )
        status_counts[routing_status] = status_counts.get(routing_status, 0) + 1
        if routing_status == "DELIVERED" and inserted:
            pending_signals.append(signal)

    return PersistenceSummary(
        delivered_signals=pending_signals,
        status_counts=status_counts,
    )


def confirm_delivered_signals(signals: list[Signal]) -> None:
    """Flip PENDING → DELIVERED for successfully sent signals."""
    for sig in signals:
        repository.update_routing_status(sig.alert_id, "DELIVERED")


def render_digest(
    *,
    job_name: str,
    scanned_tickers: int,
    delivered_signals: list[Signal],
    status_counts: dict[str, int],
) -> DigestPayload:
    """Render the plain-text digest required by the live jobs."""
    delivered_count = status_counts.get("DELIVERED", len(delivered_signals))
    suppressed_count = status_counts.get("SUPPRESSED", 0)
    monitoring_count = status_counts.get("MONITORING", 0)
    title = job_name.replace("-", " ").title()
    alert_label = "alert" if delivered_count == 1 else "alerts"

    lines = [
        f"Scanned {scanned_tickers} tickers, {delivered_count} {alert_label}",
        f"Suppressed: {suppressed_count}",
        f"Monitoring: {monitoring_count}",
    ]

    if delivered_signals:
        lines.append("")
        lines.append("Delivered Alerts")
        lines.append("----------------")
        for signal in delivered_signals:
            lines.append(f"{signal.ticker or 'UNKNOWN'} — {signal.severity}")
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines.append("")
        lines.append(f"Scanned {scanned_tickers} tickers, 0 alerts")

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
