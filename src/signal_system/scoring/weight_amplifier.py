"""Position-weight severity amplifier.

Adjusts severity classification thresholds based on a ticker's portfolio
allocation relative to the median. Higher-weight positions get lower thresholds
(easier to promote to ACTION_REQUIRED). Raw scores are never modified —
preserving IC measurement integrity.
"""
from __future__ import annotations

import math
import statistics
from typing import Literal

Severity = Literal["ACTION_REQUIRED", "INFORMATIONAL", "MONITORING"]

_CLAMP_MIN: float = 0.25
_CLAMP_MAX: float = 4.0
_SHIFT_SCALE: float = 10.0
_MAX_NEGATIVE_SHIFT: float = 10.0


def _compute_shift(weight: float, median_weight: float) -> float:
    """Compute threshold shift from position weight relative to median.

    Returns positive values for above-median weights (lowers thresholds)
    and negative values for below-median weights (raises thresholds).
    """
    if median_weight <= 0:
        return 0.0
    ratio = weight / median_weight
    clamped = max(_CLAMP_MIN, min(ratio, _CLAMP_MAX))
    return _SHIFT_SCALE * math.log2(clamped)


def adjusted_severity(
    score: float,
    ticker: str,
    weights: dict[str, float],
    base_thresholds: tuple[float, float],
) -> Severity:
    """Determine severity with position-weight threshold adjustment.

    Args:
        score: Raw composite score or confidence (0-100 scale).
        ticker: Ticker symbol to look up in weights.
        weights: {ticker: weight_pct} from universe.csv.
        base_thresholds: (action_required_threshold, informational_threshold).

    Returns:
        Severity string: ACTION_REQUIRED, INFORMATIONAL, or MONITORING.
    """
    ar_base, info_base = base_thresholds

    if not weights:
        if score >= ar_base:
            return "ACTION_REQUIRED"
        if score >= info_base:
            return "INFORMATIONAL"
        return "MONITORING"

    positive_weights = [w for w in weights.values() if w > 0]
    if not positive_weights:
        median_weight = 0.0
    else:
        median_weight = statistics.median(positive_weights)

    if ticker not in weights:
        shift = 0.0
    else:
        weight = weights[ticker]

        if weight <= 0 and median_weight > 0:
            shift = _SHIFT_SCALE * math.log2(_CLAMP_MIN)
        else:
            shift = _compute_shift(weight, median_weight)

    shift = max(shift, -_MAX_NEGATIVE_SHIFT)

    ar_threshold = ar_base - shift
    info_threshold = info_base - shift

    if score >= ar_threshold:
        return "ACTION_REQUIRED"
    if score >= info_threshold:
        return "INFORMATIONAL"
    return "MONITORING"
