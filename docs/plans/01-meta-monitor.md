# Plan 01 — Meta-Monitor Job (`watchdog`)

**Type:** Add · **Priority:** P0 · **Est. effort:** ~0.5 day

## Problem

The system's core promise is "never miss a material thesis-relevant event." It broke that
promise silently: `news-morning` failed every scheduled run from 2026-06-08 to 2026-08-09
(~42 consecutive failures) and nothing told the operator. Two failure modes combined:

1. **No cross-job liveness check.** Each job only knows about itself. A job that stops
   emitting `runs` rows — or emits only `failed` — is invisible.
2. **Health-check masking.** Per-job `HEALTHCHECKS_UUID_*` were never set in `.env`, so all
   jobs pinged the shared check. `news-morning` pinged `/fail` at 09:00, then `daily-close`
   and `discovery` pinged `/success` later the same day and reset it. The shared check
   looked green every evening.

A dead job must be as loud as a bad alert. Right now it is completely silent.

## Goal

A small scheduled job — `watchdog` — that reads the `runs` table, detects any scheduled
job that is overdue for a success, and sends a single Telegram message when something is
wrong. It is the system's answer to "who watches the watchmen."

## Design

### Expected-cadence table

The watchdog needs to know how often each job *should* succeed. Encode this as a static
map in the new module (not the DB — it changes only with code):

```python
# jobs/watchdog.py
# job_name -> max allowed hours since last SUCCESS before it is "stale"
EXPECTED_MAX_AGE_HOURS = {
    "daily-close":      30,   # weekdays 17:15 ET → allow a weekend gap via weekday logic
    "news-morning":     30,   # weekdays 09:00 ET
    "discovery":        30,   # weekdays 10:30 ET
    "sentinel":         30,   # weekdays 17:00 ET
    "review-report":    None, # cadence TBD — skip until scheduled
    "weekly-synthesis": 200,  # Mondays 08:00 ET → ~8 days tolerance
    "backup":           200,  # Sundays 08:00 ET
    "outcome-backfill": None, # skip until scheduled
}
```

Jobs mapped to `None` are intentionally unmonitored (not yet on a schedule — see Plan 03/
open items) and are skipped, not flagged.

### Weekend / market-day awareness

Weekday jobs must not false-alarm on Monday morning about "no success since Friday."
Two acceptable approaches — pick one:

- **Simple (recommended v1):** widen `EXPECTED_MAX_AGE_HOURS` to 30h for daily jobs and run
  the watchdog only on weekdays; a Friday success keeps Monday green until the next run.
  Over a weekend, Saturday/Sunday have no watchdog run, so no false alarm.
- **Precise (v2 if needed):** compute "last expected fire time" from a per-job cron-like
  spec using `America/New_York` and the US market calendar, and compare last success to
  that. More correct, more code. Defer unless v1 proves noisy.

### Algorithm

```
for job, max_age in EXPECTED_MAX_AGE_HOURS.items():
    if max_age is None: continue
    last_success = repository.get_latest_successful_run_datetime(job)   # NEW repo fn
    if last_success is None:
        stale.append((job, "never succeeded"))
    elif (now_et - last_success) > timedelta(hours=max_age):
        stale.append((job, f"last success {humanize(age)} ago"))
    # Also flag a job whose MOST RECENT run is 'failed' even if a stale success exists:
    last_run = repository.get_latest_run(job)                           # NEW repo fn
    if last_run and last_run.status == "failed":
        failing.append((job, last_run.started_at))

if stale or failing:
    telegram_sender.send_message(render_watchdog_alert(stale, failing))
```

The watchdog itself wraps in `heartbeat.heartbeat(job="watchdog")` and writes its own
`runs` row, so the watchdog's own death is caught by its dedicated health check
(`HEALTHCHECKS_UUID_WATCHDOG`). This is the base case of the recursion: healthchecks.io
watches the watchdog; the watchdog watches everything else.

### Message format (Telegram)

```
⚠️ SIGNAL SYSTEM WATCHDOG — 2 job(s) need attention

FAILING (most recent run failed):
  • news-morning — last run 2026-08-09 09:00 ET (failed)

STALE (no success within SLA):
  • sentinel — last success 3 days ago (SLA 30h)

Investigate: uv run python -m signal_system <job>
```

Keep it one message, opinionated and actionable — same philosophy as `morning-note`.

## Files touched

| File | Change |
|------|--------|
| `src/signal_system/jobs/watchdog.py` | **New.** `run()` entry, cadence map, render fn. |
| `src/signal_system/state/repository.py` | **New fns:** `get_latest_successful_run_datetime(job) -> datetime | None` and `get_latest_run(job) -> Run | None` (return `started_at`, `status`). Reuse the pattern of existing `get_latest_successful_run_date`. |
| `src/signal_system/__main__.py` | Register `"watchdog": run_watchdog` in `JOBS`. |
| `src/signal_system/config.py` | No change (per-job UUID already resolved via `healthchecks_uuid_for("watchdog")`). |
| `.env.example` | Add commented `HEALTHCHECKS_UUID_WATCHDOG=`. |
| `ops/tasks/watchdog.xml` | **New** Task Scheduler definition. |
| `ops/windows-task-scheduler.md` | Document the new task. |
| `tests/test_watchdog.py` | **New.** |

## Repository additions (exact signatures)

```python
def get_latest_successful_run_datetime(job: str) -> datetime | None:
    """Timezone-aware ET datetime of the most recent status='success' run, or None."""

def get_latest_run(job: str) -> dict | None:
    """Most recent run row for a job: {'started_at': datetime, 'status': str} or None."""
```

Both are thin `SELECT ... ORDER BY started_at DESC LIMIT 1` queries inside `repository.py`.

## Scheduling

- **Cadence:** weekdays, 18:00 ET — after the last daily job (`sentinel` at 17:00,
  `daily-close` at 17:15) has had time to finish. A second run at 09:30 ET catches an
  overnight `news-morning`/`discovery` death before the trading day.
- **Rollout order for the health check (critical, per CLAUDE.md):**
  1. Create the `watchdog` check in healthchecks.io **first**.
  2. Set `HEALTHCHECKS_UUID_WATCHDOG` in `.env`.
  3. Enable the Task Scheduler task.
  Pinging a nonexistent check 404s silently.

## Testing

- `test_watchdog_flags_stale_job` — seed `runs` with an old success, assert it appears in
  the stale list and `telegram_sender.send_message` is called once (mock the sender).
- `test_watchdog_flags_failing_last_run` — most recent run is `failed` though an older
  success exists → flagged in "failing".
- `test_watchdog_silent_when_all_healthy` — all jobs recently succeeded → `send_message`
  **not** called.
- `test_watchdog_skips_none_cadence_jobs` — jobs mapped to `None` never flagged.
- `test_watchdog_never_succeeded` — job with zero rows → "never succeeded".
- Weekend/edge: freeze `now` to Monday 09:30 ET with a Friday 17:15 last success and assert
  `daily-close` is **not** flagged (validates the 30h SLA choice).

## Rollout

1. Land code + tests behind no flag (the job is inert until scheduled).
2. Create healthchecks.io check, set env var.
3. Add the Task Scheduler task; run once manually: `uv run python -m signal_system watchdog`.
4. Verify: with a healthy DB it sends nothing and logs a `success` run row.
5. **Immediately after:** set the per-job UUIDs for *all* existing jobs
   (`HEALTHCHECKS_UUID_NEWS_MORNING`, etc.) so masking can never recur. This plan and the
   per-job UUID rollout are complementary — do both.

## Risks & mitigations

- **Alert fatigue / false positives** over weekends or holidays → start with generous SLAs
  (30h), weekday-only runs; tighten later.
- **Watchdog itself dies silently** → mitigated by its own dedicated health check (the base
  case). Do not let watchdog fall back to the shared UUID.
- **Telegram outage** hides the alert → acceptable for v1; the health check is the backstop.

## Acceptance criteria

- A manually-induced `failed` run for any monitored job produces exactly one Telegram
  message on the next watchdog run.
- A fully healthy system produces **zero** messages and a `success` watchdog run row.
- Killing the watchdog's schedule turns its healthchecks.io check red within its grace
  window.
