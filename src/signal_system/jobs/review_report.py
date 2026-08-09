"""review-report job — the quarterly tuning readout.

Standard job shape: runs row, heartbeat, print + Telegram delivery.
The report itself is pure computation in review/report.py.
"""

from __future__ import annotations

import logging

from signal_system.delivery.telegram_sender import send_message
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.review.report import compute_report
from signal_system.state import repository

logger = logging.getLogger(__name__)

_AGENT_LABELS: dict[str, str] = {
    "discovery_agent": "Discovery Agent",
    "news_classifier": "News Classifier",
    "exit_sentinel": "Exit Sentinel",
}


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _render_report_text() -> str:
    result = compute_report()
    lines = ["Review Report", "=" * 20]

    for agent, metrics in result.per_agent.items():
        label = _AGENT_LABELS.get(agent, agent)
        lines.append("")
        lines.append(f"{label} ({agent})")
        lines.append(f"  n: {metrics.n_30d} (30d) / {metrics.n_90d} (90d)")
        lines.append(
            f"  mean signed excess: {_fmt_pct(metrics.mean_signed_excess_30d)} "
            f"(30d) / {_fmt_pct(metrics.mean_signed_excess_90d)} (90d)"
        )
        lines.append(
            f"  median signed excess: {_fmt_pct(metrics.median_signed_excess_30d)} "
            f"(30d) / {_fmt_pct(metrics.median_signed_excess_90d)} (90d)"
        )
        lines.append(
            f"  hit rate: {metrics.hit_rate_30d if metrics.hit_rate_30d is None else f'{metrics.hit_rate_30d:.0%}'} "
            f"(30d) / {metrics.hit_rate_90d if metrics.hit_rate_90d is None else f'{metrics.hit_rate_90d:.0%}'} (90d)"
        )
        lines.append(
            f"  IC: {metrics.ic_30d if metrics.ic_30d is None else f'{metrics.ic_30d:.3f}'} "
            f"(30d, n>=10) / {metrics.ic_90d if metrics.ic_90d is None else f'{metrics.ic_90d:.3f}'} (90d)"
        )
        if metrics.neutral_count_30d or metrics.neutral_count_90d:
            lines.append(
                f"  neutral (excluded): {metrics.neutral_count_30d} (30d) / {metrics.neutral_count_90d} (90d)"
            )
        if metrics.missing_benchmark_30d or metrics.missing_benchmark_90d:
            lines.append(
                f"  missing benchmark: {metrics.missing_benchmark_30d} (30d) / {metrics.missing_benchmark_90d} (90d)"
            )
        if metrics.delivered_mean_excess_30d is not None or metrics.suppressed_mean_excess_30d is not None:
            lines.append(
                f"  router: DELIVERED {_fmt_pct(metrics.delivered_mean_excess_30d)} "
                f"vs SUPPRESSED {_fmt_pct(metrics.suppressed_mean_excess_30d)} (30d)"
            )
        if metrics.acted_split_30d:
            split = metrics.acted_split_30d
            lines.append(
                f"  acted {_fmt_pct(split.get('acted_mean'))} vs ignored "
                f"{_fmt_pct(split.get('ignored_mean'))} (30d)"
            )
        if metrics.unacted_count:
            lines.append(f"  unreviewed: {metrics.unacted_count}")
        if metrics.weight_version_counts:
            breakdown = ", ".join(
                f"{k}={v}" for k, v in sorted(metrics.weight_version_counts.items())
            )
            lines.append(f"  weight_version: {breakdown}")

    news_metrics = result.per_agent.get("news_classifier")
    if news_metrics and news_metrics.calibration:
        lines.append("")
        lines.append("Confidence calibration (news)")
        for label, bucket in news_metrics.calibration.items():
            hit = "N/A" if bucket["hit_rate"] is None else f"{bucket['hit_rate']:.0%}"
            mean = "N/A" if bucket["mean_signed_excess"] is None else _fmt_pct(bucket["mean_signed_excess"])
            lines.append(
                f"  {label:<12} n={bucket['n']:<4} hit={hit:<5} mean_signed_excess={mean}"
            )
        if news_metrics.calibration_out_of_range:
            lines.append(
                f"  out-of-range (conf<0.60): {news_metrics.calibration_out_of_range}"
            )

    if result.calibration_flags:
        lines.append("")
        lines.append("CALIBRATION FLAGS")
        for flag in result.calibration_flags:
            lines.append(f"  ! {flag}")

    if result.router_flags:
        lines.append("")
        lines.append("ROUTER FLAGS")
        for flag in result.router_flags:
            lines.append(f"  ! {flag}")

    if result.llm_summary:
        lines.append("")
        lines.append("LLM Calls")
        for row in result.llm_summary:
            lines.append(
                f"  {row['job']}: {row['calls']} calls, {row['input_tokens']} in, "
                f"{row['output_tokens']} out, cache_r={row['cache_read_input_tokens']} "
                f"cache_c={row['cache_creation_input_tokens']}"
            )
    if result.cache_warning:
        lines.append("")
        lines.append("WARNING: prompt caching INACTIVE — news_classifier has >=20 calls "
                     "with zero cache tokens. The 2-pillar system prompt may be under the "
                     "cacheable minimum (~1024 tokens).")

    return "\n".join(lines)


def run() -> None:
    run_id = repository.insert_run("review-report")
    try:
        with heartbeat(job="review-report"):
            text = _render_report_text()
            logger.info("review-report:\n%s", text)
            print(text)
            send_message(text)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
