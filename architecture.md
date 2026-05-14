# System Architecture

## Components

### 1. Scheduler

**Windows Task Scheduler** invokes the Python application with a job argument.

| Job | Cadence | Time (ET) | Purpose |
|---|---|---|---|
| `news-morning` | Weekdays | 9:00 AM | News classifier — pre-open scan |
| `news-midday` | Weekdays | 12:00 PM | News classifier — midday update |
| `daily-close` | Weekdays | 4:15 PM | News classifier + Discovery Agent |
| `weekly-digest` | Mondays | 8:00 AM | Roll up the prior week, send digest |

Operator owns uptime (machine awake, power settings, post-Windows-Update reboots).

### 2. Heartbeat — Healthchecks.io

Every scheduled job pings three endpoints:
- `/{uuid}/start` — at job start
- `/{uuid}` — on success
- `/{uuid}/fail` — on caught exception

If a ping is missed within the configured grace window, Healthchecks.io emails/SMS the operator. Silent failure is the failure mode we are explicitly defending against.

Minimum two checks for MVP: `daily-close` and `news-morning`. Add others as agents come online.

### 3. State Manager — SQLite

Single file at `./state/signals.db`. Tables:

- `signals` — every alert ever emitted (see `signal-log-schema.md`)
- `runs` — every scheduled job invocation, with start/end timestamps and outcome
- `wash_sale` — sold positions and 30-day window expiry, **per account** (see open items)
- `universe` — current discovery universe with last-scored timestamp

**Why SQLite over JSON:** atomic writes, transactional updates, queryable from CLI. Concurrent writes from overlapping jobs are a non-issue with SQLite's WAL mode.

### 4. Agents

Two agents in scope. Each reads from State Manager, emits zero or more proposed signals to the Alert Router, and writes a run record.

#### 4a. Discovery Agent

**Cadence:** Once daily at 4:15 PM ET, after market close.

**Universe:** Wide — starts from Finnhub's US stock list filtered by:
- Market cap > $5B
- Average daily volume > 500K shares
- Excludes ADRs that issue K-1 forms

This will yield ~1,500 tickers. For free-tier rate limits (60 calls/min), scan rotates: a third of the universe per day, full coverage every 3 days. Core holdings always scanned daily.

**Scoring (composite, 0–100):**

```
score = 35 × technical_score
      + 30 × fundamental_score
      + 25 × thesis_alignment_score
      + 10 × insider_sentiment_score
```

These weights are **initial defaults, not validated**. After 90 days of logged signals, review and tune.

**Output:** Ranked top 10 tickers per run. Signal fires only when score crosses the alert threshold (see Alert Router below).

**Sizing rule (enforced in suggestion text, not by the agent):**
- Non-thesis pick: max 3–5% of portfolio per position
- All non-thesis positions combined: max 15% of portfolio
- Promotion: if a non-thesis name proves out over 2 quarters AND fits a coherent thesis extension, it gets promoted to thesis (cap lifts). Promotion is a manual operator decision, logged in `thesis.yaml`.

#### 4b. News Classifier

**Cadence:** 3× daily (9 AM, 12 PM, 4:15 PM ET weekdays).

**Inputs:**
- Finnhub `/news?category=general` for macro headlines
- Finnhub `/company-news?symbol={t}` for each core holding
- `thesis.yaml` for the current thesis pillars (operator-maintained)

**Processing:** Single Claude API call per run. Prompt structure:

```
You are a financial news classifier. Given:
1. The operator's current thesis pillars (below)
2. Today's macro and per-ticker headlines (below)

Output a JSON object with:
- per_pillar_delta: { pillar_name: { score: -3..+3, reasoning: string } }
- standout_headlines: [ { headline, pillar, impact_description } ]
- thesis_silence: list any pillar with zero relevant news in last 30 days

Do not output any pillar or headline that scored 0 with no reasoning.

THESIS:
{contents of thesis.yaml}

HEADLINES:
{deduped list of headlines from last cadence interval}
```

**Output:** Pillar deltas with reasoning. Signal fires when `|score| ≥ 2` for any pillar.

**Key design decision:** taxonomy is **not hardcoded**. The classifier reads `thesis.yaml` at runtime. When the operator updates the thesis, the classifier adapts without code changes.

### 5. Alert Router

Receives proposed signals from agents, applies budget, writes to log, dispatches delivery.

**Daily budget (both agents combined):**

| Tier | Cap | Channel | Threshold |
|---|---|---|---|
| `ACTION_REQUIRED` | ≤1 per day | Push notification (Pushover or similar) | Discovery score > 85 + fresh catalyst (RSI cross, insider buy, etc.) OR news delta ≥ \|3\| |
| `INFORMATIONAL` | ≤3 per day | Daily email digest at 4:30 PM ET | Discovery top-3 or news delta ≥ \|2\| |
| `MONITORING` | Unlimited | Weekly digest, Monday AM | All other signals worth logging |

**Slot competition:** If two agents both want to fire `ACTION_REQUIRED` on the same day, the router picks the higher-scored one and demotes the others to `INFORMATIONAL`. No exceptions. This is the forcing function for signal quality.

**No-signal days:** Explicitly logged with a "scanned N tickers, no alerts fired" entry in the daily digest. Silence with confirmation > silence with ambiguity.

### 6. Alert Delivery

**MVP:** Gmail SMTP via stdlib `smtplib`. Operator's Gmail account, app password stored in Windows Credential Manager or a gitignored `.env` file.

**Future:** Push notifications via Pushover (paid, $5 one-time) or ntfy.sh (free). Defer until MVP plumbing is solid.

---

## Data Flow

```
4:15 PM ET
  │
  ├─ Task Scheduler fires daily-close job
  ├─ Python script pings healthchecks.io /start
  │
  ├─ Discovery Agent runs:
  │   ├─ Read state: which tickers due for scan today
  │   ├─ Fetch Finnhub: technicals + insider + analyst
  │   ├─ Score, rank, propose signals to Alert Router
  │   └─ Write run record
  │
  ├─ News Classifier runs:
  │   ├─ Read thesis.yaml
  │   ├─ Fetch Finnhub: macro + per-ticker news since 12 PM
  │   ├─ Single Claude API call → pillar deltas
  │   ├─ Propose signals to Alert Router
  │   └─ Write run record
  │
  ├─ Alert Router:
  │   ├─ Apply daily budget + slot competition
  │   ├─ Write all signals to SQLite (even suppressed ones, tagged MONITORING)
  │   ├─ Compose 4:30 PM digest email
  │   ├─ If any ACTION_REQUIRED: also fire push notification
  │   └─ Send
  │
  └─ Python script pings healthchecks.io /success (or /fail on exception)
```

---

## What This Architecture Does NOT Include

These are intentional cuts from the original design, documented so they're not silently re-added later:

| Cut | Why |
|---|---|
| Earnings Setup agent | Seeking Alpha and Schwab's earnings tab already cover this |
| Portfolio Drift agent | Eyeballing pillar weights monthly in Schwab is sufficient for a concentrated portfolio |
| Regime Classifier | Subsumed into the news classifier — pillar deltas convey regime info |
| Automated execution | All trades placed manually on Schwab |
| Multi-portfolio reconciliation | Out of MVP scope; revisit when wash sale tracking gets serious (see open items) |
