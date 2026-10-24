"""Alert Router — per-agent AR lanes plus a pooled INFO budget.

Pure logic: reads count_delivered_today() for the relevant lanes, runs slot
competition with deterministic tiebreaks, and returns (signal, routing_status,
demoted_from, suppression_reason) tuples. Does NOT write to DB; the calling job
persists the outcomes.
"""
from __future__ import annotations

import dataclasses
import json
import logging

from signal_system.models import (
    DISCOVERY_AGENT,
    EXIT_SENTINEL_AGENT,
    NEWS_CLASSIFIER_AGENT,
    ROUTING_REGIME,
    Signal,
    competition_score,
)
from signal_system.state import repository

logger = logging.getLogger(__name__)

# Per-agent ACTION_REQUIRED lanes (Plan 12a). News and sentinel each get one
# lane; Discovery is shadow-only, so it has none. BUDGET_AR is the derived
# total, kept public for digest cap-fraction rendering.
BUDGET_AR_BY_AGENT: dict[str, int] = {
    NEWS_CLASSIFIER_AGENT: 1,
    EXIT_SENTINEL_AGENT: 1,
    DISCOVERY_AGENT: 0,
}
BUDGET_AR: int = sum(BUDGET_AR_BY_AGENT.values())

# Shared INFORMATIONAL pool. Sentinel may reserve one slot for a fresh
# drawdown / day-drop trigger (Plan 12a2); below-cost-only breaches compete in
# the normal pool.
BUDGET_INFO: int = 3
SENTINEL_RESERVED_INFO: int = 1

# ROUTING_REGIME is defined in models.py (stdlib-only so repository.py can
# stamp inserts without importing this module) and re-exported here.
__all__ = [
    "BUDGET_AR",
    "BUDGET_AR_BY_AGENT",
    "BUDGET_INFO",
    "ROUTING_REGIME",
    "SENTINEL_RESERVED_INFO",
    "dispatch_candidates",
    "dispatch_rank_key",
    "is_fresh_sentinel",
    "route_signals",
    "select_reserved_sentinel",
    "sentinel_rank_key",
    "sentinel_severity_ratio",
    "sentinel_trigger_magnitudes",
    "stage_signals",
]

RoutedTuple = tuple[Signal, str, "str | None", "str | None"]


def stage_signals(signals: list[Signal]) -> list[RoutedTuple]:
    """Agent-job intake: AR now, INFO deferred to dispatch.

    Each agent's ACTION_REQUIRED signals compete for that agent's own lane
    (news 1, sentinel 1, discovery 0). The winner is DELIVERED; losers are
    demoted to INFORMATIONAL and staged as CANDIDATE carrying
    demoted_from='ACTION_REQUIRED', so a silently downgraded AR stays visible
    all the way through dispatch. Tickerless macro news is capped at
    INFORMATIONAL with demoted_from='macro_cap'.

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

    results: list[RoutedTuple] = []

    # 12c1: tickerless macro news can never be ACTION_REQUIRED. It is not
    # directly actionable, carries no price snapshot, and is invisible to
    # outcome backfill. Demote at the router (no prompt change, no eval gate);
    # demoted_from preserves the audit trail.
    ar_signals: list[Signal] = []
    for sig in signals:
        if (
            sig.agent == NEWS_CLASSIFIER_AGENT
            and sig.severity == "ACTION_REQUIRED"
            and sig.ticker is None
        ):
            demoted = dataclasses.replace(sig, severity="INFORMATIONAL")
            results.append((demoted, "CANDIDATE", "macro_cap", None))
        else:
            if sig.severity == "ACTION_REQUIRED":
                ar_signals.append(sig)

    # Per-agent AR lanes: one lane each for news and sentinel, none for
    # shadow-mode Discovery. A spent news lane no longer starves the sentinel.
    by_agent: dict[str, list[Signal]] = {}
    for sig in ar_signals:
        by_agent.setdefault(sig.agent, []).append(sig)

    for agent, lane in by_agent.items():
        delivered_today = repository.count_delivered_today(agent=agent)
        lane_budget = BUDGET_AR_BY_AGENT.get(agent, 0)
        ar_remaining = max(0, lane_budget - delivered_today.get("ACTION_REQUIRED", 0))
        if agent == EXIT_SENTINEL_AGENT:
            lane = sorted(lane, key=sentinel_rank_key)
        else:
            lane = sorted(
                lane, key=lambda s: (-competition_score(s), s.ticker or "")
            )
        for i, sig in enumerate(lane):
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

# Sentinel severity norms for the unclamped ranking ratio (Plan 12a2). The
# 0-100 competition score pins at 100 for every severe breach, so ranking
# severe sentinels by score degenerates into alphabetical order.
_DRAWDOWN_SEVERE: float = 0.15
_DAY_DROP_SEVERE: float = 0.08
_FRESH_SENTINEL_TRIGGERS: frozenset[str] = frozenset({"drawdown_20d", "day_drop_1d"})


def sentinel_trigger_magnitudes(signal: Signal) -> dict[str, float]:
    """Return a sentinel signal's breached trigger magnitudes, or {}."""
    if signal.agent != EXIT_SENTINEL_AGENT or not signal.trigger_json:
        return {}
    try:
        parsed = json.loads(signal.trigger_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    magnitudes: dict[str, float] = {}
    for name, value in parsed.items():
        try:
            magnitudes[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return magnitudes


def is_fresh_sentinel(signal: Signal) -> bool:
    """True when a sentinel candidate carries a fresh drawdown / day-drop trigger."""
    triggers = sentinel_trigger_magnitudes(signal)
    return any(name in triggers for name in _FRESH_SENTINEL_TRIGGERS)


def sentinel_severity_ratio(signal: Signal) -> float:
    """Unclamped severity ratio: max(drawdown/0.15, day_drop/0.08)."""
    triggers = sentinel_trigger_magnitudes(signal)
    return max(
        triggers.get("drawdown_20d", 0.0) / _DRAWDOWN_SEVERE,
        triggers.get("day_drop_1d", 0.0) / _DAY_DROP_SEVERE,
        0.0,
    )


def sentinel_rank_key(signal: Signal) -> tuple[float, str]:
    """Rank sentinel signals by unclamped severity ratio, then ticker."""
    return (-sentinel_severity_ratio(signal), signal.ticker or "")


def dispatch_rank_key(
    signal: Signal, held_tickers: set[str] | None = None
) -> tuple[int, int, float, str]:
    """Sort key for pooled INFO candidates: agent priority, then score.

    Inside the news band, held tickers rank above non-held tickers, which rank
    above macro (tickerless) headlines (Plan 12c2). Other agents are unchanged.
    """
    held = held_tickers or set()
    band = 0
    if signal.agent == NEWS_CLASSIFIER_AGENT:
        if signal.ticker is None:
            band = 2
        elif signal.ticker in held:
            band = 0
        else:
            band = 1
    return (
        AGENT_PRIORITY.get(signal.agent, len(AGENT_PRIORITY)),
        band,
        -competition_score(signal),
        signal.ticker or "",
    )


def _fresh_sentinel_key(pair: tuple[Signal, str | None]) -> tuple[int, float, str]:
    signal, demoted_from = pair
    return (
        0 if demoted_from == "ACTION_REQUIRED" else 1,
        -sentinel_severity_ratio(signal),
        signal.ticker or "",
    )


def select_reserved_sentinel(
    candidates: list[tuple[Signal, str | None]],
    info_remaining: int,
) -> tuple[Signal, str | None] | None:
    """Return the fresh sentinel pair that takes the reserved INFO slot, or None.

    Pure: this is the exact selection dispatch_candidates() applies, exposed so
    callers can validate and label the reserved slot without re-deriving it from
    the delivered set. A fresh sentinel is one whose trigger_json carries a
    drawdown_20d / day_drop_1d breach, and the slot is only taken when INFO
    budget remains.
    """
    if info_remaining <= 0:
        return None
    fresh = [pair for pair in candidates if is_fresh_sentinel(pair[0])]
    if not fresh:
        return None
    return sorted(fresh, key=_fresh_sentinel_key)[0]


def dispatch_candidates(
    candidates: list[tuple[Signal, str | None]],
    *,
    held_tickers: set[str] | None = None,
) -> list[RoutedTuple]:
    """Pool the day's staged INFO candidates from every agent and route once.

    *candidates* are (signal, stored_demoted_from) pairs read back from the
    database. One fresh sentinel candidate (drawdown / day-drop trigger) is
    delivered first through the reserved slot when INFO budget remains; the
    other slots then use dispatch_rank_key() (agent priority, news held-first,
    then competition_score). The rest are SUPPRESSED as 'outscored', or
    'cap_exhausted' when the INFO budget was already spent before dispatch ran.
    The stored demoted_from travels with every outcome.

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
    ranked = sorted(
        candidates, key=lambda pair: dispatch_rank_key(pair[0], held_tickers)
    )

    reserved = select_reserved_sentinel(candidates, info_remaining)

    ordered: list[tuple[Signal, str | None]] = []
    if reserved is not None:
        ordered.append(reserved)
    ordered.extend(pair for pair in ranked if pair != reserved)

    results: list[RoutedTuple] = []
    for i, (sig, dmf) in enumerate(ordered):
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

    # Read DB budget once — cross-run awareness (D-07). Legacy single-batch
    # routing keeps its historical one-AR cap; only stage_signals() uses the
    # per-agent lanes whose sum is BUDGET_AR.
    delivered = repository.count_delivered_today()
    ar_used = delivered.get("ACTION_REQUIRED", 0)
    info_used = delivered.get("INFORMATIONAL", 0)
    ar_remaining = max(0, 1 - ar_used)
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
