"""Alert Router — enforces daily budget caps and slot competition.

Pure logic: reads count_delivered_today() once, runs severity-first slot competition
with deterministic tiebreak, returns (signal, routing_status, demoted_from) tuples.
Does NOT write to DB. Caller (Phase 6 job) handles insert_signal() and email.
"""
from __future__ import annotations

import dataclasses
import logging

from signal_system.models import Signal
from signal_system.state import repository

logger = logging.getLogger(__name__)

# Hard-coded daily budget caps (ROUT-01 / D-01)
_BUDGET_AR: int = 1
_BUDGET_INFO: int = 3


def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:
    """Route a batch of signals against today's delivery budget.

    Returns a list of (signal, routing_status, demoted_from) tuples,
    one per input signal. routing_status is 'DELIVERED' or 'SUPPRESSED'.
    demoted_from is None for DELIVERED signals, unless they were demoted
    from ACTION_REQUIRED to INFORMATIONAL (then it is 'ACTION_REQUIRED').

    Outscored ACTION_REQUIRED signals are demoted to INFORMATIONAL and
    compete for INFO slots, consistent with the core design: "others demote."
    """
    # Guard: MONITORING signals must never enter the router (D-15)
    for sig in signals:
        if sig.severity == "MONITORING":
            raise ValueError(
                f"MONITORING signals bypass the router; got ticker={sig.ticker!r}"
            )

    if not signals:
        return []

    # Read DB budget once — cross-run awareness (D-07)
    delivered = repository.count_delivered_today()
    ar_used = delivered.get("ACTION_REQUIRED", 0)
    info_used = delivered.get("INFORMATIONAL", 0)
    ar_remaining = max(0, _BUDGET_AR - ar_used)
    info_remaining = max(0, _BUDGET_INFO - info_used)

    results: list[tuple[Signal, str, str | None]] = []

    # --- Severity-first slot competition (D-04) ---
    ar_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )
    info_signals = sorted(
        [s for s in signals if s.severity == "INFORMATIONAL"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )

    # Track AR signals demoted to INFO so they carry demoted_from="ACTION_REQUIRED"
    demoted_ids: set[str] = set()
    demoted_info: list[Signal] = []

    for i, sig in enumerate(ar_signals):
        if i < ar_remaining:
            results.append((sig, "DELIVERED", None))
        elif ar_remaining == 0 and ar_used >= _BUDGET_AR:
            results.append((sig, "SUPPRESSED", "budget_cap_ar"))
        else:
            demoted = dataclasses.replace(sig, severity="INFORMATIONAL")
            demoted_ids.add(demoted.alert_id)
            demoted_info.append(demoted)

    # Merge demoted AR signals into the INFO pool for slot competition
    if demoted_info:
        info_signals = sorted(
            info_signals + demoted_info,
            key=lambda s: (-(s.score or 0.0), s.ticker or ""),
        )

    for i, sig in enumerate(info_signals):
        is_demoted = sig.alert_id in demoted_ids
        if i < info_remaining:
            dmf = "ACTION_REQUIRED" if is_demoted else None
            results.append((sig, "DELIVERED", dmf))
        elif info_remaining == 0 and info_used >= _BUDGET_INFO:
            results.append((sig, "SUPPRESSED", "budget_cap_info"))
        else:
            results.append((sig, "SUPPRESSED", "outscored"))

    return results
