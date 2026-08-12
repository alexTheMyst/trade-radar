"""sentinel job — mechanical exit triggers on held positions.

Canonical job shape: runs row, heartbeat, ONE fetch_history batch, quotes for
triggered tickers only, cooldown partition, route -> deltas -> persist ->
digest -> validate -> send -> confirm. Cooldown-downgraded triggers persist
directly as [cooldown] MONITORING rows and never enter the router.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.data.finnhub_client import fetch_quotes
from signal_system.data.holdings import require_non_empty_holdings
from signal_system.data.yahoo_client import fetch_history
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import (
    PersistenceSummary,
    compute_score_deltas,
    confirm_delivered_signals,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
from signal_system.monitoring import heartbeat
from signal_system.models import Signal
from signal_system.router import route_signals
from signal_system.sentinel.exit_sentinel import (
    EXIT_SENTINEL_AGENT,
    HISTORY_DAYS,
    build_signal,
    compute_exit_triggers,
    dedupe_holdings,
    is_cooldown,
    prior_alert_ids,
    score_breaches,
    severity_for,
)
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _index_dates(df) -> list[str]:
    """Return the DataFrame's index as ISO date strings (cooldown lookback)."""
    dates: list[str] = []
    for idx in df.index:
        if hasattr(idx, "strftime"):
            dates.append(idx.strftime("%Y-%m-%d"))
        else:
            dates.append(str(idx)[:10])
    return dates


def _quote_snapshot(ticker: str, df) -> float | None:
    """Best-effort live close, falling back to the last candle close.

    The candle-close fallback guarantees a non-null snapshot whenever the
    trigger engine already computed from this df — a row without a snapshot is
    invisible to the review report and can never be measured.
    """
    quote = fetch_quotes([ticker]).get(ticker)
    close = quote.get("c") if quote else None
    if close and close > 0:
        return float(close)
    closes = df["Close"].values
    last = float(closes[-1]) if len(closes) else 0.0
    return last if last > 0 else None


def _cooldown_signal(signal: Signal) -> Signal:
    """Rewrite a routed signal as a [cooldown] MONITORING row.

    Keeps the original alert_id so a same-day re-run is INSERT-OR-IGNORE
    idempotent. get_signal_meta_by_alert_ids() excludes MONITORING rows, so a
    cooldown row never suppresses a later trigger by itself.
    """
    from dataclasses import replace

    return replace(
        signal,
        severity="MONITORING",
        title=f"[cooldown] {signal.title}",
        monitoring_reason="cooldown",
    )


def run() -> None:
    run_id = repository.insert_run("sentinel")
    try:
        with heartbeat.heartbeat(job="sentinel"):
            now_et = datetime.now(_ET)
            date_iso = now_et.date().isoformat()
            holdings = require_non_empty_holdings()
            positions = dedupe_holdings(holdings)
            tickers = [p.ticker for p in positions]

            history = fetch_history(tickers, days=HISTORY_DAYS)

            routable: list[Signal] = []
            cooldown_count = 0

            for position in positions:
                df = history.get(position.ticker)
                if df is None or df.empty:
                    logger.debug("No candle data for %r — skipping", position.ticker)
                    continue
                triggers = compute_exit_triggers(df, position.cost_basis)
                if triggers is None:
                    continue
                scored = score_breaches(triggers)
                if scored is None:
                    continue
                severity = severity_for(triggers)
                primary = scored[1]

                prior_ids = prior_alert_ids(
                    position.ticker, primary, date_iso, _index_dates(df)
                )
                prior_meta = repository.get_signal_meta_by_alert_ids(prior_ids)
                if is_cooldown(prior_meta, severity):
                    snapshot = _quote_snapshot(position.ticker, df)
                    if snapshot is None:
                        continue
                    signal = build_signal(
                        ticker=position.ticker,
                        triggers=triggers,
                        cost_basis=position.cost_basis,
                        date_iso=date_iso,
                        snapshot=snapshot,
                    )
                    inserted = repository.insert_signal(
                        _cooldown_signal(signal), routing_status="MONITORING"
                    )
                    if inserted:
                        cooldown_count += 1
                    else:
                        logger.info(
                            "cooldown signal deduped on alert_id: %s",
                            signal.alert_id[:12],
                        )
                    continue

                snapshot = _quote_snapshot(position.ticker, df)
                if snapshot is None:
                    logger.warning(
                        "No price snapshot for %r — sentinel trigger skipped",
                        position.ticker,
                    )
                    continue
                routable.append(
                    build_signal(
                        ticker=position.ticker,
                        triggers=triggers,
                        cost_basis=position.cost_basis,
                        date_iso=date_iso,
                        snapshot=snapshot,
                    )
                )

            routed = route_signals(routable)
            # Deltas MUST be computed before persistence (same invariant as the
            # other jobs) — persisting first would let get_previous_score() see
            # this run's own rows.
            score_deltas = compute_score_deltas(routed)
            persistence_summary: PersistenceSummary = persist_routed_signals(routed)

            status_counts = dict(persistence_summary.status_counts)
            status_counts["MONITORING"] = status_counts.get("MONITORING", 0) + cooldown_count
            monitoring_breakdown = {"cooldown": cooldown_count} if cooldown_count else None

            held_tickers = set(tickers)
            digest = render_digest(
                job_name="sentinel",
                scanned_tickers=len(tickers),
                routed_signals=routed,
                held_tickers=held_tickers,
                score_deltas=score_deltas,
                status_counts=status_counts,
                monitoring_breakdown=monitoring_breakdown,
            )
            db_counts = repository.count_signals_by_routing_status(
                now_et.date(), EXIT_SENTINEL_AGENT
            )
            db_monitoring_breakdown = repository.count_monitoring_by_reason(
                now_et.date(), EXIT_SENTINEL_AGENT
            )
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                db_counts=db_counts,
                delivered_signals=persistence_summary.delivered_signals,
                db_monitoring_breakdown=db_monitoring_breakdown,
            )
            telegram_sender.send_message(f"{digest.subject}\n\n{digest.body}")
            confirm_delivered_signals(persistence_summary.delivered_signals)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
