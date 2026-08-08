"""Alert Router — enforces daily budget caps and slot competition.

Pure logic: reads count_delivered_today() once, runs severity-first slot competition
with deterministic tiebreak, returns (signal, routing_status, demoted_from,
suppression_reason) tuples. Does NOT write to DB. Caller (Phase 6 job) handles
insert_signal() and email.
"""
from __future__ import annotations

import dataclasses
import logging

from signal_system.models import Signal, competition_score
from signal_system.state import repository

logger = logging.getLogger(__name__)

# Hard-coded daily budget caps (ROUT-01 / D-01)
BUDGET_AR: int = 1
BUDGET_INFO: int = 3


def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None, str | None]]:
    """Route a batch of signals against today's delivery budget.

    Returns a list of (signal, routing_status, demoted_from, suppression_reason)
    tuples, one per input signal. routing_status is 'DELIVERED' or 'SUPPRESSED'.

    demoted_from records the severity a signal was demoted *from* (currently
    only ACTION_REQUIRED -> INFORMATIONAL). suppression_reason records why a
    signal was ultimately cut: 'outscored' (lost a head-to-head slot
    competition within this batch) or 'cap_exhausted' (the severity's daily
    budget was already full — either from earlier today or after in-batch
    demotion still left no room).

    AR signals always demote before suppressing, including across runs: when
    today's AR budget is already exhausted (by an earlier run), a new AR
    signal is demoted to INFORMATIONAL and competes for the INFO pool rather
    than being suppressed outright. Only if the INFO pool is also full does it
    get suppressed (cap_exhausted) — still carrying demoted_from.

    The two fields answer different questions and are NOT mutually exclusive:
    an ACTION_REQUIRED signal that loses the AR slot is demoted into the INFO
    pool, and if it then also loses the INFO competition it carries both a
    demoted_from of 'ACTION_REQUIRED' and a suppression_reason. Dropping the
    demotion in that case would hide a silent AR -> nothing downgrade.
    suppression_reason remains None for DELIVERED signals.

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
    ar_remaining = max(0, BUDGET_AR - ar_used)
    info_remaining = max(0, BUDGET_INFO - info_used)

    results: list[tuple[Signal, str, str | None, str | None]] = []

    # --- Severity-first slot competition (D-04) ---
    ar_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: (-competition_score(s), s.ticker or ""),
    )
    info_signals = sorted(
        [s for s in signals if s.severity == "INFORMATIONAL"],
        key=lambda s: (-competition_score(s), s.ticker or ""),
    )

    # Track AR signals demoted to INFO so they carry demoted_from="ACTION_REQUIRED"
    demoted_ids: set[str] = set()
    demoted_info: list[Signal] = []

    for i, sig in enumerate(ar_signals):
        if i < ar_remaining:
            results.append((sig, "DELIVERED", None, None))
        else:
            demoted = dataclasses.replace(sig, severity="INFORMATIONAL")
            demoted_ids.add(demoted.alert_id)
            demoted_info.append(demoted)

    # Merge demoted AR signals into the INFO pool for slot competition
    if demoted_info:
        info_signals = sorted(
            info_signals + demoted_info,
            key=lambda s: (-competition_score(s), s.ticker or ""),
        )

    for i, sig in enumerate(info_signals):
        # demoted_from travels with the signal regardless of outcome — a demoted
        # AR signal that then loses the INFO competition must still show that it
        # was once ACTION_REQUIRED, not read as a plain INFORMATIONAL miss.
        dmf = "ACTION_REQUIRED" if sig.alert_id in demoted_ids else None
        if i < info_remaining:
            results.append((sig, "DELIVERED", dmf, None))
        elif info_remaining == 0 and info_used >= BUDGET_INFO:
            results.append((sig, "SUPPRESSED", dmf, "cap_exhausted"))
        else:
            results.append((sig, "SUPPRESSED", dmf, "outscored"))

    return results
