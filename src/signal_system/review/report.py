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

import pandas as pd

from signal_system.state import repository

_ET = ZoneInfo("America/New_York")

_AGENTS: tuple[str, ...] = ("discovery_agent", "news_classifier")
_NEWS_NEUTRAL: str = "neutral"
_IC_MIN_N: int = 10


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


@dataclass(frozen=True, slots=True)
class ReportResult:
    per_agent: dict[str, AgentMetrics] = field(default_factory=dict)
    router_flags: list[str] = field(default_factory=list)
    llm_summary: list[dict] = field(default_factory=list)
    cache_warning: bool = False


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
    if agent == "news_classifier" and direction == "negative":
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


def _spearman_ic(scores: list[float], excesses: list[float]) -> float | None:
    if len(scores) < _IC_MIN_N or len(scores) != len(excesses):
        return None
    if len(set(excesses)) < 2:
        return None
    # Spearman = Pearson on ranks. pandas corr(method="spearman") pulls in scipy,
    # which is out of scope — rank manually instead.
    frame = pd.DataFrame({"score": scores, "signed_excess": excesses})
    ranked = frame.rank()
    return float(ranked["score"].corr(ranked["signed_excess"], method="pearson"))


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
    metrics.ic_30d = _spearman_ic(stats_30.scores, stats_30.excesses)
    metrics.neutral_count_30d = stats_30.neutral
    metrics.missing_benchmark_30d = stats_30.missing

    metrics.n_90d = len(stats_90.excesses)
    metrics.mean_signed_excess_90d = _mean(stats_90.excesses)
    metrics.median_signed_excess_90d = _median(stats_90.excesses)
    metrics.hit_rate_90d = _hit_rate(stats_90.excesses)
    metrics.ic_90d = _spearman_ic(stats_90.scores, stats_90.excesses)
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
    )
