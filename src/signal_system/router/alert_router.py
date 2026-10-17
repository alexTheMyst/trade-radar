"""Alert Router — enforces daily budget caps and slot competition.

Pure logic: reads count_delivered_today() once, runs severity-first slot competition
with deterministic tiebreak, returns (signal, routing_status, demoted_from,
suppression_reason) tuples. Does NOT write to DB. Caller (Phase 6 job) handles
insert_signal() and email.
"""
from __future__ import annotations

import dataclasses
import logging

from signal_system.models import (
    DISCOVERY_AGENT,
    EXIT_SENTINEL_AGENT,
    NEWS_CLASSIFIER_AGENT,
    Signal,
    competition_score,
)
from signal_system.state import repository

logger = logging.getLogger(__name__)

# Hard-coded daily budget caps (ROUT-01 / D-01)
BUDGET_AR: int = 1
BUDGET_INFO: int = 3

# Routing-regime stamp written to signals.routing_regime (Plan 06, Option D).
# NULL = legacy first-come routing; never backfilled, so pre- and post-change
# rows are never aggregated together (same convention as weight_version).
ROUTING_REGIME: str = "deferred-info-priority-v1"

RoutedTuple = tuple[Signal, str, "str | None", "str | None"]


def stage_signals(signals: list[Signal]) -> list[RoutedTuple]:
    """Agent-job intake under Option D: AR now, INFO deferred to dispatch.

    ACTION_REQUIRED signals compete for today's remaining AR slot immediately
    (an AR is by definition the thing that must not wait). The winner is
    returned DELIVERED. Losers are demoted to INFORMATIONAL and staged as
    CANDIDATE carrying demoted_from='ACTION_REQUIRED', so a silently
    downgraded AR stays visible all the way through dispatch.

    INFORMATIONAL signals are never routed here: they are returned CANDIDATE
    and compete once, across every agent, in the evening `dispatch` job. That
    is what makes the delivered set a function of score instead of job start
    time.

    Returns (signal, routing_status, demoted_from, suppression_reason) tuples
    where routing_status is 'DELIVERED' or 'CANDIDATE'. Pure: reads the AR
    budget, never writes.
    """
    for sig in signals:
        if sig.severity == "MONITORING":
            raise ValueError(
                f"MONITORING signals bypass the router; got ticker={sig.ticker!r}"
            )

    if not signals:
        return []

    delivered = repository.count_delivered_today()
    ar_remaining = max(0, BUDGET_AR - delivered.get("ACTION_REQUIRED", 0))

    ar_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: (-competition_score(s), s.ticker or ""),
    )
    results: list[RoutedTuple] = []
    for i, sig in enumerate(ar_signals):
        if i < ar_remaining:
            results.append((sig, "DELIVERED", None, None))
        else:
            demoted = dataclasses.replace(sig, severity="INFORMATIONAL")
            results.append((demoted, "CANDIDATE", "ACTION_REQUIRED", None))

    for sig in signals:
        if sig.severity == "INFORMATIONAL":
            results.append((sig, "CANDIDATE", None, None))

    return results


# Dispatch ranking (operator decision 2026-10-16, after a 42-day replay):
# pooled INFO candidates rank by agent first, score second. Pure
# competition_score() ranking let Discovery (scores not predictive) and the
# Exit Sentinel (saturates at 100 on long-standing losers) crowd out the News
# Classifier and delivered worse 30-day outcomes. Unknown agents rank last.
AGENT_PRIORITY: dict[str, int] = {
    NEWS_CLASSIFIER_AGENT: 0,
    EXIT_SENTINEL_AGENT: 1,
    DISCOVERY_AGENT: 2,
}


def dispatch_rank_key(signal: Signal) -> tuple[int, float, str]:
    """Sort key for pooled INFO candidates: agent priority, then score, then ticker."""
    return (
        AGENT_PRIORITY.get(signal.agent, len(AGENT_PRIORITY)),
        -competition_score(signal),
        signal.ticker or "",
    )


def dispatch_candidates(
    candidates: list[tuple[Signal, str | None]],
) -> list[RoutedTuple]:
    """Pool the day's staged INFO candidates from every agent and route once.

    *candidates* are (signal, stored_demoted_from) pairs read back from the
    database. They are ranked by dispatch_rank_key() (agent priority, then
    competition_score) and the top slots of today's remaining INFO budget are
    DELIVERED; the rest are SUPPRESSED as 'outscored', or 'cap_exhausted' when
    the INFO budget was already spent before dispatch ran (e.g. the legacy
    path delivered INFO earlier on a transition day). The stored demoted_from
    travels with every outcome.

    Raises ValueError if a candidate is not INFORMATIONAL: staging demotes
    every non-winning AR, so an AR candidate means the staging invariant broke.
    """
    for sig, _ in candidates:
        if sig.severity != "INFORMATIONAL":
            raise ValueError(
                f"dispatch candidates must be INFORMATIONAL; got {sig.severity} "
                f"for ticker={sig.ticker!r}"
            )
    if not candidates:
        return []

    info_used = repository.count_delivered_today().get("INFORMATIONAL", 0)
    info_remaining = max(0, BUDGET_INFO - info_used)
    ranked = sorted(candidates, key=lambda pair: dispatch_rank_key(pair[0]))

    results: list[RoutedTuple] = []
    for i, (sig, dmf) in enumerate(ranked):
        if i < info_remaining:
            results.append((sig, "DELIVERED", dmf, None))
        elif info_remaining == 0:
            results.append((sig, "SUPPRESSED", dmf, "cap_exhausted"))
        else:
            results.append((sig, "SUPPRESSED", dmf, "outscored"))
    return results


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
