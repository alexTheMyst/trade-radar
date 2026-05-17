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
    """Persist routed tuples and return delivered signals plus normalized status counts."""
    status_counts = {"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0}
    delivered_signals: list[Signal] = []

    for signal, routing_status, demoted_from in routed_signals:
        repository.insert_signal(
            signal,
            routing_status=routing_status,
            demoted_from=demoted_from,
        )
        status_counts[routing_status] = status_counts.get(routing_status, 0) + 1
        if routing_status == "DELIVERED":
            delivered_signals.append(signal)

    return PersistenceSummary(
        delivered_signals=delivered_signals,
        status_counts=status_counts,
    )


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
