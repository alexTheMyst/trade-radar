# Windows Task Scheduler setup

Use `ops/task-scheduler-reference.xml` as a scrubbed reference only. Replace every placeholder with an absolute path on the Windows host before importing or recreating the task.

## Command shape

- Program/script: absolute path to `uv.exe`
- Arguments: `run python -m signal_system <job>`
- Start in: absolute path to the repo root

Example:

```text
Program/script: C:\ABSOLUTE\PATH\TO\uv.exe
Arguments: run python -m signal_system news-morning
Start in: C:\ABSOLUTE\PATH\TO\trading_agent
```

Do not use relative paths. Task Scheduler often starts from `C:\Windows\System32`, so every command and working directory must be an absolute path.

## Required scheduler settings

1. **Trigger in Eastern Time**
   - `news-morning`: weekdays at 9:00 AM Eastern Time
   - `discovery`: weekdays after market close in Eastern Time
   - `sentinel`: weekdays at 5:00 PM Eastern Time
   - `backup`: Sundays at 8:00 AM Eastern Time
   - `watchdog`: weekdays at 9:30 AM and 6:00 PM Eastern Time (meta-monitor; see below)
   - If the machine is not set to Eastern Time, convert the trigger manually and note the intended ET mapping in the task description.
2. **StartWhenAvailable**
   - Enable `StartWhenAvailable` so a missed run fires as soon as the machine is back.
3. **Single-instance enforcement**
   - Set the task to `IgnoreNew` / “Do not start a new instance.”
   - Document this as the required single-instance policy for every signal-system task.
4. **Run whether logged on or not**
   - Select **run whether logged on or not**.
   - Use a **password-backed** Windows account. Do not use `S4U`; it breaks network and credential access needed for Finnhub, Anthropic, and Healthchecks.
5. **Wake and retry posture**
   - Enable wake-to-run if the machine sleeps.
   - Test each task manually once after saving credentials.

## Per-job healthchecks setup

Each scheduled job pings its **own** healthchecks.io check (the shared
`HEALTHCHECKS_UUID` fallback lets one job's success mask another's death,
defeating the no-silent-failure promise).

**Rollout order is critical — create the check FIRST, then set the env var.**
A ping to a nonexistent check 404s silently, and the shared check would go
quiet while you believe that job is covered.

1. In healthchecks.io, create one check per job below. Match the schedule and
   set the grace period to a couple hours of the trigger.
2. Set the matching env var on the Windows box (`.env` or Credential Manager):
   - `HEALTHCHECKS_UUID_DAILY_CLOSE` — weekdays after close
   - `HEALTHCHECKS_UUID_NEWS_MORNING` — weekdays 9:00 AM ET
   - `HEALTHCHECKS_UUID_DISCOVERY` — weekdays after close
   - `HEALTHCHECKS_UUID_ADVISOR` — weekdays after close
   - `HEALTHCHECKS_UUID_SENTINEL` — weekdays 5:00 PM ET
   - `HEALTHCHECKS_UUID_OUTCOME_BACKFILL` — weekdays 5:30 PM ET
   - `HEALTHCHECKS_UUID_REVIEW_REPORT` — 1st Saturday 9:00 AM ET monthly
   - `HEALTHCHECKS_UUID_WEEKLY_SYNTHESIS` — Mondays 8:00 AM ET
   - `HEALTHCHECKS_UUID_BACKUP` — Sundays 8:00 AM ET
   - `HEALTHCHECKS_UUID_WATCHDOG` — weekdays 9:30 AM / 6:00 PM ET
3. Verify per-job pings land on the right check: run the job manually once and
   confirm the matching check reports "success" in healthchecks.io while the
   others stay quiet.

A job without its per-job var set keeps using the shared `HEALTHCHECKS_UUID`
— harmless during rollout, but the shared check must never be the long-term
home for a scheduled job.

## Backup & restore

`backup` (Sundays 8:00 AM ET) writes a WAL-safe snapshot to
`state/backups/signals-YYYYMMDD.db` (see `BACKUP_DIR`; defaults to
`state/backups`), keeps the newest 8, and fails loudly if the snapshot is empty
while the live DB has rows. Test a restore before relying on it:

1. Stop the signal-system scheduled tasks (so nothing writes mid-restore).
2. Copy the newest `state/backups/signals-*.db` over `state/signals.db`.
3. Delete any stale `state/signals.db-wal` and `state/signals.db-shm`.
4. Re-enable the tasks.

The signal log is one file on one box — the backup is the only copy. Verify
the backup task pings its own healthchecks check and that a backup file exists
after the first scheduled run.

## Job-specific notes

- `watchdog` is the meta-monitor — the answer to "who watches the watchmen."
  It reads the `runs` table and sends a single Telegram message when any
  monitored job is stale (no success within its SLA) or failed. Weekdays only:
  the two daily runs (9:30 AM, 6:00 PM ET) bookend the trading day and its
  jobs. It is inert until scheduled; schedule it and set
  `HEALTHCHECKS_UUID_WATCHDOG` (rollout order: create the check FIRST, then
  set the env var). Its own `runs` row + dedicated check are the base case of
  the recursion — never let watchdog fall back to the shared UUID. See
  `ops/tasks/watchdog.xml`.
- `news-morning` depends on at least one successful `daily-close` run already being in SQLite. Run `uv run python -m signal_system daily-close` manually before enabling the morning schedule on a fresh deployment.
- `outcome-backfill` (overdue as of Aug 2026): daily at **5:30 PM ET weekdays** (30/90-day outcome prices for the review report).
- `review-report`: **first Saturday 9:00 AM ET monthly** + on-demand before each quarterly review.
- `weekly-synthesis`: **Mondays 8:00 AM ET** (the slot architecture.md reserves) — distills the MONITORING corpus into pillar candidates.
- `backup`: **Sundays 8:00 AM ET** — WAL-safe snapshot of `state/signals.db` (see "Backup & restore" above). Run once manually after deployment and verify the file restores.
- `backfill-benchmark` is a one-time bootstrap, not a schedule: run `uv run python -m signal_system backfill-benchmark` once after deployment so the SPY series covers every historical signal. Re-run after a long gap to top up.
- `log-action` / `list-signals` are manual operator commands, never scheduled:
  - `uv run python -m signal_system list-signals [--days N]` — recent DELIVERED signals
  - `uv run python -m signal_system log-action <prefix> --acted|--passed [--note "..."]` — record your acted/passed decision (the unreviewed nag in news-morning clears when acted is set)
  - `uv run python -m signal_system log-trade <TICKER> --buy|--sell --account <literal> --qty N --price P [--date YYYY-MM-DD]` — log every executed trade to the wash-sale ledger; `--sell` warns if a buy occurred in any account within 30 days
- Healthchecks should remain the source of truth for run success/failure; use non-email alerts there so the operator can distinguish "job ran" from "digest arrived."

## Classifier eval runbook (before ANY model change)

`eval-classifier` runs the golden-set (20 pinned headlines against a frozen
thesis) through the live classifier. It is a readout, not a gate — exit 0
always, ~20 API calls (~$0.01), logged to `llm_calls` as `eval_classifier`.

```text
uv run python -m signal_system eval-classifier
```

Run it BEFORE and AFTER any `ANTHROPIC_MODEL` change and compare the
`Accuracy: X/20 — <model> — set v1` lines. Same set_version = comparable
numbers; a changed set (eval/golden_headlines.yaml) is not comparable to the
old one. The frozen thesis at `eval/thesis-eval.yaml` (review_due 2099) keeps
the staleness gate from blocking eval runs.

## Weight replay before quarterly reviews

`replay-weights` replays the live cross-sectional Discovery formula offline
across candidate weight sets and horizons — no heartbeat, no DB writes.

```text
uv run python -m signal_system replay-weights --days 180 --weights 50/30/20 --weights 40/40/20 --horizons 5,20 --gates on
```

Read the caveat lines: survivorship bias (today's universe), pooled-pairs IC
across days. Use it BEFORE each quarterly weight review, never as the sole
decision input.

## Finnhub general-news feed probe (2026-08-07)

`general_news('general', min_id=0)` is **available on the free tier** with the production key: returned 100 items, recency within hours, fields `id`, `datetime`, `headline`, `source`, `summary`, `related`, `category`, `url`, `image`. The macro/general-news ingestion (news-morning) is built against this endpoint.
