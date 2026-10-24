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

# Agent identities — canonical names used for per-agent measurement.
# NEWS_CLASSIFIER_AGENT was historically defined in classifier/news_classifier.py;
# it is re-exported there for backward compatibility. Discovery's identity is
# the literal "discovery_agent" used at Signal construction.
NEWS_CLASSIFIER_AGENT: str = "news_classifier"
DISCOVERY_AGENT: str = "discovery_agent"
EXIT_SENTINEL_AGENT: str = "exit_sentinel"

# Routing-regime stamp written to signals.routing_regime. Defined here (stdlib
# only) so repository.py can stamp every insert without importing the router.
# NULL = legacy first-come routing; never backfilled, so pre- and post-change
# rows are never aggregated together (same convention as weight_version).
#
# v2 (Plan 12): per-agent ACTION_REQUIRED lanes, a reserved sentinel INFO slot,
# a macro-news cap, explicit shadow-mode Discovery, and the repeat-story
# monitoring reason.
ROUTING_REGIME: str = "deferred-info-priority-v2"

# The sentinel emits native 0-100 scores (raw passthrough), so it sits on the
# same competition scale as Discovery. An agent NOT on 0-100 loses every slot
# competition — competition_score() only rescales news_classifier confidences.


def competition_score(signal: Signal) -> float:
    """Return *signal*'s score on a unified 0-100 competition scale.

    The News Classifier's score is a confidence in [0.0, 1.0]; Discovery's is
    a 0-100 composite. The router must compare them on one scale for
    cross-agent AR/INFO slot competition (Phase 3.2). News confidences are
    multiplied by 100; Discovery scores pass through. Display rendering keeps
    each agent's native scale (jobs/common.py format_score).

    Lives in stdlib-only models.py so the router never imports the
    anthropic-heavy classifier module.
    """
    if signal.agent == NEWS_CLASSIFIER_AGENT and signal.score is not None:
        return signal.score * 100.0
    return signal.score or 0.0


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
    weight_version: str | None = None  # scoring-regime stamp (Discovery); NULL = legacy-mixed regime
    monitoring_reason: str | None = None  # off_thesis | volume_cap | parse_failure | below_threshold | reconciled | cooldown | repeat
    trigger_json: str | None = None  # JSON of breached sentinel triggers; NULL for other agents


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
