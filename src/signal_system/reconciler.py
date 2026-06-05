"""Direction reconciliation — resolves same-pillar conflicting signals before routing.

Pure function: no DB access, no side effects. Caller persists losers.

Grouping key: (ticker, pillar). Different pillars on the same ticker are independent
theses and both survive. Only same-pillar contradictions (positive vs negative) are
resolved.

Netting policy: highest-confidence wins. If the best opposing-direction signal is
within MARGIN_GUARD of the winner's score, the winner is downgraded one severity band
(ACTION_REQUIRED -> INFORMATIONAL -> MONITORING) so a contested call cannot claim the
scarce daily ACTION_REQUIRED slot.

Signals with pillar=None or direction=None/neutral are passed through as-is (no
reconciliation needed — they have no direction to conflict with).
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict

from signal_system.models import Signal, Severity

logger = logging.getLogger(__name__)

MARGIN_GUARD: float = 0.10

_SEVERITY_DOWNGRADE: dict[Severity, Severity] = {
    "ACTION_REQUIRED": "INFORMATIONAL",
    "INFORMATIONAL": "MONITORING",
    "MONITORING": "MONITORING",  # already floor
}


def reconcile_directions(
    routable: list[Signal],
) -> tuple[list[Signal], list[Signal]]:
    """Resolve same-pillar direction conflicts within a batch of routable signals.

    Args:
        routable: Signals that passed classification (severity != MONITORING).
                  Discovery signals (direction=None) pass through untouched.

    Returns:
        (winners, losers) where:
        - winners: signals to forward to route_signals (may have margin-guard
          severity downgrade applied via dataclasses.replace)
        - losers: signals dropped by reconciliation; caller persists as MONITORING
          with demoted_from='reconciled'
    """
    if not routable:
        return [], []

    # Partition: reconcilable = news signals with a ticker, pillar, and a directional
    # opinion. Everything else passes through without inspection.
    reconcilable: list[Signal] = []
    passthrough: list[Signal] = []
    for sig in routable:
        if (
            sig.ticker is not None
            and sig.pillar is not None
            and sig.direction in ("positive", "negative")
        ):
            reconcilable.append(sig)
        else:
            passthrough.append(sig)

    if not reconcilable:
        return list(routable), []

    # Group by (ticker, pillar)
    groups: dict[tuple[str, str], list[Signal]] = defaultdict(list)
    for sig in reconcilable:
        groups[(sig.ticker, sig.pillar)].append(sig)  # type: ignore[index]

    winners: list[Signal] = list(passthrough)
    losers: list[Signal] = []

    for (ticker, pillar), group in groups.items():
        if len(group) == 1:
            winners.append(group[0])
            continue

        # Check if there are conflicting directions in this group
        directions = {s.direction for s in group}
        if len(directions) == 1:
            # All same direction — keep all (no contradiction)
            winners.extend(group)
            continue

        # Contradiction detected — highest-score wins, rest are losers
        sorted_group = sorted(group, key=lambda s: (-(s.score or 0.0), s.alert_id))
        winner = sorted_group[0]
        group_losers = sorted_group[1:]

        # Margin-guard: find the best opposing-direction signal
        winner_dir = winner.direction
        opposing = [s for s in group_losers if s.direction != winner_dir]
        if opposing:
            best_opposing_score = max(s.score or 0.0 for s in opposing)
            winner_score = winner.score or 0.0
            if (winner_score - best_opposing_score) <= MARGIN_GUARD:
                new_severity = _SEVERITY_DOWNGRADE[winner.severity]
                if new_severity != winner.severity:
                    logger.debug(
                        "margin-guard: %s/%s winner score=%.2f vs opposing=%.2f — "
                        "downgrading %s -> %s",
                        ticker, pillar, winner_score, best_opposing_score,
                        winner.severity, new_severity,
                    )
                    winner = dataclasses.replace(winner, severity=new_severity)

        # If margin-guard pushed winner to MONITORING, it cannot enter route_signals
        if winner.severity == "MONITORING":
            losers.append(winner)
        else:
            winners.append(winner)

        losers.extend(group_losers)

    return winners, losers
