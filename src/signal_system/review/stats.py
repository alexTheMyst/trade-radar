"""Shared statistical helpers for signal measurement.

Pure functions only — no DB access, no side effects. The Spearman IC lives
here so both the review report (live signals) and the weight-replay CLI
(offline hypotheticals) rank on the same formula.
"""

from __future__ import annotations

import pandas as pd

IC_MIN_N: int = 10


def spearman_ic(
    scores: list[float], excesses: list[float], *, min_n: int = IC_MIN_N
) -> float | None:
    """Spearman rank correlation between scores and signed excesses.

    Returns None when either list is too short, lengths differ, or the
    excesses have fewer than 2 distinct values (no variation to rank).
    """
    if len(scores) < min_n or len(scores) != len(excesses):
        return None
    if len(set(excesses)) < 2:
        return None
    # Spearman = Pearson on ranks. pandas corr(method="spearman") pulls in scipy,
    # which is out of scope — rank manually instead.
    frame = pd.DataFrame({"score": scores, "signed_excess": excesses})
    ranked = frame.rank()
    return float(ranked["score"].corr(ranked["signed_excess"], method="pearson"))
