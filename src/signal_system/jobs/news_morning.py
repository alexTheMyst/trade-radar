from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.classifier import classify_headlines
from signal_system.classifier.news_classifier import (
    NEWS_CLASSIFIER_AGENT,
    article_dedup_key,
    headline_dedup_key,
)
from signal_system.data.finnhub_client import fetch_company_news
from signal_system.data.thesis_loader import load_thesis
from signal_system.data.universe import (
    get_core_holdings,
    get_position_weights,
    require_non_empty_universe,
)
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import (
    PersistenceSummary,
    confirm_delivered_signals,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
from signal_system.models import Signal, compute_alert_id
from signal_system.monitoring import heartbeat
from signal_system.reconciler import reconcile_directions
from signal_system.router import route_signals
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_MAX_NEWS_HEADLINES = 50


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
    ticker: str,
    headline: str,
    headline_dt: datetime,
    generated_at: datetime,
    thesis_version_hash: str,
) -> Signal:
    # `timestamp` is signal-generation time (the run), NOT the article's publication
    # time — the article time is preserved in the body. Stamping article time here
    # produced phantom weekend/no-run rows in the signals table.
    rule = f"volume_cap:{headline_dedup_key(ticker, headline)[:16]}"
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
            f"Headline skipped because deduped news exceeded the newest-"
            f"{_MAX_NEWS_HEADLINES} volume cap. Published {headline_dt.isoformat()}."
        ),
        model_version=config.ANTHROPIC_MODEL,
        thesis_version_hash=thesis_version_hash,
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


def _dedupe_and_cap_headlines(
    candidates: list[tuple[str, dict, datetime]],
) -> tuple[list[tuple[str, dict, datetime]], list[tuple[str, dict, datetime]]]:
    unique_items: list[tuple[str, dict, datetime]] = []
    seen: set[str] = set()

    for ticker, item, item_dt in sorted(candidates, key=lambda row: row[2], reverse=True):
        headline = str(item.get("headline", ""))
        if not headline.strip():
            continue
        # Dedup by article identity, not (ticker, headline): the same story is
        # returned under every related ticker, and must alert only once.
        dedup_key = article_dedup_key(item)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        unique_items.append((ticker, item, item_dt))

    return unique_items[:_MAX_NEWS_HEADLINES], unique_items[_MAX_NEWS_HEADLINES:]


def _persist_monitoring_signal(signal: Signal) -> None:
    repository.insert_signal(signal, routing_status="MONITORING")


def _classify_kept_headlines(
    kept_items: list[tuple[str, dict, datetime]],
    *,
    thesis: object,
    thesis_version_hash: str,
    weights: dict[str, float],
) -> tuple[list[Signal], int]:
    headlines_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for ticker, item, _ in kept_items:
        headlines_by_ticker[ticker].append(item)

    routable: list[Signal] = []
    monitoring_count = 0
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
                _persist_monitoring_signal(signal)
                monitoring_count += 1
            else:
                routable.append(signal)

    return routable, monitoring_count


def _send_digest_once(*, subject: str, body: str) -> None:
    telegram_sender.send_message(f"{subject}\n\n{body}")


def run() -> None:
    run_id = repository.insert_run("news-morning")
    try:
        with heartbeat.heartbeat():
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
            kept_items, overflow_items = _dedupe_and_cap_headlines(candidates)

            for ticker, item, item_dt in overflow_items:
                _persist_monitoring_signal(
                    _make_overflow_monitoring_signal(
                        ticker=ticker,
                        headline=str(item.get("headline", "")),
                        headline_dt=item_dt,
                        generated_at=now_et,
                        thesis_version_hash=thesis_version_hash,
                    )
                )

            weights = get_position_weights()
            routable, classifier_monitoring_count = _classify_kept_headlines(
                kept_items,
                thesis=thesis,
                thesis_version_hash=thesis_version_hash,
                weights=weights,
            )
            routable, reconciled_losers = reconcile_directions(routable)
            for loser in reconciled_losers:
                repository.insert_signal(loser, routing_status="MONITORING", demoted_from="reconciled")
            persistence_summary: PersistenceSummary = persist_routed_signals(route_signals(routable))
            status_counts = dict(persistence_summary.status_counts)
            status_counts["MONITORING"] += classifier_monitoring_count + len(overflow_items) + len(reconciled_losers)

            digest = render_digest(
                job_name="news-morning",
                scanned_tickers=len(tickers),
                delivered_signals=persistence_summary.delivered_signals,
                status_counts=status_counts,
            )
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                expected_counts=status_counts,
                delivered_signals=persistence_summary.delivered_signals,
            )
            _send_digest_once(subject=digest.subject, body=digest.body)
            confirm_delivered_signals(persistence_summary.delivered_signals)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
