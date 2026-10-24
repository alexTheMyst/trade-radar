from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.classifier import classify_headlines
from signal_system.classifier.news_classifier import (
    NEWS_CLASSIFIER_AGENT,
    article_dedup_key,
    headline_dedup_key,
    is_repeat_story,
)
from signal_system.data.finnhub_client import fetch_company_news, fetch_general_news
from signal_system.data.holdings import held_ticker_set
from signal_system.data.thesis_loader import load_thesis
from signal_system.data.universe import (
    get_core_holdings,
    get_position_weights,
    require_non_empty_universe,
)
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import (
    PersistenceSummary,
    compute_score_deltas,
    confirm_delivered_signals,
    persist_routed_signals,
    render_digest,
    send_alert_buttons,
    validate_digest_payload,
)
from signal_system.models import Signal, competition_score, compute_alert_id
from signal_system.monitoring import heartbeat
from signal_system.reconciler import reconcile_directions
from signal_system.router import stage_signals
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Per-ticker round-robin allocation (Plan 12b, supersedes Plan 09 §9a).
_MAX_PER_TICKER = 8
_MAX_TOTAL_COMPANY = 120
_WINDOW_MULTIPLIER_CAP = 3
_MAX_GENERAL_HEADLINES = 20
# Deprecated alias kept for the overflow body and any external readers; the
# allocator no longer uses a single global "newest N" cap for company news.
_MAX_NEWS_HEADLINES = _MAX_TOTAL_COMPANY

# Every MONITORING reason this job can legitimately emit. A signal whose
# monitoring_reason is not in this set has no breakdown bucket — that is a bug,
# and the sum-to-total assertion below must fail rather than quietly under-add.
_KNOWN_MONITORING_REASONS: frozenset[str] = frozenset(
    {
        "off_thesis",
        "volume_cap",
        "parse_failure",
        "below_threshold",
        "reconciled",
        "repeat",
    }
)


def _now_et() -> datetime:
    return datetime.now(_ET)


def _previous_close_datetime(previous_close_date: date) -> datetime:
    return datetime(
        previous_close_date.year,
        previous_close_date.month,
        previous_close_date.day,
        16,
        0,
        0,
        tzinfo=_ET,
    )


def _news_item_datetime(item: dict) -> datetime | None:
    raw_timestamp = item.get("datetime")
    if raw_timestamp in (None, ""):
        return None

    try:
        timestamp = float(raw_timestamp)
    except (TypeError, ValueError):
        return None

    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).astimezone(_ET)


def _make_overflow_monitoring_signal(
    *,
    ticker: str | None,
    headline: str,
    headline_dt: datetime,
    generated_at: datetime,
    thesis_version_hash: str,
) -> Signal:
    # `timestamp` is signal-generation time (the run), NOT the article's publication
    # time — the article time is preserved in the body. Stamping article time here
    # produced phantom weekend/no-run rows in the signals table.
    # ticker=None marks a general/macro overflow row; compute_alert_id normalizes
    # None -> '_' exactly like the classifier does.
    rule = f"volume_cap:{headline_dedup_key(ticker or 'GENERAL', headline)[:16]}"
    return Signal(
        ticker=ticker,
        score=None,
        severity="MONITORING",
        agent=NEWS_CLASSIFIER_AGENT,
        timestamp=generated_at,
        alert_id=compute_alert_id(
            ticker, headline_dt.date().isoformat(), rule, NEWS_CLASSIFIER_AGENT
        ),
        title=f"[volume_cap] {headline[:120]}",
        body=(
            "Headline skipped by the per-ticker/global volume cap "
            f"({_MAX_PER_TICKER}/ticker, {_MAX_TOTAL_COMPANY} total). "
            f"Published {headline_dt.isoformat()}."
        ),
        model_version=config.ANTHROPIC_MODEL,
        thesis_version_hash=thesis_version_hash,
        monitoring_reason="volume_cap",
    )


def _collect_windowed_headlines(
    tickers: list[str],
    *,
    previous_close: datetime,
    now_et: datetime,
) -> list[tuple[str, dict, datetime]]:
    candidates: list[tuple[str, dict, datetime]] = []
    from_date = previous_close.date()
    to_date = now_et.date()

    for ticker in tickers:
        for item in fetch_company_news(ticker, from_date, to_date):
            item_dt = _news_item_datetime(item)
            if item_dt is None:
                continue
            if previous_close <= item_dt <= now_et:
                candidates.append((ticker, item, item_dt))

    return candidates


def _collect_general_headlines(
    *,
    previous_close: datetime,
    now_et: datetime,
) -> list[tuple[str | None, dict, datetime]]:
    """Fetch macro/general headlines; ticker is None for all of them."""
    candidates: list[tuple[str | None, dict, datetime]] = []
    for item in fetch_general_news("general"):
        item_dt = _news_item_datetime(item)
        if item_dt is None:
            continue
        if previous_close <= item_dt <= now_et:
            candidates.append((None, item, item_dt))
    return candidates


def _dedupe_headlines(
    candidates: list[tuple[str | None, dict, datetime]],
    seen: set[str],
) -> list[tuple[str | None, dict, datetime]]:
    """Dedup by article identity into a caller-supplied *seen* set (ticker-independent)."""
    unique: list[tuple[str | None, dict, datetime]] = []
    for ticker, item, item_dt in sorted(candidates, key=lambda row: row[2], reverse=True):
        headline = str(item.get("headline", ""))
        if not headline.strip():
            continue
        dedup_key = article_dedup_key(item)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        unique.append((ticker, item, item_dt))
    return unique


def _dedupe_and_cap_headlines(
    candidates: list[tuple[str | None, dict, datetime]],
    *,
    seen: set[str] | None = None,
    cap: int = _MAX_NEWS_HEADLINES,
) -> tuple[list[tuple[str | None, dict, datetime]], list[tuple[str | None, dict, datetime]]]:
    """Dedup by article identity, cap, and return (kept, overflow).

    Pass a shared *seen* set across feeds so the same story (returned in both the
    company and general feeds, or under multiple related tickers) alerts once.
    """
    dedup_seen = set() if seen is None else seen
    unique = _dedupe_headlines(candidates, dedup_seen)
    return unique[:cap], unique[cap:]


def _window_days(previous_close: datetime, now_et: datetime) -> int:
    """Elapsed whole-ish days between the previous close and now, min 1.

    Elapsed time — not trading days — is what drives headline volume. A normal
    overnight window is ~17h → 1; Fri 16:00 → Mon 09:00 is 65h → 3; one missed
    daily-close gives ~41h → 2. This avoids needing a holiday calendar.
    """
    hours = max(0.0, (now_et - previous_close).total_seconds() / 3600.0)
    return max(1, math.ceil(hours / 24.0))


def allocate_headlines(
    unique: list[tuple[str | None, dict, datetime]],
    *,
    held: set[str],
    window_days: int,
) -> tuple[list[tuple[str | None, dict, datetime]], list[tuple[str | None, dict, datetime]]]:
    """Round-robin allocate already-deduped company headlines, held tickers first.

    Each bucket is newest-first. A bucket is capped at
    ``_MAX_PER_TICKER * min(window_days, _WINDOW_MULTIPLIER_CAP)`` and the total
    kept is capped at ``_MAX_TOTAL_COMPANY``. Every pass takes the next item
    from each bucket in bucket order, so a held ticker's first headline always
    precedes any non-held ticker's second.
    """
    buckets: dict[str | None, list[tuple[str | None, dict, datetime]]] = {}
    for row in unique:
        buckets.setdefault(row[0], []).append(row)

    def _order(ticker: str | None) -> tuple[int, str]:
        return (0 if ticker in held else 1, ticker or "")

    ordered = sorted(buckets, key=_order)
    per_ticker_cap = _MAX_PER_TICKER * min(window_days, _WINDOW_MULTIPLIER_CAP)

    kept: list[tuple[str | None, dict, datetime]] = []
    taken: dict[str | None, int] = {ticker: 0 for ticker in ordered}
    while len(kept) < _MAX_TOTAL_COMPANY:
        progressed = False
        for ticker in ordered:
            if len(kept) >= _MAX_TOTAL_COMPANY:
                break
            index = taken[ticker]
            if index >= per_ticker_cap or index >= len(buckets[ticker]):
                continue
            kept.append(buckets[ticker][index])
            taken[ticker] = index + 1
            progressed = True
        if not progressed:
            break

    kept_ids = {id(row) for row in kept}
    overflow = [row for bucket in buckets.values() for row in bucket if id(row) not in kept_ids]
    return kept, overflow


def _persist_monitoring_signal(
    signal: Signal,
    *,
    demoted_from: str | None = None,
    monitoring_reason: str | None = None,
) -> bool:
    """Persist a MONITORING signal, returning True only on a real insert.

    insert_signal uses INSERT OR IGNORE — an alert_id collision (the same
    headline surfacing twice in a window) is a no-op, not a write. Callers must
    count actual inserts, never calls, or the digest over-reports.
    """
    inserted = repository.insert_signal(
        signal,
        routing_status="MONITORING",
        demoted_from=demoted_from,
        monitoring_reason=monitoring_reason,
    )
    if not inserted:
        logger.info("monitoring signal deduped on alert_id: %s", signal.alert_id[:12])
    return inserted


def _classify_kept_headlines(
    kept_items: list[tuple[str | None, dict, datetime]],
    *,
    thesis: object,
    thesis_version_hash: str,
    weights: dict[str, float],
) -> tuple[list[Signal], dict[str, int], int, int]:
    headlines_by_ticker: dict[str | None, list[dict]] = defaultdict(list)
    for ticker, item, _ in kept_items:
        headlines_by_ticker[ticker].append(item)

    routable: list[Signal] = []
    monitoring_breakdown: dict[str, int] = {}
    monitoring_total = 0
    monitoring_deduped = 0
    dedup_seen: set[str] = set()

    for ticker, headlines in headlines_by_ticker.items():
        for signal in classify_headlines(
            ticker=ticker,
            headlines=headlines,
            thesis=thesis,
            thesis_version_hash=thesis_version_hash,
            dedup_seen=dedup_seen,
            weights=weights,
        ):
            if signal.severity == "MONITORING":
                if _persist_monitoring_signal(signal):
                    monitoring_total += 1
                    reason = signal.monitoring_reason
                    if reason in _KNOWN_MONITORING_REASONS:
                        monitoring_breakdown[reason] = (
                            monitoring_breakdown.get(reason, 0) + 1
                        )
                    else:
                        logger.warning(
                            "MONITORING signal missing a known monitoring_reason: %r",
                            signal.alert_id[:12],
                        )
                else:
                    monitoring_deduped += 1
            else:
                routable.append(signal)

    return routable, monitoring_breakdown, monitoring_total, monitoring_deduped


def _send_digest_once(*, subject: str, body: str) -> None:
    telegram_sender.send_message(f"{subject}\n\n{body}")


def run() -> None:
    run_id = repository.insert_run("news-morning")
    try:
        with heartbeat.heartbeat(job="news-morning"):
            previous_close_date = repository.get_latest_successful_run_date("daily-close")
            if previous_close_date is None:
                raise RuntimeError(
                    "news-morning requires a successful prior daily-close run before fetching news"
                )

            thesis, thesis_version_hash = load_thesis(config.THESIS_PATH)
            now_et = _now_et()
            previous_close = _previous_close_datetime(previous_close_date)
            tickers = require_non_empty_universe(get_core_holdings(), job="news-morning")

            candidates = _collect_windowed_headlines(
                tickers,
                previous_close=previous_close,
                now_et=now_et,
            )
            general_candidates = _collect_general_headlines(
                previous_close=previous_close,
                now_et=now_et,
            )

            # Shared dedup set across both feeds: the same story surfaced under a
            # related ticker and in the general feed must alert only once.
            shared_seen: set[str] = set()
            window_days = _window_days(previous_close, now_et)
            held_tickers = held_ticker_set(fallback_to_core=False)
            unique_company = _dedupe_headlines(candidates, shared_seen)
            kept_items, overflow_items = allocate_headlines(
                unique_company, held=held_tickers, window_days=window_days
            )
            kept_general, overflow_general = _dedupe_and_cap_headlines(
                general_candidates,
                seen=shared_seen,
                cap=_MAX_GENERAL_HEADLINES,
            )
            all_overflow = overflow_items + overflow_general

            # Held-ticker coverage: classified / available for every held ticker
            # that overflowed (Plan 12b digest addition).
            available_by_ticker: dict[str, int] = defaultdict(int)
            for ticker, _item, _dt in unique_company:
                if ticker is not None:
                    available_by_ticker[ticker] += 1
            kept_by_ticker: dict[str, int] = defaultdict(int)
            for ticker, _item, _dt in kept_items:
                if ticker is not None:
                    kept_by_ticker[ticker] += 1
            coverage_parts = [
                f"{ticker} {kept_by_ticker.get(ticker, 0)}/{available_by_ticker[ticker]}"
                for ticker in sorted(available_by_ticker)
                if ticker in held_tickers
                and kept_by_ticker.get(ticker, 0) < available_by_ticker[ticker]
            ]

            volume_cap_count = 0
            volume_cap_deduped = 0
            for ticker, item, item_dt in all_overflow:
                if _persist_monitoring_signal(
                    _make_overflow_monitoring_signal(
                        ticker=ticker,
                        headline=str(item.get("headline", "")),
                        headline_dt=item_dt,
                        generated_at=now_et,
                        thesis_version_hash=thesis_version_hash,
                    )
                ):
                    volume_cap_count += 1
                else:
                    volume_cap_deduped += 1

            weights = get_position_weights()
            routable, classifier_breakdown, classifier_total, classifier_deduped = _classify_kept_headlines(
                kept_items + kept_general,
                thesis=thesis,
                thesis_version_hash=thesis_version_hash,
                weights=weights,
            )
            routable, reconciled_losers = reconcile_directions(routable)
            reconciled_count = 0
            reconciled_deduped = 0
            for loser in reconciled_losers:
                if _persist_monitoring_signal(
                    loser,
                    demoted_from="reconciled",
                    monitoring_reason="reconciled",
                ):
                    reconciled_count += 1
                else:
                    reconciled_deduped += 1
            # Cross-day repeat-story filter (Plan 12c3). Compare against DELIVERED
            # rows from the last 7 calendar days plus higher-ranked signals from
            # this same run. Repeats become MONITORING and never reach the router,
            # so they cannot consume an AR slot.
            repeat_count = 0
            repeat_deduped = 0
            run_alert_ids = {signal.alert_id for signal in routable}
            recent_rows: dict[str, list[dict]] = {}
            accepted_titles: dict[str, list[str]] = defaultdict(list)
            kept_routable: list[Signal] = []
            for signal in sorted(
                routable, key=lambda s: (-competition_score(s), s.ticker or "")
            ):
                if signal.ticker is not None:
                    ticker = signal.ticker
                    if ticker not in recent_rows:
                        recent_rows[ticker] = repository.list_recent_delivered_news(
                            ticker, now_et.date() - timedelta(days=7)
                        )
                    comparison = [
                        row["title"]
                        for row in recent_rows[ticker]
                        if row["alert_id"] not in run_alert_ids
                    ] + accepted_titles[ticker]
                    if is_repeat_story(signal, comparison):
                        repeat_signal = replace(
                            signal,
                            severity="MONITORING",
                            title=f"[repeat] {signal.title}",
                            monitoring_reason="repeat",
                        )
                        if _persist_monitoring_signal(repeat_signal):
                            repeat_count += 1
                        else:
                            repeat_deduped += 1
                        continue
                    accepted_titles[ticker].append(signal.title)
                kept_routable.append(signal)
            routable = kept_routable

            # Option D: AR routes now; INFO is staged for the evening dispatch job.
            routed = stage_signals(routable)
            # Deltas MUST be computed before persistence — the classifier stamps a
            # per-signal timestamp, so several same-run signals can share a ticker;
            # persisting first lets get_previous_score() compare against one of them.
            score_deltas = compute_score_deltas(routed)
            persistence_summary: PersistenceSummary = persist_routed_signals(routed)
            status_counts = dict(persistence_summary.status_counts)
            monitoring_breakdown = dict(classifier_breakdown)
            monitoring_breakdown["volume_cap"] = volume_cap_count
            monitoring_breakdown["reconciled"] = reconciled_count
            monitoring_breakdown["repeat"] = repeat_count
            status_counts["MONITORING"] = (
                classifier_total
                + volume_cap_count
                + reconciled_count
                + repeat_count
            )
            monitoring_deduped = (
                classifier_deduped
                + volume_cap_deduped
                + reconciled_deduped
                + repeat_deduped
            )

            # A breakdown that does not reconcile is a bug — fail rather than send
            # a digest that quietly does not add up. This assert is an early,
            # local check on the in-memory counters and is stripped under -O;
            # validate_digest_payload() re-checks the same sum against the
            # database below, so the guarantee does not depend on assertions.
            assert sum(monitoring_breakdown.values()) == status_counts["MONITORING"], (
                f"breakdown {monitoring_breakdown} does not sum to "
                f"{status_counts['MONITORING']}"
            )

            # The digest/button HELD tag reflects actual positions (holdings.csv),
            # not the scanned core-holding set: a core ticker that is not held
            # must not be tagged HELD. Fall back to core only when holdings.csv
            # is unavailable.
            digest_held_tickers = held_ticker_set(fallback_to_core=True)
            digest = render_digest(
                job_name="news-morning",
                scanned_tickers=len(tickers),
                routed_signals=routed,
                held_tickers=digest_held_tickers,
                score_deltas=score_deltas,
                status_counts=status_counts,
                monitoring_breakdown=monitoring_breakdown,
                monitoring_deduped=monitoring_deduped,
            )
            # Scoped to this run, not the calendar day: every signal this run
            # writes is stamped at or after now_et, and an earlier attempt's rows
            # are excluded so a rerun reports its own (deduped) result instead of
            # failing against rows it did not write.
            db_counts = repository.count_signals_by_routing_status(
                now_et, NEWS_CLASSIFIER_AGENT
            )
            db_monitoring_breakdown = repository.count_monitoring_by_reason(
                now_et, NEWS_CLASSIFIER_AGENT
            )
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                db_counts=db_counts,
                delivered_signals=persistence_summary.delivered_signals,
                db_monitoring_breakdown=db_monitoring_breakdown,
            )
            extra_lines: list[str] = []
            if window_days > 1:
                hours = int((now_et - previous_close).total_seconds() // 3600)
                extra_lines.append(
                    f"Window: {previous_close:%Y-%m-%d %H:%M} → "
                    f"{now_et:%Y-%m-%d %H:%M} ({hours}h, extended)"
                )
            if coverage_parts:
                extra_lines.append(
                    "Coverage (held, classified/available): " + ", ".join(coverage_parts)
                )
            body = digest.body
            if extra_lines:
                body = f"{body}\n\n" + "\n".join(extra_lines)
            _send_digest_once(subject=digest.subject, body=body)
            confirm_delivered_signals(persistence_summary.delivered_signals)
            send_alert_buttons(
                persistence_summary.delivered_signals, held_tickers=digest_held_tickers
            )
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
