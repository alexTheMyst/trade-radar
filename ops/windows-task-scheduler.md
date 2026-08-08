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

## Job-specific notes

- `news-morning` depends on at least one successful `daily-close` run already being in SQLite. Run `uv run python -m signal_system daily-close` manually before enabling the morning schedule on a fresh deployment.
- `outcome-backfill` (overdue as of Aug 2026): daily at **5:30 PM ET weekdays** (30/90-day outcome prices for the review report).
- `review-report`: **first Saturday 9:00 AM ET monthly** + on-demand before each quarterly review.
- `weekly-synthesis`: **Mondays 8:00 AM ET** (the slot architecture.md reserves) — distills the MONITORING corpus into pillar candidates.
- `backfill-benchmark` is a one-time bootstrap, not a schedule: run `uv run python -m signal_system backfill-benchmark` once after deployment so the SPY series covers every historical signal. Re-run after a long gap to top up.
- `log-action` / `list-signals` are manual operator commands, never scheduled:
  - `uv run python -m signal_system list-signals [--days N]` — recent DELIVERED signals
  - `uv run python -m signal_system log-action <prefix> --acted|--passed [--note "..."]` — record your acted/passed decision (the unreviewed nag in news-morning clears when acted is set)
- Healthchecks should remain the source of truth for run success/failure; use non-email alerts there so the operator can distinguish "job ran" from "digest arrived."

## Finnhub general-news feed probe (2026-08-07)

`general_news('general', min_id=0)` is **available on the free tier** with the production key: returned 100 items, recency within hours, fields `id`, `datetime`, `headline`, `source`, `summary`, `related`, `category`, `url`, `image`. The macro/general-news ingestion (news-morning) is built against this endpoint.
