import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system.state import repository
from signal_system.jobs.daily_close import run as run_daily_close
from signal_system.jobs.discovery import run as run_discovery
from signal_system.jobs.news_morning import run as run_news_morning
from signal_system.jobs.advisor import run as run_advisor, advise_ticker
from signal_system.jobs.outcome_backfill import (
    run as run_outcome_backfill,
    backfill_benchmark,
)
from signal_system.jobs.review_report import run as run_review_report
from signal_system.jobs.weekly_synthesis import run as run_weekly_synthesis
from signal_system.jobs.backup import run as run_backup
from signal_system.data.universe_builder import apply_merged_universe, diff_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
)

_ET = ZoneInfo("America/New_York")

JOBS = {
    "daily-close": run_daily_close,
    "discovery": run_discovery,
    "news-morning": run_news_morning,
    "advisor": run_advisor,
    "outcome-backfill": run_outcome_backfill,
    "review-report": run_review_report,
    "weekly-synthesis": run_weekly_synthesis,
    "backup": run_backup,
}


def _cmd_list_signals(days: int = 14, *, now: datetime | None = None) -> None:
    """Print recent DELIVERED signals. No heartbeat, no runs row."""
    effective_now = now or datetime.now(_ET)
    rows = repository.list_recent_delivered_signals(days=days, now=effective_now)
    if not rows:
        print(f"No delivered signals in the last {days} days.")
        return
    for row in rows:
        acted = "ACTED" if row.get("acted") == 1 else ("PASSED" if row.get("acted") == 0 else "UNREVIEWED")
        ticker = row.get("ticker") or "MACRO"
        print(
            f"{row['timestamp'][:10]} {ticker:>6} [{row['agent']}] "
            f"{row['severity']:<15} {row['alert_id'][:8]} {acted} {row['title']}"
        )


def _cmd_log_action(
    prefix: str, *, acted: bool, user_note: str | None = None
) -> None:
    """Record an acted/passed decision on signals matching *prefix*.

    Ambiguity guard: 0 matches or >1 matches → print guidance, exit 2.
    """
    try:
        matches = repository.find_signals_by_alert_prefix(prefix)
    except ValueError as exc:
        print(f"Invalid prefix: {exc}")
        sys.exit(2)

    if len(matches) == 0:
        print(f"No signals match alert_id prefix {prefix!r}. Use list-signals to see recent alert IDs.")
        sys.exit(2)
    if len(matches) > 1:
        print(f"Ambiguous prefix {prefix!r}: {len(matches)} signals match:")
        for m in matches:
            print(f"  {m['alert_id']} {m['title']}")
        sys.exit(2)

    target = matches[0]
    repository.update_signal_acted(target["alert_id"], acted=acted, user_note=user_note)
    verb = "ACTED" if acted else "PASSED"
    note = f" note={user_note!r}" if user_note else ""
    print(f"Logged {verb} for {target['alert_id']} ({target['title']}){note}")


def _parse_list_signals(argv: list[str]) -> int:
    days = 14
    i = 0
    while i < len(argv):
        if argv[i] == "--days":
            if i + 1 >= len(argv):
                print("Usage: python -m signal_system list-signals [--days N]")
                sys.exit(2)
            try:
                days = int(argv[i + 1])
            except ValueError:
                print(f"Invalid --days value: {argv[i + 1]!r}")
                sys.exit(2)
            i += 2
        else:
            print(f"Unknown list-signals argument: {argv[i]!r}")
            sys.exit(2)
    _cmd_list_signals(days=days)
    return 0


def _cmd_build_universe(argv: list[str]) -> int:
    """Dry-run by default; --apply writes the merged universe atomically."""
    apply = "--apply" in argv
    lines = diff_universe()
    if lines:
        print("Universe build diff:")
        for line in lines:
            print(f"  {line}")
    else:
        print("Universe build: no changes vs current universe.csv")
    if apply:
        n = apply_merged_universe(dry_run=False)
        print(f"Applied: {n} rows written to universe.csv (backup at universe.csv.bak)")
    else:
        n = apply_merged_universe(dry_run=True)
        print(f"Dry-run: {n} rows would be written. Re-run with --apply to commit.")
    return 0


def _parse_log_action(argv: list[str]) -> int:
    if not argv:
        print("Usage: python -m signal_system log-action <prefix> --acted|--passed [--note \"...\"]")
        sys.exit(2)
    prefix = argv[0]
    acted: bool | None = None
    user_note: str | None = None
    i = 1
    while i < len(argv):
        if argv[i] == "--acted":
            acted = True
        elif argv[i] == "--passed":
            acted = False
        elif argv[i] == "--note":
            if i + 1 >= len(argv):
                print("--note requires a value")
                sys.exit(2)
            user_note = argv[i + 1]
            i += 1
        else:
            print(f"Unknown log-action argument: {argv[i]!r}")
            sys.exit(2)
        i += 1

    if acted is None:
        print("Must specify either --acted or --passed")
        sys.exit(2)

    _cmd_log_action(prefix, acted=acted, user_note=user_note)
    return 0


if __name__ == "__main__":
    repository.init_db()
    if len(sys.argv) < 2:
        print("Usage: python -m signal_system <job>  |  advise <TICKER>  |  "
              "list-signals [--days N]  |  log-action <prefix> --acted|--passed  |  backfill-benchmark")
        print(f"Available jobs: {list(JOBS)}")
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "advise":
        if len(sys.argv) < 3:
            print("Usage: python -m signal_system advise <TICKER>")
            sys.exit(2)
        advise_ticker(sys.argv[2])
    elif cmd == "list-signals":
        _parse_list_signals(sys.argv[2:])
    elif cmd == "build-universe":
        _cmd_build_universe(sys.argv[2:])
    elif cmd == "log-action":
        _parse_log_action(sys.argv[2:])
    elif cmd == "backfill-benchmark":
        written = backfill_benchmark()
        print(f"backfill-benchmark complete: {written} SPY benchmark rows written")
    elif cmd in JOBS:
        JOBS[cmd]()
    else:
        print(f"Unknown command: {cmd!r}")
        print(f"Available jobs: {list(JOBS)}  |  advise <TICKER>  |  list-signals  |  log-action  |  build-universe  |  backfill-benchmark")
        sys.exit(2)
