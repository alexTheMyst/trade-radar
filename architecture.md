# System Architecture

## Components

### 1. Scheduler

**Windows Task Scheduler** invokes the Python application with a job argument.

| Job | Cadence | Time (ET) | Purpose |
|---|---|---|---|
| `news-morning` | Weekdays | 9:00 AM | News classifier — pre-open scan (company + general/macro feeds) |
| `daily-close` | Weekdays | 4:15 PM | SPY benchmark close |
| `discovery` | Weekdays | 4:15 PM | Discovery Agent — cross-sectional momentum scan |
| `advisor` | Weekdays | after close | Advisor Agent — held-position verdicts (shadow mode) |
| `sentinel` | Weekdays | 5:00 PM | Exit Sentinel — mechanical exit triggers on held positions |
| `outcome-backfill` | Weekdays | 5:30 PM | Fill 30d/90d outcome prices for measurement |
| `review-report` | 1st Saturday monthly | 9:00 AM | Per-agent quality readout (excess/IC/hit-rate/calibration) |
| `weekly-synthesis` | Mondays | 8:00 AM | Distill the MONITORING corpus into pillar candidates |
| `backup` | Sundays | 8:00 AM | WAL-safe snapshot of state/signals.db (keep newest 8) |

Operator owns uptime (machine awake, power settings, post-Windows-Update reboots).

### 2. Heartbeat — Healthchecks.io

Every scheduled job pings three endpoints on its **own** per-job check:
- `/{uuid}/start` — at job start
- `/{uuid}` — on success
- `/{uuid}/fail` — on caught exception

Each job resolves its UUID from `HEALTHCHECKS_UUID_<JOB>` (e.g.
`HEALTHCHECKS_UUID_SENTINEL`), falling back to the shared `HEALTHCHECKS_UUID`
when the per-job var is unset. The per-job check prevents one job's success
from masking another's death — the exact silent-failure mode this system
exists to eliminate. Rollout order matters: create the check in
healthchecks.io FIRST, then set the env var (a ping to a nonexistent check
404s silently).

If a ping is missed within the configured grace window, Healthchecks.io emails/SMS the operator. Silent failure is the failure mode we are explicitly defending against.

### 3. State Manager — SQLite

Single file at `./state/signals.db`. Tables:

- `signals` — every alert ever emitted (see `signal-log-schema.md`)
- `runs` — every scheduled job invocation, with start/end timestamps and outcome
- `wash_sale` — trade ledger with a `side` column (buy/sell); the legacy
  `cost_basis` column holds the per-share trade price. Lookups are deliberately
  cross-account (the IRS wash-sale rule spans all 4 Schwab accounts).
- `benchmark_prices` — SPY daily closes for excess-return measurement
- `llm_calls` — per-call token telemetry
- `ticker_meta` — sector/industry cache
- `synthesis_reports` — weekly-synthesis run records

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

**General/macro news feed:** `news-morning` also ingests the Finnhub general news feed (probed 2026-08-07 — **available on the free tier**, 100 items, current). General headlines are classified with `ticker=None` (rendered as `MACRO` in the digest, `_`-prefixed alert ids), capped at 20 independently of the 50-company cap, with a shared article-level dedup set so the same story never alerts twice across feeds. Off-thesis classifications from both feeds are persisted as `[off_thesis]` MONITORING signals — the paid-for rationale becomes the weekly-synthesis corpus.

#### 4c. Exit Sentinel

**Cadence:** Weekdays at 5:00 PM ET (after discovery).

**Inputs:** `holdings.csv` (positions across the 4 Schwab accounts) + yfinance candle history.

**Triggers (raw magnitudes vs thresholds):**
- `drawdown_20d` — drawdown of current close from the 20-day high (INFO ≥ 10%, AR ≥ 15%)
- `day_drop_1d` — one-day close-to-close drop (INFO ≥ 5%, AR ≥ 8%)
- `below_cost` — current close below the position's (share-weighted, cross-account) cost basis (INFO ≥ 5%)

**Severity is rule-determined, not score-driven** — the weight amplifier is
deliberately unused. A drawdown is a drawdown regardless of position weight.
Scores are 100 × min(1, magnitude/severe_norm), so sentinel competes on the
same 0–100 scale as Discovery.

**Cooldown:** a prior routable sentinel row for the same ticker within 5
trading days demotes the repeat to a `[cooldown]` MONITORING row — UNLESS the
new trigger escalates to ACTION_REQUIRED with no prior severe row (escalation
always gets through). One signal per ticker per run; secondary breaches ride
in the body so a crash day doesn't triple-bill the shared INFO budget.

**Measurement:** sentinel hit = price fell after the alert. The review report
sign-flips excess for `direction="negative"`, so a sentinel "hit" reads as a
positive hit rate exactly like a News Classifier negative call.

### 5. Alert Router

Receives proposed signals from agents, applies budget, writes to log, dispatches delivery.

**Daily budget (both agents combined):**

| Tier | Cap | Channel | Threshold |
|---|---|---|---|
| `ACTION_REQUIRED` | ≤1 per day | Push notification (Pushover or similar) | Discovery score > 85 + fresh catalyst (RSI cross, insider buy, etc.) OR news delta ≥ \|3\| |
| `INFORMATIONAL` | ≤3 per day | Daily email digest at 4:30 PM ET | Discovery top-3 or news delta ≥ \|2\| |
| `MONITORING` | Unlimited | Weekly synthesis, Monday AM | Off-thesis, volume-cap, parse-failure, reconciled losers |

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
