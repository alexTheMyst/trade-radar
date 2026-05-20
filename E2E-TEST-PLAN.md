# End-to-End Test Plan

Run these commands in order. Each section has a verification step before moving on.

---

## 0. Pre-flight: environment and files

```powershell
# Confirm .env has all required keys (none should print as blank)
uv run python -c "
import os; from dotenv import load_dotenv; load_dotenv()
keys = ['FINNHUB_API_KEY','HEALTHCHECKS_UUID','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID',
        'ANTHROPIC_API_KEY','ANTHROPIC_MODEL']
[print(k, '=', 'OK' if os.environ.get(k,'').strip() else 'MISSING') for k in keys]
"

# Confirm thesis.yaml exists (news-morning requires it)
uv run python -c "from pathlib import Path; print('thesis.yaml:', Path('thesis.yaml').exists())"

# Confirm universe.csv exists with at least one core holding
uv run python -c "
from signal_system.data.universe import get_core_holdings, get_todays_universe
cores = get_core_holdings()
today = get_todays_universe()
print(f'{len(cores)} core holdings, {len(today)} total today:', cores[:5])
"

# Confirm DISCOVERY_PHASE in .env (should be A or B)
uv run python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('DISCOVERY_PHASE:', os.environ.get('DISCOVERY_PHASE','A (default)'))"
```

---

## 1. DB initialization

```powershell
# Remove stale DB to start clean (optional but recommended for E2E)
Remove-Item -ErrorAction SilentlyContinue state\signals.db, state\signals.db-wal, state\signals.db-shm

# Initialize DB
uv run python -c "from signal_system.state import repository; repository.init_db(); print('DB initialized')"

# Verify all tables exist
uv run python -c "import sqlite3; tbl='table'; [print(r[0]) for r in sqlite3.connect('state/signals.db').execute('SELECT name FROM sqlite_master WHERE type=? ORDER BY name', (tbl,))]"
# Expected: llm_calls  runs  signals  wash_sale
```

---

## 2. Job 1 — `daily-close`

```powershell
uv run python -m signal_system daily-close
```

**Verify:**

```powershell
# Run should be marked success
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT job, status, started_at, ended_at FROM runs ORDER BY started_at DESC LIMIT 3')]"

# SPY signal should exist with routing_status=DELIVERED
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT alert_id, ticker, severity, routing_status, score, title FROM signals ORDER BY timestamp DESC LIMIT 5')]"
```

Expected: run row with `status=success`, one signal row for `SPY` with `severity=INFORMATIONAL`.

Check Telegram: message starting with `Daily Close — SPY <price>` should arrive.

---

## 3. Job 2 — `discovery` (Phase A: logs-only)

```powershell
uv run python -m signal_system discovery
```

**Verify:**

```powershell
# Run should complete successfully
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT job, status, tickers_scanned, tickers_signaled FROM runs ORDER BY started_at DESC LIMIT 3')]"

# Discovery signals should be MONITORING (Phase A — no routing, no email)
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT agent, severity, routing_status, ticker, score FROM signals WHERE agent=? ORDER BY score DESC LIMIT 10', ('discovery_agent',))]"
```

Expected: run with `tickers_scanned > 0`. Signals should have `routing_status=MONITORING`. **No Telegram message sent in Phase A.**

---

## 4. Job 3 — `news-morning`

This requires `daily-close` to have run successfully first (step 2 satisfies this).

```powershell
uv run python -m signal_system news-morning
```

**Verify:**

```powershell
# Run should succeed
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT job, status, started_at FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 3', ('news-morning',))]"

# Check classified signals — should have routing_status set
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT agent, ticker, severity, routing_status, title FROM signals WHERE agent=? ORDER BY timestamp DESC LIMIT 10', ('news_morning',))]"

# Check LLM token telemetry was logged
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT job, model_version, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens FROM llm_calls ORDER BY id DESC LIMIT 5')]"

# Check alert router budget enforced (max 1 ACTION_REQUIRED, 3 INFORMATIONAL today)
uv run python -c "import sqlite3, datetime; today=datetime.date.today().isoformat(); [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT severity, COUNT(*) FROM signals WHERE routing_status=? AND timestamp LIKE ? GROUP BY severity', ('DELIVERED', today+'%'))]"
```

Check Telegram: digest message starting with `News Morning Digest — <date>` should arrive. Must show **scanned N tickers, X alerts** even if no alerts fired (no-signal day confirmation).

---

## 5. Idempotency — rerun daily-close

```powershell
uv run python -m signal_system daily-close
```

**Verify:**

```powershell
# Signal count for SPY should NOT increase (INSERT OR IGNORE)
uv run python -c "import sqlite3; print(sqlite3.connect('state/signals.db').execute('SELECT COUNT(*) FROM signals WHERE ticker=?', ('SPY',)).fetchone()[0])"
```

Expected: same count as after step 2. Duplicate run is inserted in `runs` table but signal is deduplicated by `alert_id`.

---

## 6. Alert budget audit

```powershell
# Full budget check for today
uv run python -c "import sqlite3, datetime; today=datetime.date.today().isoformat(); [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT severity, routing_status, COUNT(*) FROM signals WHERE timestamp LIKE ? GROUP BY severity, routing_status ORDER BY severity, routing_status', (today+'%',))]"
```

Expected: `ACTION_REQUIRED/DELIVERED` ≤ 1, `INFORMATIONAL/DELIVERED` ≤ 3. Overflow signals appear as `SUPPRESSED` or `MONITORING`.

---

## 7. Heartbeat audit

All three jobs share a single `HEALTHCHECKS_UUID`. Log into [healthchecks.io](https://healthchecks.io) and confirm:

- The check for your `HEALTHCHECKS_UUID` shows **Success** status with a last ping timestamp matching one of the recent job runs

If it shows **Grace** or **Down**, check the `runs` table for `status=failed` rows and review stderr output.

---

## 8. Final DB state summary

```powershell
uv run python -c "import sqlite3; c=sqlite3.connect('state/signals.db'); [print(t, c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ('runs','signals','llm_calls')]"

# Confirm no NULL routing_status for non-DAILY_CLOSE signals
uv run python -c "import sqlite3; [print(*r) for r in sqlite3.connect('state/signals.db').execute('SELECT agent, COUNT(*) FROM signals WHERE routing_status IS NULL GROUP BY agent')]"
```

---

## Known failure modes

| Symptom | Likely cause |
|---|---|
| `RuntimeError: Required environment variable` | Missing key in `.env` |
| `news-morning requires a successful prior daily-close run` | Run step 2 first |
| `ThesisStaleError` | `review_due` in `thesis.yaml` is past — update it |
| No signals from `discovery` at all | Free-tier endpoint returning 403 — check logs for `paid tier` warnings |
| Telegram message not received | Check `TELEGRAM_BOT_TOKEN` is valid and `TELEGRAM_CHAT_ID` matches your chat — send `/start` to the bot first if new |
| `ZoneInfoNotFoundError` | Install `tzdata`: `uv add tzdata` |
