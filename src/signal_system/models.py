"""Canonical Signal dataclass and alert_id helper for signal-system.

This module is imported by all agents, the router, and jobs. Use stdlib only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Severity values — constrained to these three strings.
Severity = Literal["ACTION_REQUIRED", "INFORMATIONAL", "MONITORING"]


@dataclass(frozen=True, slots=True)
class Signal:
    """Immutable value object produced by agents and passed to the router.

    Fields are write-once at construction time (frozen=True).
    routing_status is NOT a field — it lives in the DB and is set by the router.
    """

    ticker: str | None
    score: float | None
    severity: Severity
    agent: str
    timestamp: datetime
    alert_id: str
    title: str
    body: str | None = None
    sub_scores: dict[str, float] = field(default_factory=dict)
    model_version: str | None = None
    thesis_version_hash: str | None = None
    signal_price_snapshot: float | None = None
    direction: str | None = None  # "positive" | "negative" | "neutral" from ClassificationResult
    pillar: str | None = None  # thesis pillar name from ClassificationResult; None for non-news signals


def compute_alert_id(ticker: str | None, date_iso: str, rule: str, agent: str) -> str:
    """Return the SHA-256 hex digest of '{ticker}:{date_iso}:{rule}:{agent}'.

    Args:
        ticker: Ticker symbol or None (normalised to '_').
        date_iso: ISO date string YYYY-MM-DD.
        rule: Rule or classifier name that generated the signal.
        agent: Agent name (e.g. 'news_classifier', 'DAILY_CLOSE').

    Returns:
        64-character lowercase hex string, deterministic across processes/OSes.
    """
    key = f"{ticker or '_'}:{date_iso}:{rule}:{agent}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
