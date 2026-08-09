"""Review report — the tuning readout for per-agent signal quality.

Pure computation over repository data: excess returns vs SPY, hit rates,
Spearman IC, router validation, acted-vs-ignored splits, weight-version
breakdown, and llm_calls telemetry. No DB writes, no side effects.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system.review.stats import spearman_ic
from signal_system.state import repository

_ET = ZoneInfo("America/New_York")

_AGENTS: tuple[str, ...] = ("discovery_agent", "news_classifier", "exit_sentinel")
_NEWS_NEUTRAL: str = "neutral"
_IC_MIN_N: int = 10

# News-classifier confidence calibration buckets (news only). The amplifier can
# deliver confidences below 0.60, so everything under the first bucket's lower
# edge is counted, not silently dropped.
_CAL_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("[0.60,0.70)", 0.60, 0.70),
    ("[0.70,0.80)", 0.70, 0.80),
    ("[0.80,0.85)", 0.80, 0.85),
    ("[0.85,1.00]", 0.85, 1.00),
)
_CAL_FLAG_MIN_N: int = 10


@dataclass
class AgentMetrics:
    n_30d: int = 0
    n_90d: int = 0
    mean_signed_excess_30d: float | None = None
    mean_signed_excess_90d: float | None = None
    median_signed_excess_30d: float | None = None
    median_signed_excess_90d: float | None = None
    hit_rate_30d: float | None = None
    hit_rate_90d: float | None = None
    ic_30d: float | None = None
    ic_90d: float | None = None
    neutral_count_30d: int = 0
    neutral_count_90d: int = 0
    missing_benchmark_30d: int = 0
    missing_benchmark_90d: int = 0
    delivered_mean_excess_30d: float | None = None
    suppressed_mean_excess_30d: float | None = None
    acted_split_30d: dict = field(default_factory=dict)
    acted_split_90d: dict = field(default_factory=dict)
    weight_version_counts: dict[str, int] = field(default_factory=dict)
    unacted_count: int = 0
    calibration: dict[str, dict] = field(default_factory=dict)
    calibration_out_of_range: int = 0


@dataclass(frozen=True, slots=True)
class ReportResult:
    per_agent: dict[str, AgentMetrics] = field(default_factory=dict)
    router_flags: list[str] = field(default_factory=list)
    llm_summary: list[dict] = field(default_factory=list)
    cache_warning: bool = False
    calibration_flags: list[str] = field(default_factory=list)


@dataclass
class _HorizonStats:
    scores: list[float] = field(default_factory=list)
    excesses: list[float] = field(default_factory=list)
    missing: int = 0
    neutral: int = 0


def _signal_date(row: dict) -> str:
    return datetime.fromisoformat(row["timestamp"]).astimezone(_ET).date().isoformat()


def _horizon_date(row: dict, days: int) -> str:
    return (
        datetime.fromisoformat(row["timestamp"]).astimezone(_ET)
        + timedelta(days=days)
    ).date().isoformat()


def _raw_return(snapshot: float, outcome: float) -> float:
    if not snapshot or snapshot <= 0:
        raise ValueError("snapshot must be positive")
    return outcome / snapshot - 1.0


def _signed_excess(excess: float, direction: str | None, agent: str) -> float:
    # Direction-aware sign: for negative-directional agents a price FALL is a
    # hit. exit_sentinel is always direction="negative" — a sentinel hit means
    # the price fell after the alert.
    if agent in ("news_classifier", "exit_sentinel") and direction == "negative":
        return -excess
    return excess


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _hit_rate(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for v in values if v > 0) / len(values)


def _bucket_for(score: float) -> str | None:
    """Return the calibration bucket label for *score*, or None if out of range."""
    for label, low, high in _CAL_BUCKETS:
        if low <= score < high or (score == high and high == 1.00):
            return label
    return None


def _compute_calibration(rows: list[dict]) -> tuple[dict[str, dict], int]:
    """Build news-classifier confidence calibration from routed rows.

    Returns (bucket_stats, out_of_range) where each bucket maps label ->
    {n, hit_rate, mean_signed_excess} over the 30d signed-excess pipeline
    (neutral excluded, same as hit-rate/IC). Out-of-range counts confidences
    below 0.60 — the amplifier can deliver them and they must not vanish.
    """
    stats: dict[str, dict] = {
        label: {"n": 0, "hit_rate": None, "mean_signed_excess": None}
        for label, _, _ in _CAL_BUCKETS
    }
    excess_by_bucket: dict[str, list[float]] = {label: [] for label, _, _ in _CAL_BUCKETS}
    out_of_range = 0

    for row in rows:
        if row.get("direction") == _NEWS_NEUTRAL:
            continue
        outcome = row.get("outcome_price_30d")
        if outcome is None:
            continue
        spy_at_signal = repository.get_benchmark_close_on_or_after("SPY", _signal_date(row))
        spy_at_horizon = repository.get_benchmark_close_on_or_after("SPY", _horizon_date(row, 30))
        if spy_at_signal is None or spy_at_horizon is None:
            continue
        raw = _raw_return(row["signal_price_snapshot"], outcome)
        excess = raw - (spy_at_horizon / spy_at_signal - 1.0)
        signed = _signed_excess(excess, row.get("direction"), "news_classifier")

        score = row.get("score")
        if score is None:
            continue
        label = _bucket_for(float(score))
        if label is None:
            out_of_range += 1
            continue
        excess_by_bucket[label].append(signed)

    for label, _low, _high in _CAL_BUCKETS:
        values = excess_by_bucket[label]
        stats[label]["n"] = len(values)
        if values:
            stats[label]["hit_rate"] = _hit_rate(values)
            stats[label]["mean_signed_excess"] = _mean(values)
    return stats, out_of_range


def _calibration_flags(per_agent: dict[str, AgentMetrics]) -> list[str]:
    """Flag news-classifier miscalibration: top bucket beat by a lower bucket.

    Only fires when both buckets have n >= _CAL_FLAG_MIN_N — a single lucky
    low-confidence hit is noise, not evidence.
    """
    flags: list[str] = []
    news = per_agent.get("news_classifier")
    if news is None:
        return flags
    cal = news.calibration
    top_label, _, _ = _CAL_BUCKETS[-1]
    top = cal.get(top_label)
    if top is None or top["n"] < _CAL_FLAG_MIN_N or top["hit_rate"] is None:
        return flags
    for label, _, _ in _CAL_BUCKETS[:-1]:
        lower = cal.get(label)
        if lower is None or lower["n"] < _CAL_FLAG_MIN_N or lower["hit_rate"] is None:
            continue
        if lower["hit_rate"] > top["hit_rate"]:
            flags.append(
                f"MISCALIBRATION: {top_label} hit rate {top['hit_rate']:.0%} < "
                f"{label} hit rate {lower['hit_rate']:.0%} (both n>={_CAL_FLAG_MIN_N})"
            )
    return flags


def _accumulate_horizon(rows: list[dict], agent: str, days: int) -> _HorizonStats:
    stats = _HorizonStats()
    outcome_key = f"outcome_price_{days}d"
    for row in rows:
        outcome = row.get(outcome_key)
        if outcome is None:
            continue
        spy_at_signal = repository.get_benchmark_close_on_or_after("SPY", _signal_date(row))
        spy_at_horizon = repository.get_benchmark_close_on_or_after("SPY", _horizon_date(row, days))
        if spy_at_signal is None or spy_at_horizon is None:
            stats.missing += 1
            continue

        raw = _raw_return(row["signal_price_snapshot"], outcome)
        spy_ret = spy_at_horizon / spy_at_signal - 1.0
        excess = raw - spy_ret

        if agent == "news_classifier" and row.get("direction") == _NEWS_NEUTRAL:
            stats.neutral += 1
            continue

        signed = _signed_excess(excess, row.get("direction"), agent)
        stats.scores.append(float(row["score"]) if row["score"] is not None else 0.0)
        stats.excesses.append(signed)
    return stats


def _compute_agent_metrics(rows: list[dict], agent: str) -> AgentMetrics:
    metrics = AgentMetrics()
    delivered_excess: list[float] = []
    suppressed_excess: list[float] = []
    acted_excess: list[float] = []
    ignored_excess: list[float] = []
    unacted = 0

    for row in rows:
        wv = row.get("weight_version") or "legacy-mixed"
        metrics.weight_version_counts[wv] = metrics.weight_version_counts.get(wv, 0) + 1

    stats_30 = _accumulate_horizon(rows, agent, 30)
    stats_90 = _accumulate_horizon(rows, agent, 90)

    metrics.n_30d = len(stats_30.excesses)
    metrics.mean_signed_excess_30d = _mean(stats_30.excesses)
    metrics.median_signed_excess_30d = _median(stats_30.excesses)
    metrics.hit_rate_30d = _hit_rate(stats_30.excesses)
    metrics.ic_30d = spearman_ic(stats_30.scores, stats_30.excesses, min_n=_IC_MIN_N)
    metrics.neutral_count_30d = stats_30.neutral
    metrics.missing_benchmark_30d = stats_30.missing

    metrics.n_90d = len(stats_90.excesses)
    metrics.mean_signed_excess_90d = _mean(stats_90.excesses)
    metrics.median_signed_excess_90d = _median(stats_90.excesses)
    metrics.hit_rate_90d = _hit_rate(stats_90.excesses)
    metrics.ic_90d = spearman_ic(stats_90.scores, stats_90.excesses, min_n=_IC_MIN_N)
    metrics.neutral_count_90d = stats_90.neutral
    metrics.missing_benchmark_90d = stats_90.missing

    for row in rows:
        outcome = row.get("outcome_price_30d")
        if outcome is None:
            continue
        spy_at_signal = repository.get_benchmark_close_on_or_after("SPY", _signal_date(row))
        spy_at_horizon = repository.get_benchmark_close_on_or_after("SPY", _horizon_date(row, 30))
        if spy_at_signal is None or spy_at_horizon is None:
            continue
        raw = _raw_return(row["signal_price_snapshot"], outcome)
        excess = raw - (spy_at_horizon / spy_at_signal - 1.0)
        if agent == "news_classifier" and row.get("direction") == _NEWS_NEUTRAL:
            continue
        signed = _signed_excess(excess, row.get("direction"), agent)
        if row.get("routing_status") == "DELIVERED":
            delivered_excess.append(signed)
        else:
            suppressed_excess.append(signed)
        if row.get("acted") == 1:
            acted_excess.append(signed)
        elif row.get("acted") == 0:
            ignored_excess.append(signed)
        else:
            unacted += 1

    metrics.delivered_mean_excess_30d = _mean(delivered_excess)
    metrics.suppressed_mean_excess_30d = _mean(suppressed_excess)
    metrics.acted_split_30d = {
        "acted_mean": _mean(acted_excess),
        "ignored_mean": _mean(ignored_excess),
    }
    metrics.unacted_count = unacted

    if agent == "news_classifier":
        metrics.calibration, metrics.calibration_out_of_range = _compute_calibration(rows)
    return metrics


def _router_flags(per_agent: dict[str, AgentMetrics]) -> list[str]:
    flags: list[str] = []
    for agent, metrics in per_agent.items():
        delivered = metrics.delivered_mean_excess_30d
        suppressed = metrics.suppressed_mean_excess_30d
        if delivered is not None and suppressed is not None and suppressed > delivered:
            flags.append(
                f"{agent} SUPPRESSED beats DELIVERED on 30d mean excess "
                f"({suppressed:+.2%} vs {delivered:+.2%})"
            )
    return flags


def _cache_warning(llm_summary: list[dict]) -> bool:
    for row in llm_summary:
        if row["job"] != "news_classifier":
            continue
        if row["calls"] >= 20 and row["cache_read_input_tokens"] + row["cache_creation_input_tokens"] == 0:
            return True
    return False


def compute_report() -> ReportResult:
    """Compute the full review-report metrics from repository data."""
    rows = repository.list_signals_for_review()
    per_agent: dict[str, AgentMetrics] = {}
    for agent in _AGENTS:
        agent_rows = [r for r in rows if r["agent"] == agent]
        per_agent[agent] = _compute_agent_metrics(agent_rows, agent)

    llm_summary = repository.get_llm_call_summary()
    return ReportResult(
        per_agent=per_agent,
        router_flags=_router_flags(per_agent),
        llm_summary=llm_summary,
        cache_warning=_cache_warning(llm_summary),
        calibration_flags=_calibration_flags(per_agent),
    )
