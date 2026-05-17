# Operator setup checklist

## Scheduler

- [ ] Replace every placeholder in `ops/task-scheduler-reference.xml` with an **absolute path** on the Windows host.
- [ ] Use the command shape `uv run python -m signal_system <job>` for every scheduled task.
- [ ] Set triggers in **Eastern Time** and record the ET intent in the task description if the host runs in another timezone.
- [ ] Enable `StartWhenAvailable`.
- [ ] Enforce **single-instance** execution with “Do not start a new instance” / `IgnoreNew`.
- [ ] Select **run whether logged on or not** and store credentials for a **password-backed** Windows account.
- [ ] Manually run `uv run python -m signal_system daily-close` once before enabling `news-morning`.

## Gmail + Healthchecks

- [ ] Create a **Gmail filter** using `from:GMAIL_USERNAME` for system mail and set it to **Never send it to Spam**.
- [ ] Confirm the digest mailbox keeps signal-system mail visible; do not rely on spam-folder checks.
- [ ] In **Healthchecks**, configure SMS or push notifications as the canonical “job ran / job failed” signal.
- [ ] Do not rely on Healthchecks email alone for runtime awareness; email is the digest channel, not the primary liveness channel.

## Measurement workflow

- [ ] Leave `MEAS-02` inactive until approximately **30 days post-go-live** even though the code already exists internally.
- [ ] Within **7 days** of each alert, record:
  - `acted` — whether you acted on the signal
  - `acted_at` — when you acted or decided not to act
  - `user_note` — short reason or context
- [ ] Treat missing `acted`, `acted_at`, or `user_note` values after 7 days as a workflow miss that must be corrected before quarterly review.
- [ ] Keep this workflow manual; Phase 06 does not add a public CLI or scheduler entry for outcome backfill.
