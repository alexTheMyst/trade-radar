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
   - Use a **password-backed** Windows account. Do not use `S4U`; it breaks network and credential access needed for Finnhub, Anthropic, Gmail SMTP, and Healthchecks.
5. **Wake and retry posture**
   - Enable wake-to-run if the machine sleeps.
   - Test each task manually once after saving credentials.

## Job-specific notes

- `news-morning` depends on at least one successful `daily-close` run already being in SQLite. Run `uv run python -m signal_system daily-close` manually before enabling the morning schedule on a fresh deployment.
- Keep `MEAS-02` deferred: the internal outcome backfill code exists, but do not schedule or activate it until roughly **30 days post-go-live**.
- Healthchecks should remain the source of truth for run success/failure; use non-email alerts there so the operator can distinguish “job ran” from “digest arrived.”
