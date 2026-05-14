# MVP Week 1 — Tracer Bullet

The MVP is **not the agents.** The MVP is the plumbing. End of week 1 proves the boring infrastructure works end-to-end. Agents come in week 2+.

## Why Plumbing First

The hardest, most failure-prone parts of this system are: scheduling, state persistence, secret handling, and alert delivery. Bugs here cause **silent** signal misses, which look identical to "no alerts today." Agent logic is easy to write once the plumbing is reliable.

## Acceptance Criteria — End of Week 1

A working tracer bullet means **all five** of these are true:

1. ✅ Python service runs on Windows Task Scheduler, daily at 4:30 PM ET
2. ✅ Healthchecks.io receives `/start` and `/success` pings on every successful run
3. ✅ One trivial signal is written to SQLite per run — e.g., today's S&P close pulled from Finnhub
4. ✅ One email arrives in operator's inbox by ~4:31 PM ET containing the signal
5. ✅ Failure-mode test passes: kill the network mid-run, confirm `/fail` ping fires and operator gets the Healthchecks notification

If any of these fail, the agents won't save you. Fix the plumbing first.

## Task Breakdown

### Pre-work (do this before writing any Python)

- [ ] **Validate Finnhub free tier** — sign up, try fetching the S&P 500 close. The architecture doc warns about `^GSPC` / `^VIX` symbol strings; figure out what Finnhub actually wants for index data. If macro symbols aren't on free tier, stop and re-plan the data layer before writing code.
- [ ] **Create Healthchecks.io account** — set up one check for `daily-close`, grace window 10 minutes, alert channel = email + SMS
- [ ] **Set up Gmail app password** — Google account → Security → App passwords. Don't use main password.
- [ ] **Create Anthropic API key** (not needed for MVP run but good to have configured)
- [ ] **Install uv on the Windows machine** — `pip install uv` or grab the installer from astral.sh. Falls back to plain `pip + venv` if you prefer; just keep the choice consistent.

### Python project skeleton

- [ ] Initialize project: `uv init signal-system && cd signal-system`
- [ ] Dependencies (`uv add`):
  - `anthropic` — official Anthropic SDK (for week 2, fine to add now)
  - `finnhub-python` — official Finnhub SDK
  - `python-dotenv` — for loading `.env`
  - `pyyaml` — for `thesis.yaml` parsing (week 2)
  - `httpx` — for healthchecks pings (lighter than requests; `urllib` also works)
- [ ] Stdlib used: `sqlite3`, `smtplib`, `email.message`, `logging`, `datetime`, `zoneinfo`
- [ ] Package layout:

```
signal-system/
├── pyproject.toml
├── .gitignore                  # includes .env, state/, *.db
├── .env.example                # template for secrets, committed
├── .env                        # actual secrets, NEVER committed
├── README.md
├── state/                      # gitignored
│   └── signals.db
├── src/
│   └── signal_system/
│       ├── __init__.py
│       ├── __main__.py         # `python -m signal_system daily-close`
│       ├── config.py           # loads .env, exposes settings
│       ├── jobs/
│       │   ├── __init__.py
│       │   └── daily_close.py  # the only job for MVP
│       ├── data/
│       │   └── finnhub_client.py
│       ├── state/
│       │   └── repository.py   # SQLite access
│       ├── delivery/
│       │   └── email_sender.py # Gmail SMTP
│       └── monitoring/
│           └── heartbeat.py    # healthchecks.io pings
└── tests/
    └── test_smoke.py
```

### Secrets

- [ ] Store secrets in a `.env` file at project root
- [ ] **Never commit `.env` to git.** Add it to `.gitignore` from commit 1. Commit `.env.example` as a template.
- [ ] Required secrets for MVP:
  - `FINNHUB_API_KEY`
  - `HEALTHCHECKS_UUID`
  - `GMAIL_USERNAME`
  - `GMAIL_APP_PASSWORD`
  - `ALERT_RECIPIENT_EMAIL`

### The actual MVP code path

```python
# src/signal_system/__main__.py
import sys
from signal_system.jobs.daily_close import run as run_daily_close

JOBS = {"daily-close": run_daily_close}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print(f"Usage: python -m signal_system <job>. Jobs: {list(JOBS)}")
        sys.exit(2)
    JOBS[sys.argv[1]]()
```

```python
# src/signal_system/jobs/daily_close.py
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.data.finnhub_client import fetch_sp500_close
from signal_system.state.repository import insert_signal
from signal_system.delivery.email_sender import send_email

def run():
    with heartbeat():                                  # context manager pings start/success/fail
        sp_close = fetch_sp500_close()
        insert_signal(agent="DAILY_CLOSE", ticker="SPX",
                      title=f"S&P closed at {sp_close}", score=sp_close)
        send_email(subject="Daily Close",
                   body=f"S&P closed at {sp_close}")
```

That's it. ~150 lines of Python total across all modules.

### Windows Task Scheduler

- [ ] Create a basic task: "Daily Close Signal"
- [ ] Trigger: daily at 4:30 PM ET (adjust for your machine timezone)
- [ ] Action — recommended setup:
  - Program: `C:\path\to\signal-system\.venv\Scripts\python.exe`
  - Arguments: `-m signal_system daily-close`
  - Start in: `C:\path\to\signal-system`
- [ ] Settings: run whether user is logged on or not; wake the computer to run this task
- [ ] **Test by running it manually first.** Don't trust the scheduler until you've seen the email arrive.

### Failure-mode test

This is the test most people skip. Do it.

- [ ] Disable network on the Windows machine
- [ ] Manually run the job
- [ ] Confirm: exception thrown, `/fail` ping sent to Healthchecks, Healthchecks email arrives
- [ ] Re-enable network, run manually again, confirm normal path works

If you can't get a `/fail` notification when the network is down, you have no way to know when the system silently breaks in production.

## SQLite Schema (MVP only)

Just enough to log signals. Full schema lives in `signal-log-schema.md`. Apply on first run via `sqlite3` from stdlib.

```sql
CREATE TABLE IF NOT EXISTS signals (
    alert_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    agent TEXT NOT NULL,
    severity TEXT NOT NULL,
    ticker TEXT,
    title TEXT NOT NULL,
    body TEXT,
    suggested_action TEXT,
    score REAL,
    acted INTEGER,         -- 1 = yes, 0 = no, NULL = not yet decided
    acted_at TEXT,
    user_note TEXT,
    outcome_price_30d REAL,
    outcome_price_90d REAL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL   -- 'running', 'success', 'failed'
);
```

## Definition of Done

End of week 1, you should be able to look at your inbox Friday afternoon and see five emails with the S&P close. SQLite should have five `success` rows in `runs` and five rows in `signals`. Healthchecks dashboard should be green.

If that works, week 2 is just plugging in the news classifier — same plumbing.
