"""Advisor Agent scheduled job and on-demand advise command.

run()           -- scheduled daily job; heartbeat-wrapped, persists advice, sends Telegram digest.
advise_ticker() -- on-demand; prints verdict to stdout only; no heartbeat, no DB write.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.advisor.advisor_agent import (
    HISTORY_DAYS as _HISTORY_DAYS,
    _compute_sma,
    compute_news_net,
    has_thesis_break,
    produce_advice,
)
from signal_system.classifier.news_classifier import NEWS_CLASSIFIER_AGENT
from signal_system.advisor.rationale import generate_rationale
from signal_system.advisor.verdict_engine import NEWS_LOOKBACK_DAYS, compute_verdict
from signal_system.data.holdings import EmptyHoldingsError, require_non_empty_holdings
from signal_system.data.thesis_loader import load_thesis
from signal_system.data.yahoo_client import fetch_history
from signal_system.delivery.telegram_sender import send_message
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.state import repository

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _finnhub_close(ticker: str) -> float | None:
    """Wrap finnhub fetch_quote to return just the close price."""
    from signal_system.data.finnhub_client import fetch_quote
    quote = fetch_quote(ticker)
    if not quote:
        return None
    close = quote.get("c")
    return float(close) if close and close > 0 else None


def _thesis_text(thesis) -> str:
    return "\n".join(f"{p.name}: {p.description}" for p in thesis.pillars)


def _render_digest(advice_rows: list[dict], shadow_mode: bool) -> str:
    today = datetime.now(_ET).date().isoformat()
    lines = [f"Advisory Digest -- {today}"]
    if shadow_mode:
        lines.append("SHADOW MODE -- log only, not actionable")
    lines.append("")

    held = [r for r in advice_rows if r.get("held")]
    new_buys = [r for r in advice_rows if not r.get("held") and r.get("verdict") == "BUY"]

    if held:
        lines.append("-- Held Positions --")
        for row in held:
            flag_str = f" [{row['flags']}]" if row.get("flags") else ""
            lines.append(
                f"{row['ticker']} ({row['account']}): {row['verdict']} "
                f"(conf {row['confidence']:.0%}){flag_str}"
            )
            if row.get("rationale"):
                lines.append(f"  {row['rationale']}")
        lines.append("")

    if new_buys:
        lines.append("-- New Buy Candidates --")
        for row in new_buys:
            lines.append(f"{row['ticker']}: BUY (conf {row['confidence']:.0%})")
            if row.get("rationale"):
                lines.append(f"  {row['rationale']}")
        lines.append("")

    if not held and not new_buys:
        lines.append("No verdicts produced -- check holdings.csv and signals DB.")

    lines.append(f"Evaluated {len(held)} holdings, {len(new_buys)} new-buy candidates.")
    return "\n".join(lines)


def run() -> None:
    """Entry point for scheduled advisor job (heartbeat-wrapped)."""
    run_id = repository.insert_run("advisor")
    try:
        with heartbeat():
            thesis, thesis_version_hash = load_thesis(config.THESIS_PATH)
            holdings = require_non_empty_holdings()

            advice_rows = produce_advice(
                holdings=holdings,
                fetch_history=fetch_history,
                fetch_quote=_finnhub_close,
                get_recent_signals=lambda ticker, since: repository.get_recent_signals(
                ticker, since, agent=NEWS_CLASSIFIER_AGENT
            ),
                get_discovery_candidates=lambda since, excl: repository.get_delivered_discovery_signals(
                    since, excluded_tickers=excl
                ),
                thesis_text=_thesis_text(thesis),
                thesis_version_hash=thesis_version_hash,
                run_id=run_id,
                shadow_mode=config.ADVISOR_SHADOW_MODE,
            )

            for row in advice_rows:
                repository.insert_advice(row)

            digest = _render_digest(advice_rows, config.ADVISOR_SHADOW_MODE)
            send_message(digest)
            repository.update_run(run_id, "success")

    except Exception:
        repository.update_run(run_id, "failed")
        raise


def advise_ticker(ticker: str) -> None:
    """On-demand single-ticker advisor.

    Prints verdict to stdout. No heartbeat, no Telegram, no advice table write.
    Ad-hoc lookups must not pollute the measured verdict set.
    """
    ticker = ticker.upper()

    thesis, _ = load_thesis(config.THESIS_PATH)

    try:
        all_holdings = require_non_empty_holdings()
    except EmptyHoldingsError:
        all_holdings = []
    holding = next((h for h in all_holdings if h.ticker == ticker), None)
    held = holding is not None

    history_map = fetch_history([ticker], _HISTORY_DAYS)
    df = history_map.get(ticker)

    if df is None or len(df) < 201:
        n = len(df) if df is not None else 0
        print(f"{ticker}: NO DATA -- insufficient price history ({n} rows, need 201+)")
        return

    closes: list[float] = df["Close"].tolist()
    sma50 = _compute_sma(closes, 50)
    sma200 = _compute_sma(closes, 200)
    if sma50 is None or sma200 is None:
        print(f"{ticker}: NO DATA -- could not compute SMAs")
        return

    price = _finnhub_close(ticker) or closes[-1]
    close_high_20d = max(closes[-20:])
    close_low_20d = min(closes[-20:])

    since = datetime.now(_ET).date() - timedelta(days=NEWS_LOOKBACK_DAYS)
    news_sigs = repository.get_recent_signals(ticker, since, agent=NEWS_CLASSIFIER_AGENT)
    net = compute_news_net(news_sigs)
    t_break = has_thesis_break(news_sigs)

    result = compute_verdict(
        price=price, sma50=sma50, sma200=sma200, news_net=net,
        close_high_20d=close_high_20d, close_low_20d=close_low_20d,
        cost_basis=holding.cost_basis if holding else None,
        held=held, thesis_break=t_break,
    )

    thesis_text = _thesis_text(thesis)
    rationale_text, rationale_source = generate_rationale(
        ticker=ticker, verdict=result.verdict,
        mom_axis=result.mom_axis, news_axis=result.news_axis,
        factors=result.factors, flags=list(result.flags),
        thesis_text=thesis_text, job="advise",
    )

    held_str = "held" if held else "not held"
    print(f"{ticker} ({held_str}): {result.verdict}  confidence={result.confidence:.0%}")
    print(f"  trend={result.mom_axis}  news={result.news_axis}  price={price:.2f}  sma50={sma50:.2f}  sma200={sma200:.2f}")
    if result.flags:
        print(f"  flags: {', '.join(result.flags)}")
    print(f"  {rationale_text}")
    if rationale_source == "template":
        print("  [rationale: template fallback -- Claude unavailable]")
