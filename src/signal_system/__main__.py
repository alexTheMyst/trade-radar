import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from signal_system import redaction
from signal_system.state import repository
from signal_system.data.universe_builder import apply_merged_universe, diff_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
)

# finnhub-python carries the API key as a `token` query parameter, and both
# requests exception messages and tenacity's before_sleep_log retry hook embed
# the full URL. Without this, every transient Finnhub failure writes the live
# key into the job log. Attached to the root handlers so it covers every
# module's logger, for every command — not just preflight.
redaction.install_redaction_filter()

_ET = ZoneInfo("America/New_York")

# Job modules import `config`, which raises at import time on a missing secret.
# preflight has to survive exactly that condition — its whole purpose is to
# report a broken .env as an [A] row rather than an import traceback — so the
# job imports are deferred into _load_jobs() and only resolved when a job is
# actually about to run. Keying this off sys.argv at import time instead would
# make the module's shape depend on the argv of whatever process imported it.
JOB_NAMES = (
    "daily-close",
    "discovery",
    "news-morning",
    "outcome-backfill",
    "review-report",
    "weekly-synthesis",
    "backup",
    "sentinel",
    "watchdog",
    "dispatch",
    "telegram-poll",
)


def _load_jobs() -> dict:
    """Import the job entry points and return the name -> callable mapping.

    Deferred: importing these pulls in `config`, which raises on a missing
    secret. Callers that must work without config (preflight) never call this.
    """
    from signal_system.jobs.backup import run as run_backup
    from signal_system.jobs.daily_close import run as run_daily_close
    from signal_system.jobs.discovery import run as run_discovery
    from signal_system.jobs.news_morning import run as run_news_morning
    from signal_system.jobs.outcome_backfill import run as run_outcome_backfill
    from signal_system.jobs.review_report import run as run_review_report
    from signal_system.jobs.sentinel import run as run_sentinel
    from signal_system.jobs.watchdog import run as run_watchdog
    from signal_system.jobs.dispatch import run as run_dispatch
    from signal_system.jobs.telegram_poll import run as run_telegram_poll
    from signal_system.jobs.weekly_synthesis import run as run_weekly_synthesis

    jobs = {
        "daily-close": run_daily_close,
        "discovery": run_discovery,
        "news-morning": run_news_morning,
        "outcome-backfill": run_outcome_backfill,
        "review-report": run_review_report,
        "weekly-synthesis": run_weekly_synthesis,
        "backup": run_backup,
        "sentinel": run_sentinel,
        "watchdog": run_watchdog,
        "dispatch": run_dispatch,
        "telegram-poll": run_telegram_poll,
    }
    # JOB_NAMES is the import-safe registry used for dispatch and usage text;
    # drift between the two would let a job be advertised but not runnable.
    assert tuple(jobs) == JOB_NAMES, (
        f"JOB_NAMES {JOB_NAMES} out of sync with _load_jobs() {tuple(jobs)}"
    )
    return jobs


# Attributes that used to be module-level imports. They stay part of the
# module's public surface — resolved on access rather than at import — so
# deferring the imports does not change what callers can reach.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "backfill_benchmark": ("signal_system.jobs.outcome_backfill", "backfill_benchmark"),
    "run_classifier_eval": ("signal_system.evals.classifier_eval", "run_eval"),
}


def __getattr__(name: str):
    """Resolve deferred attributes on access (PEP 562).

    `JOBS` and the job-module helpers were module-level imports before
    preflight needed this module to import without `config`. Building them
    eagerly would defeat that; resolving them here keeps the public surface
    identical while the imports stay lazy.
    """
    if name == "JOBS":
        return _load_jobs()
    if name in _LAZY_ATTRS:
        module_path, attr = _LAZY_ATTRS[name]
        import importlib

        return getattr(importlib.import_module(module_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def _cmd_bulk_close(before: date, note: str) -> None:
    """Bulk-close unreviewed DELIVERED rows older than *before* (Plan 10 §10d.1)."""
    updated = repository.bulk_close_delivered_before(before.isoformat(), note)
    print(
        f"Bulk-closed {updated} unreviewed delivered signal(s) older than "
        f"{before.isoformat()} with note {note!r} (acted stays NULL)."
    )


_LOG_ACTION_USAGE = (
    "Usage: python -m signal_system log-action <prefix> --acted|--passed "
    "[--note \"...\"]  |  log-action --bulk-close-before YYYY-MM-DD --note \"...\""
)


def _parse_log_action(argv: list[str]) -> int:
    if not argv:
        print(_LOG_ACTION_USAGE)
        sys.exit(2)

    bulk_before: date | None = None
    user_note: str | None = None
    acted: bool | None = None
    prefix: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--acted":
            acted = True
        elif arg == "--passed":
            acted = False
        elif arg == "--note":
            if i + 1 >= len(argv):
                print("--note requires a value")
                sys.exit(2)
            user_note = argv[i + 1]
            i += 1
        elif arg == "--bulk-close-before":
            if i + 1 >= len(argv):
                print("--bulk-close-before requires a value (YYYY-MM-DD)")
                sys.exit(2)
            try:
                bulk_before = datetime.strptime(argv[i + 1], "%Y-%m-%d").date()
            except ValueError:
                print(f"Invalid --bulk-close-before value: {argv[i + 1]!r}")
                sys.exit(2)
            i += 1
        elif arg.startswith("-"):
            print(f"Unknown log-action argument: {arg!r}")
            sys.exit(2)
        elif prefix is None:
            prefix = arg
        else:
            print(f"Unexpected log-action argument: {arg!r}")
            sys.exit(2)
        i += 1

    if bulk_before is not None:
        if not user_note:
            print("--bulk-close-before requires a distinguishing --note")
            sys.exit(2)
        _cmd_bulk_close(bulk_before, user_note)
        return 0

    if prefix is None:
        print(_LOG_ACTION_USAGE)
        sys.exit(2)
    if acted is None:
        print("Must specify either --acted or --passed")
        sys.exit(2)

    _cmd_log_action(prefix, acted=acted, user_note=user_note)
    return 0


def _parse_replay_weights(argv: list[str]) -> int:
    """Parse replay-weights args and run the offline weight replay.

    No heartbeat, no runs row, no DB access — this is analysis tooling.
    """
    from signal_system.replay.weight_replay import parse_weights, run_replay_cli

    days = 180
    weight_specs: list[str] = []
    horizons: list[int] = [5, 20]
    gates_on = True

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days":
            if i + 1 >= len(argv):
                print("--days requires a value")
                sys.exit(2)
            try:
                days = int(argv[i + 1])
            except ValueError:
                print(f"Invalid --days value: {argv[i + 1]!r}")
                sys.exit(2)
            i += 2
        elif arg == "--weights":
            if i + 1 >= len(argv):
                print("--weights requires a value (e.g. 50/30/20)")
                sys.exit(2)
            try:
                parse_weights(argv[i + 1])
            except ValueError as exc:
                print(f"Invalid --weights: {exc}")
                sys.exit(2)
            weight_specs.append(argv[i + 1])
            i += 2
        elif arg == "--horizons":
            if i + 1 >= len(argv):
                print("--horizons requires a comma-separated list (e.g. 5,20)")
                sys.exit(2)
            try:
                horizons = [int(h) for h in argv[i + 1].split(",")]
            except ValueError:
                print(f"Invalid --horizons value: {argv[i + 1]!r}")
                sys.exit(2)
            i += 2
        elif arg == "--gates":
            if i + 1 >= len(argv):
                print("--gates requires on or off")
                sys.exit(2)
            value = argv[i + 1].lower()
            if value not in ("on", "off"):
                print(f"--gates must be 'on' or 'off', got {argv[i + 1]!r}")
                sys.exit(2)
            gates_on = value == "on"
            i += 2
        else:
            print(f"Unknown replay-weights argument: {arg!r}")
            sys.exit(2)

    if not weight_specs:
        weight_specs = ["50/30/20"]

    run_replay_cli(
        days=days,
        weight_specs=weight_specs,
        horizons=horizons,
        gates_on=gates_on,
    )
    return 0


if __name__ == "__main__":
    # Task Scheduler gives a cp1252 stdout; emoji in alert text would raise
    # UnicodeEncodeError from print() and fail the job.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8", errors="replace")

    # preflight is dispatched before init_db() and before any config-dependent
    # import, so it can diagnose a machine whose .env is broken.
    if len(sys.argv) >= 2 and sys.argv[1] == "preflight":
        from signal_system.preflight import run_preflight

        sys.exit(run_preflight(sys.argv[2:]))

    repository.init_db()
    if len(sys.argv) < 2:
        print("Usage: python -m signal_system <job>  |  "
              "list-signals [--days N]  |  log-action <prefix> --acted|--passed  |  "
              "log-action --bulk-close-before YYYY-MM-DD --note \"...\"  |  "
              "backfill-benchmark  |  replay-weights [--days N] [--weights A/B/C] [--horizons H1,H2] [--gates on|off]  |  "
              "eval-classifier  |  preflight")
        print(f"Available jobs: {list(JOB_NAMES)}")
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "list-signals":
        _parse_list_signals(sys.argv[2:])
    elif cmd == "build-universe":
        _cmd_build_universe(sys.argv[2:])
    elif cmd == "log-action":
        _parse_log_action(sys.argv[2:])
    elif cmd == "backfill-benchmark":
        from signal_system.jobs.outcome_backfill import backfill_benchmark

        written = backfill_benchmark()
        print(f"backfill-benchmark complete: {written} SPY benchmark rows written")
    elif cmd == "replay-weights":
        _parse_replay_weights(sys.argv[2:])
    elif cmd == "eval-classifier":
        from signal_system.evals.classifier_eval import run_eval as run_classifier_eval

        run_classifier_eval()
    elif cmd in JOB_NAMES:
        _load_jobs()[cmd]()
    else:
        print(f"Unknown command: {cmd!r}")
        print(f"Available jobs: {list(JOB_NAMES)}  |  list-signals  |  log-action  |  build-universe  |  backfill-benchmark  |  replay-weights  |  eval-classifier  |  preflight")
        sys.exit(2)
