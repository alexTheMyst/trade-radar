from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.classifier.news_classifier import _headline_dedup_key, classify_headlines
from signal_system.data.finnhub_client import fetch_company_news
from signal_system.data.thesis_loader import load_thesis
from signal_system.data.universe import get_core_holdings, get_todays_universe
from signal_system.delivery import email_sender
from signal_system.jobs.common import (
    PersistenceSummary,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
from signal_system.models import Signal, compute_alert_id
from signal_system.monitoring import heartbeat
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
    thesis_version_hash: str,
) -> Signal:
    rule = f"volume_cap:{_headline_dedup_key(ticker, headline)[:16]}"
    return Signal(
        ticker=ticker,
        score=None,
        severity="MONITORING",
        agent="news_morning",
        timestamp=headline_dt,
        alert_id=compute_alert_id(ticker, headline_dt.date().isoformat(), rule, "news_morning"),
        title=f"[volume_cap] {headline[:120]}",
        body="Headline skipped because deduped news exceeded the newest-50 volume cap.",
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
        dedup_key = _headline_dedup_key(ticker, headline)
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
        ):
            if signal.severity == "MONITORING":
                _persist_monitoring_signal(signal)
                monitoring_count += 1
            else:
                routable.append(signal)

    return routable, monitoring_count


def _send_digest_once(*, subject: str, body: str) -> None:
    email_sender.send_email(subject=subject, body=body)
    if hasattr(email_sender.send_email, "call_count") and email_sender.send_email.call_count != 1:
        raise RuntimeError("Digest email must be sent exactly once")


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
            tickers = get_core_holdings()

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
                        thesis_version_hash=thesis_version_hash,
                    )
                )

            routable, classifier_monitoring_count = _classify_kept_headlines(
                kept_items,
                thesis=thesis,
                thesis_version_hash=thesis_version_hash,
            )
            persistence_summary: PersistenceSummary = persist_routed_signals(route_signals(routable))
            status_counts = dict(persistence_summary.status_counts)
            status_counts["MONITORING"] += classifier_monitoring_count + len(overflow_items)

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
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
