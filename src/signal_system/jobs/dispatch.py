"""dispatch job — evening cross-agent INFO arbitration (Plan 06, Option D).

Agent jobs (news-morning 09:00, discovery 10:30, sentinel 17:00 ET) route
ACTION_REQUIRED immediately and stage every INFORMATIONAL signal as a
CANDIDATE. This job (weekdays 17:40 ET) pools the day's candidates from every
agent, routes them ONCE through route_signals() on the unified
competition_score() scale, and sends a single digest. The delivered set is
therefore a function of score, not of which job happened to run first.

Candidates left over from an earlier day (dispatch did not run) are expired
to SUPPRESSED with suppression_reason='expired' — stale INFO is not worth
sending, but the rows stay measurable by outcome-backfill — and the digest
says so loudly.

Same send discipline as the other jobs: persist outcomes with DELIVERED
written as PENDING, read counts back from the database, validate, send, then
flip PENDING → DELIVERED.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.data.holdings import held_ticker_set
from signal_system.delivery import telegram_sender
from signal_system.jobs.common import (
    RoutedSignal,
    confirm_delivered_signals,
    format_score,
    send_alert_buttons,
)
from signal_system.models import (
    DISCOVERY_AGENT,
    EXIT_SENTINEL_AGENT,
    NEWS_CLASSIFIER_AGENT,
    Signal,
    competition_score,
)
from signal_system.monitoring import heartbeat
from signal_system.router import (
    BUDGET_INFO,
    SENTINEL_RESERVED_INFO,
    dispatch_candidates,
    is_fresh_sentinel,
    select_reserved_sentinel,
)
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Every agent is always listed in the digest, even at zero, so "contributed
# nothing" and "contributed 68 that all lost" look different on the page.
AGENTS: tuple[str, ...] = (NEWS_CLASSIFIER_AGENT, DISCOVERY_AGENT, EXIT_SENTINEL_AGENT)

# Suppressed candidates listed in the digest body (the rest are counted only).
MAX_SUPPRESSED_LISTED = 10

EXPIRED_REASON = "expired"


def _now_et() -> datetime:
    return datetime.now(_ET)


def _agent_counts(signals: list[Signal]) -> str:
    counts = Counter(s.agent for s in signals)
    return ", ".join(f"{agent} {counts.get(agent, 0)}" for agent in AGENTS)


def _line(signal: Signal, demoted_from: str | None, held: set[str]) -> str:
    ticker = signal.ticker or "MACRO"
    severity = signal.severity
    if demoted_from:
        severity += f" (demoted from {demoted_from})"
    line = (
        f"{ticker} — {format_score(signal.agent, signal.score)} "
        f"[{competition_score(signal):.0f}] — {signal.agent} — {severity}"
    )
    if ticker in held:
        line += " — HELD"
    return line


def render_dispatch_digest(
    *,
    routed: list[RoutedSignal],
    expired: list[Signal],
    held_tickers: set[str],
    reserved_label: str,
    shadow_logged: int = 0,
    unreviewed: int | None = None,
    sentinel_suppressed: list[tuple[Signal, str | None, str | None]] | None = None,
) -> tuple[str, str]:
    """Return (subject, body) for the evening dispatch digest."""
    pooled = [s for s, _rs, _dmf, _r in routed]
    delivered = [(s, dmf) for s, rs, dmf, _r in routed if rs == "DELIVERED"]
    suppressed = [(s, dmf, r) for s, rs, dmf, r in routed if rs == "SUPPRESSED"]
    reasons = Counter(r or "unknown" for _s, _dmf, r in suppressed)
    if sentinel_suppressed is None:
        sentinel_suppressed = [
            (s, dmf, r) for s, dmf, r in suppressed if s.agent == EXIT_SENTINEL_AGENT
        ]

    lines = [
        f"Candidates pooled: {len(pooled)} ({_agent_counts(pooled)})",
        f"Delivered: {len(delivered)} ({_agent_counts([s for s, _ in delivered])})",
        f"Suppressed: {len(suppressed)} "
        f"(outscored {reasons.get('outscored', 0)}, "
        f"cap_exhausted {reasons.get('cap_exhausted', 0)})",
    ]
    if shadow_logged:
        lines.append(f"Discovery (shadow): {shadow_logged} logged today")
    if expired:
        lines.append(
            f"⚠️ Expired undispatched candidates from earlier days: {len(expired)} "
            f"({_agent_counts(expired)}) — a dispatch run was missed"
        )

    if delivered:
        lines += ["", "Delivered Alerts", "----------------"]
        for signal, demoted_from in delivered:
            lines.append(_line(signal, demoted_from, held_tickers))
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines += ["", f"Pooled {len(pooled)} candidates, 0 alerts"]

    if suppressed:
        ranked = sorted(
            suppressed, key=lambda item: (-competition_score(item[0]), item[0].ticker or "")
        )
        header = f"Top suppressed ({min(len(ranked), MAX_SUPPRESSED_LISTED)} of {len(ranked)})"
        lines += ["", header, "-" * len(header)]
        for signal, demoted_from, reason in ranked[:MAX_SUPPRESSED_LISTED]:
            lines.append(f"{_line(signal, demoted_from, held_tickers)} — {reason}")

    # Plan 12a4: never let a suppressed held-position exit disappear behind the
    # top-N suppressed list. Every one is named, every day.
    exit_header = f"Held-position exits not delivered ({len(sentinel_suppressed)})"
    lines += ["", exit_header, "-" * len(exit_header)]
    for signal, demoted_from, reason in sentinel_suppressed:
        lines.append(f"{_line(signal, demoted_from, held_tickers)} — {reason}")
    lines.append(reserved_label)
    if unreviewed is not None:
        lines.append(f"Unreviewed this week: {unreviewed}")

    alert_label = "alert" if len(delivered) == 1 else "alerts"
    subject = f"Dispatch Digest — {len(delivered)} {alert_label}"
    return subject, "\n".join(lines).strip()


def validate_dispatch(
    *,
    routed: list[RoutedSignal],
    db_counts: dict[str, int],
    body: str,
    sentinel_suppressed_db: int | None = None,
    reserved: Signal | None = None,
) -> None:
    """Fail closed if the digest diverges from what the database now holds."""
    expected = Counter(rs for _s, rs, _dmf, _r in routed)
    for status in ("DELIVERED", "SUPPRESSED"):
        if db_counts.get(status, 0) != expected.get(status, 0):
            raise RuntimeError(
                f"Dispatch {status} count mismatch: db={db_counts} routed={dict(expected)}"
            )
    if db_counts.get("CANDIDATE", 0):
        raise RuntimeError(f"Dispatch left rows in CANDIDATE: db={db_counts}")
    delivered = [s for s, rs, _dmf, _r in routed if rs == "DELIVERED"]
    if len(delivered) > BUDGET_INFO:
        raise RuntimeError(f"Dispatch delivered {len(delivered)} > INFO budget {BUDGET_INFO}")
    if reserved is not None:
        reserved_delivered = sum(
            1 for s in delivered if s.alert_id == reserved.alert_id
        )
        if reserved_delivered != SENTINEL_RESERVED_INFO:
            raise RuntimeError(
                f"Reserved sentinel slot delivered {reserved_delivered} alert(s) "
                f"but exactly {SENTINEL_RESERVED_INFO} was expected"
            )
    routed_sentinel_suppressed = sum(
        1
        for s, rs, _dmf, _r in routed
        if rs == "SUPPRESSED" and s.agent == EXIT_SENTINEL_AGENT
    )
    expected_sentinel_suppressed = (
        sentinel_suppressed_db
        if sentinel_suppressed_db is not None
        else routed_sentinel_suppressed
    )
    exit_header = f"Held-position exits not delivered ({expected_sentinel_suppressed})"
    if exit_header not in body:
        raise RuntimeError(
            "Dispatch digest omits a suppressed sentinel candidate: "
            f"expected section {exit_header!r}"
        )
    if "Sentinel reserved slot:" not in body:
        raise RuntimeError("Dispatch digest missing the sentinel reserved-slot line")
    if not delivered and f"Pooled {len(routed)} candidates, 0 alerts" not in body:
        raise RuntimeError("Dispatch digest missing zero-alert confirmation")
    for signal in delivered:
        if signal.title not in body:
            raise RuntimeError(f"Dispatch digest missing delivered alert {signal.alert_id}")


def _unreviewed_rows(
    now_et: datetime, *, exclude_alert_ids: set[str] | None = None
) -> list[dict]:
    """Unreviewed DELIVERED rows from the past week, excluding this run's alerts."""
    excluded = exclude_alert_ids or set()
    return [
        row
        for row in repository.list_unacted_delivered_since(
            now_et - timedelta(days=7), limit=1000
        )
        if row["alert_id"] not in excluded
    ]


def _send_review_prompts(
    now_et: datetime, *, exclude_alert_ids: set[str] | None = None
) -> int:
    """Friday re-prompt for up to 10 unreviewed delivered signals (Plan 12e4).

    Alerts delivered seconds earlier in this same dispatch run are excluded:
    they already have their own action buttons, and re-prompting them would
    both duplicate messages and consume the 10-prompt limit.
    """
    rows = _unreviewed_rows(now_et, exclude_alert_ids=exclude_alert_ids)[:10]
    already = {
        row["alert_id"]
        for row in repository.list_telegram_messages_since(
            now_et - timedelta(days=1), status="sent"
        )
        if row["kind"] == "review_prompt"
    }
    sent = 0
    for row in rows:
        if row["alert_id"] in already:
            continue
        signal = Signal(
            ticker=row["ticker"],
            score=row["score"],
            severity=row["severity"],
            agent=row["agent"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            alert_id=row["alert_id"],
            title=row["title"],
        )
        try:
            message_id = telegram_sender.send_alert_with_buttons(
                signal, prefix="Review: "
            )
        except Exception:
            logger.warning(
                "Review prompt failed for %s", signal.alert_id[:12], exc_info=True
            )
            repository.insert_telegram_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                message_id=None,
                alert_id=signal.alert_id,
                kind="review_prompt",
                status="send_failed",
            )
        else:
            status = "sent" if message_id is not None else "send_failed"
            repository.insert_telegram_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                message_id=message_id,
                alert_id=signal.alert_id,
                kind="review_prompt",
                status=status,
            )
            sent += 1 if status == "sent" else 0
    return sent


def run() -> None:
    run_id = repository.insert_run("dispatch")
    try:
        with heartbeat.heartbeat(job="dispatch"):
            now_et = _now_et()
            date_iso = now_et.date().isoformat()

            expired_pairs = repository.list_candidates_before_date(date_iso)
            expired = [s for s, _ in expired_pairs]
            if expired_pairs:
                updated = repository.mark_candidates_routed(
                    [(s.alert_id, "SUPPRESSED", dmf, EXPIRED_REASON) for s, dmf in expired_pairs]
                )
                if updated != len(expired_pairs):
                    raise RuntimeError(
                        f"Expired {updated} of {len(expired_pairs)} stale candidates"
                    )
                logger.warning("dispatch expired %d stale candidates", len(expired))

            held_tickers = held_ticker_set(fallback_to_core=True)
            candidates = repository.list_candidates_for_date(date_iso)
            info_remaining = max(
                0, BUDGET_INFO - repository.count_delivered_today().get("INFORMATIONAL", 0)
            )
            routed = dispatch_candidates(candidates, held_tickers=held_tickers)
            updated = repository.mark_candidates_routed(
                [
                    (s.alert_id, "PENDING" if rs == "DELIVERED" else rs, dmf, reason)
                    for s, rs, dmf, reason in routed
                ]
            )
            if updated != len(routed):
                raise RuntimeError(
                    f"Persisted {updated} of {len(routed)} dispatch outcomes "
                    "(rows left CANDIDATE — a concurrent dispatch?)"
                )

            delivered = [
                s for s, rs, _dmf, _r in routed if rs == "DELIVERED"
            ]
            reserved_pair = select_reserved_sentinel(candidates, info_remaining)
            reserved = reserved_pair[0] if reserved_pair is not None else None
            reserved_label = (
                f"Sentinel reserved slot: {reserved.ticker}"
                if reserved is not None
                else (
                    "Sentinel reserved slot: unused (INFO budget exhausted)"
                    if info_remaining == 0
                    and any(is_fresh_sentinel(s) for s, _ in candidates)
                    else "Sentinel reserved slot: unused (no fresh trigger)"
                )
            )
            shadow_logged = (
                repository.count_shadow_signals_for_date(date_iso)
                if config.DISCOVERY_MODE == "shadow"
                else 0
            )
            current_alert_ids = {s.alert_id for s in delivered}
            friday = now_et.weekday() == 4
            unreviewed = (
                len(
                    _unreviewed_rows(
                        now_et, exclude_alert_ids=current_alert_ids
                    )
                )
                if friday
                else None
            )
            # Read the sentinel-suppressed set from the database, not just this
            # run's routed rows: a failed-send rerun sees rows suppressed by the
            # earlier run and must still render/validate the same section.
            sentinel_suppressed_rows = repository.list_suppressed_for_date(
                date_iso, EXIT_SENTINEL_AGENT
            )
            subject, body = render_dispatch_digest(
                routed=routed,
                expired=expired,
                held_tickers=held_tickers,
                reserved_label=reserved_label,
                shadow_logged=shadow_logged,
                unreviewed=unreviewed,
                sentinel_suppressed=sentinel_suppressed_rows,
            )
            db_counts = repository.count_routing_status_for_alert_ids(
                [s.alert_id for s, _rs, _dmf, _r in routed]
            )
            validate_dispatch(
                routed=routed,
                db_counts=db_counts,
                body=body,
                sentinel_suppressed_db=len(sentinel_suppressed_rows),
                reserved=reserved,
            )

            telegram_sender.send_message(f"{subject}\n\n{body}")
            confirm_delivered_signals(delivered)
            send_alert_buttons(delivered, held_tickers=held_tickers)
            if friday:
                _send_review_prompts(
                    now_et, exclude_alert_ids=current_alert_ids
                )
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
