"""watchdog job — cross-job liveness monitor.

The system's core promise is "never miss a material thesis-relevant event."
A scheduled job that stops emitting success rows — or emits only failures —
was invisible: per-job healthchecks were unset, so every job pinged the shared
check and one later success masked an earlier failure. The watchdog reads the
`runs` table and flags any monitored job that is stale (no SUCCESS within its
SLA) or whose most recent run failed — including a stuck `running` row left
by a hard crash that never reached `update_run`. A dead job must be as loud
as a bad alert.

The watchdog wraps itself in `heartbeat(job="watchdog")` and writes its own
`runs` row, so its own death is caught by its dedicated health check
(HEALTHCHECKS_UUID_WATCHDOG). healthchecks.io watches the watchdog; the
watchdog watches everything else.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from signal_system.delivery.telegram_sender import send_message
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.state import repository

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# job_name -> max allowed hours since last SUCCESS before it is "stale".
# Jobs mapped to None are intentionally unmonitored (not yet on a schedule)
# and are skipped, not flagged.
EXPECTED_MAX_AGE_HOURS: dict[str, int | None] = {
    "daily-close": 30,  # weekdays 17:15 ET
    "news-morning": 30,  # weekdays 09:00 ET
    "discovery": 30,  # weekdays 10:30 ET
    "sentinel": 30,  # weekdays 17:00 ET
    "dispatch": 30,  # weekdays 17:40 ET — pooled INFO arbitration (Plan 06)
    "outcome-backfill": 30,  # weekdays 17:30 ET
    "telegram-poll": 26,  # every 15 min, 07:00-23:00 ET
    "review-report": None,  # cadence TBD — skip until scheduled
    "weekly-synthesis": 200,  # Mondays 08:00 ET -> ~8 days tolerance
    "backup": 200,  # Sundays 08:00 ET
}

# Weekday-only jobs: weekend hours don't count against their SLA, so a Friday
# success keeps Monday green without a false alarm (the 30h SLA means "30 hours
# of weekday runtime" for these jobs).
WEEKDAY_JOBS = frozenset(
    {
        "daily-close",
        "news-morning",
        "discovery",
        "sentinel",
        "dispatch",
        "outcome-backfill",
    }
)

# A CANDIDATE row older than this was never dispatched: the evening dispatch
# died or never ran, so staged INFO signals are silently going nowhere.
STALE_CANDIDATE_MAX_AGE = timedelta(hours=36)

# A PENDING row older than this is a delivered signal whose Telegram send was
# never confirmed (the job died between persist and confirm_delivered_signals).
# 3h is far beyond any legitimately in-flight send.
STALE_PENDING_MAX_AGE = timedelta(hours=3)

# A most-recent run still 'running' older than this is a hard crash (power
# loss, OOM, kill): insert_run() wrote 'running' but update_run() never ran.
# Every job finishes well under 30 minutes (Task Scheduler ExecutionTimeLimit),
# so 2h is far beyond any legitimately in-flight run at watchdog time.
STUCK_RUNNING_MAX_AGE = timedelta(hours=2)


def _weekend_hours_between(start: datetime, end: datetime) -> float:
    """Return hours in [start, end) that fall on a Saturday or Sunday (ET)."""
    total = 0.0
    day = start.astimezone(_ET).date()
    end_date = end.astimezone(_ET).date()
    while day <= end_date:
        if day.weekday() >= 5:
            day_start = datetime.combine(day, time.min, tzinfo=_ET)
            overlap_start = max(start, day_start)
            overlap_end = min(end, day_start + timedelta(days=1))
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def _humanize_age(age: timedelta) -> str:
    """Render a timedelta as a compact relative age ('3 days ago')."""
    total_hours = age.total_seconds() / 3600.0
    if total_hours >= 24:
        days = total_hours / 24.0
        if days.is_integer():
            return f"{int(days)} days ago"
        return f"{days:.1f} days ago"
    return f"{total_hours:.0f} hours ago"


def _format_et(dt: datetime) -> str:
    return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")


def check_jobs(now: datetime | None = None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (failing, stale) lists of (job, human-readable detail).

    Pure over repository state + EXPECTED_MAX_AGE_HOURS. *now* defaults to ET
    now. A job is:
      - "failing" when its MOST RECENT run has status 'failed', or has been
        stuck 'running' longer than STUCK_RUNNING_MAX_AGE (a hard crash) —
        even if an older success keeps the age-based SLA green;
      - "stale" when it has never succeeded, or when (now - last success,
        minus weekend hours for WEEKDAY_JOBS) exceeds its SLA. The stale
        detail shows wall-clock age; only the threshold uses the adjusted age.
    Jobs mapped to None in EXPECTED_MAX_AGE_HOURS are skipped entirely.
    """
    effective_now = now or datetime.now(_ET)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=_ET)
    else:
        effective_now = effective_now.astimezone(_ET)

    failing: list[tuple[str, str]] = []
    stale: list[tuple[str, str]] = []

    for job, max_age_hours in EXPECTED_MAX_AGE_HOURS.items():
        if max_age_hours is None:
            continue

        last_success = repository.get_latest_successful_run_datetime(job)
        if last_success is None:
            stale.append((job, "never succeeded"))
        else:
            wall_age = effective_now - last_success
            threshold_age = wall_age
            if job in WEEKDAY_JOBS:
                threshold_age -= timedelta(
                    hours=_weekend_hours_between(last_success, effective_now)
                )
            if threshold_age > timedelta(hours=max_age_hours):
                stale.append(
                    (job, f"last success {_humanize_age(wall_age)} (SLA {max_age_hours}h)")
                )

        last_run = repository.get_latest_run(job)
        if last_run:
            if last_run["status"] == "failed":
                failing.append(
                    (job, f"last run {_format_et(last_run['started_at'])} (failed)")
                )
            elif (
                last_run["status"] == "running"
                and effective_now - last_run["started_at"] > STUCK_RUNNING_MAX_AGE
            ):
                failing.append(
                    (job, f"stuck running since {_format_et(last_run['started_at'])}")
                )

    stale_cutoff = effective_now - STALE_CANDIDATE_MAX_AGE
    stale_candidates = repository.count_candidates_older_than(stale_cutoff)
    if stale_candidates:
        oldest = repository.get_oldest_candidate_timestamp()
        since = f" since {_format_et(oldest)}" if oldest else ""
        failing.append(
            (
                "dispatch",
                f"{stale_candidates} staged candidate(s) never dispatched{since}",
            )
        )

    # Delivered signals whose send was never confirmed: the alert itself never
    # reached the operator, so it must be surfaced as loudly as a dead job.
    pending_cutoff = effective_now - STALE_PENDING_MAX_AGE
    stale_pending = repository.count_pending_older_than(pending_cutoff)
    if stale_pending:
        oldest = repository.get_oldest_pending_timestamp()
        since = f" since {_format_et(oldest)}" if oldest else ""
        failing.append(
            (
                "dispatch",
                f"{stale_pending} delivered signal(s) stuck PENDING (send never confirmed){since}",
            )
        )

    # Telegram button messages that never reached the operator (Plan 12e1).
    failed_buttons = repository.list_telegram_messages_since(
        effective_now - timedelta(hours=24), status="send_failed"
    )
    if failed_buttons:
        failing.append(
            (
                "telegram-poll",
                f"{len(failed_buttons)} action-button message(s) failed to send in the last 24h",
            )
        )

    return failing, stale


def render_watchdog_alert(
    failing: list[tuple[str, str]], stale: list[tuple[str, str]]
) -> str:
    """Render the single, actionable Telegram message."""
    total = len(failing) + len(stale)
    lines = [f"⚠️ SIGNAL SYSTEM WATCHDOG — {total} job(s) need attention", ""]
    if failing:
        lines.append("FAILING (most recent run failed or never completed):")
        lines.extend(f"  • {job} — {detail}" for job, detail in failing)
        lines.append("")
    if stale:
        lines.append("STALE (no success within SLA):")
        lines.extend(f"  • {job} — {detail}" for job, detail in stale)
        lines.append("")
    lines.append("Investigate: uv run python -m signal_system <job>")
    return "\n".join(lines)


def run() -> None:
    run_id = repository.insert_run("watchdog")
    try:
        with heartbeat(job="watchdog"):
            failing, stale = check_jobs()
            if failing or stale:
                text = render_watchdog_alert(failing, stale)
                logger.warning("watchdog alert:\n%s", text)
                print(text)
                send_message(text)
            else:
                logger.info("watchdog: all monitored jobs healthy")
                print("watchdog: all monitored jobs healthy")
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
