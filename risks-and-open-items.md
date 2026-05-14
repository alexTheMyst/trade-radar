# Risks & Open Items

Things we identified during design that need to be validated, decided, or actively monitored. None are blockers for starting the MVP, but several should be addressed before specific later phases.

## Pre-MVP — Validate in Week 1

### 🔴 Finnhub free tier coverage

**Risk:** The architecture assumes specific Finnhub endpoints and symbols are available on the free tier (60 calls/min). The original architecture doc explicitly flagged uncertainty about `^GSPC`, `^VIX`, `USOIL` symbol strings.

**What to validate (do this before writing any real code):**
- Can you fetch S&P 500 close from free tier? What symbol works?
- Can you fetch VIX?
- `/scan/technical-indicator` — free tier?
- `/stock/insider-sentiment` — free tier? (This one is commonly paid-only.)
- `/stock/insider-transactions` — free tier?
- `/calendar/earnings` — free tier?

**If free tier doesn't cover macro symbols:** consider Yahoo Finance scraping for indices (acceptable for daily close, not for intraday), or upgrade Finnhub to paid ($50/mo) once the system is proven.

**If insider sentiment is paid-only:** Phase A of Discovery Agent (week 3) needs the scoring formula adjusted to drop that component, or you upgrade then.

### 🟢 Anthropic SDK choice

**Decision:** Use the official `anthropic` Python SDK from PyPI.

It's the reference implementation, well-maintained, and what every Anthropic code sample uses. Pin a version in `pyproject.toml` and update intentionally — the SDK evolves quickly. No real risk here, just a note for hygiene.

## During MVP

### 🟡 Windows Task Scheduler reliability

**Risk:** Windows Update will reboot the machine without your consent. Tasks set to "run only when user is logged on" will fail after a reboot if no one logs in. Tasks set to "run whether user is logged on or not" need stored credentials and proper privileges.

**Mitigation:**
- Configure task to "wake the computer to run this task"
- Configure task to "run whether user is logged on or not"
- Set Windows Update active hours to span your scheduled job times
- Healthchecks.io will catch the failure mode — but you still need to fix it, not just be notified

### 🟡 Time zones

**Risk:** Hardcoded "4:30 PM ET" assumes your machine is in or correctly resolves to Eastern. If your laptop is on Mountain Time (Arizona, no DST), this is a perpetual source of bugs.

**Mitigation:**
- Use `from zoneinfo import ZoneInfo; ZoneInfo("America/New_York")` explicitly when comparing or scheduling around market hours
- Schedule the Windows task in *machine local time* but verify it maps to the intended ET time
- Document the conversion in the task description
- On Windows, `zoneinfo` may need the `tzdata` package: `uv add tzdata` if you hit a `ZoneInfoNotFoundError`

## Pre-Week 2

### 🟡 Prompt injection from news headlines

**Risk:** Headlines fetched from Finnhub get embedded in the Claude API prompt. A malicious or weird headline could theoretically inject instructions.

**Mitigation:**
- Strip control characters and limit each headline to ~500 chars before embedding
- Use explicit delimiters in the prompt (e.g., `<headline>...</headline>` tags)
- Worst case is one day of junk classification output — annoying, not dangerous, because nothing is executed automatically

### 🟡 thesis.yaml maintenance

**Risk:** A stale `thesis.yaml` means the classifier outputs confidently wrong pillar deltas. There's no automatic detection of staleness.

**Mitigation:**
- `review_due` field in the YAML — classifier should refuse to run (or alert loudly) if past due date
- Calendar reminder, quarterly, dedicated 1-hour slot
- Treat thesis review as non-negotiable as the signal review

## Pre-Discovery Agent (Week 3+)

### 🔴 Scoring weights are unvalidated guesses

**Risk:** The `35 / 30 / 25 / 10` weights for composite score have no empirical basis. They're the architect's intuition. The agent will emit alerts based on these weights from day one.

**Mitigation:**
- Phase A (week 3) is "log only, no alerts" for two weeks. Build intuition for what the scores mean before connecting to Alert Router.
- After 90 days of logged signals, hold a tuning session. Compare what scored highest vs what actually moved. Adjust weights with documented reasoning.
- Consider a simple backtest harness in month 2 (see roadmap.md).

## Tax & Compliance — Address Before Real Money is at Stake

### 🔴 Wash sale tracking is single-account in current design

**Risk:** You hold positions across 4 accounts (main Schwab, secondary Schwab, Roth IRA, HSA). Wash sale rules apply **across all accounts in your household**, including spousal IRAs. A loss harvested in main Schwab can be disallowed by a buy in Roth IRA within 30 days.

**The current `wash_sale` table design tracks one portfolio, not four.**

**Mitigation:**
- Extend `wash_sale` table with `account` column from the start (cheap to add now, painful to retrofit)
- When the system suggests selling at a loss, it should query *all* recent buys across all accounts within the 30-day window — both backward and forward (the window is 30 days before AND 30 days after the loss sale)
- This is a real tax risk. The system can quietly cause a disallowed loss if not designed correctly.
- If implementing full cross-account tracking adds significant complexity, defer the wash sale agent feature but **add a manual checklist reminder** to the alert text whenever a loss harvest is suggested

### 🟡 K-1 ETF check

**Already a known requirement from operator preferences:** verify any suggested ETF doesn't issue K-1s. The Discovery Agent should hardcode an exclusion list of common K-1 ETFs (USO, UNG, DBC, GSG, and most commodity futures ETFs).

**Mitigation:** include K-1 exclusion in the universe-builder filter, not just at alert time.

## Operational

### 🟡 No-signal-day discipline

**Risk:** The system explicitly logs no-signal days, but if you don't read the daily digest carefully, "no signal" looks identical to "system silently broken." Healthchecks catches *runs that didn't execute* but not *runs that executed and emitted nothing.*

**Mitigation:**
- Daily digest email should always be sent, even on no-signal days, with explicit "scanned N tickers, 0 alerts" text
- Reading the daily digest at end of day is a habit you commit to — not optional

### 🟢 Cost monitoring

**Risk:** Low but real. Anthropic API costs scale with usage. If a bug puts the news classifier in a tight loop, you could rack up unexpected costs.

**Mitigation:**
- Set a monthly budget alert in Anthropic console (suggested: $20/mo for headroom; expected actual ~$5)
- Same for any paid Finnhub tier
- Quarterly cost review as part of the regular cadence

## Things We Explicitly Decided NOT to Do

For the record, so they don't get re-added by drift:

- ❌ No GitHub Actions runner (operator runs on Windows machine, owns uptime)
- ❌ No Earnings Setup agent (covered by free tools)
- ❌ No Portfolio Drift agent (manual monthly eyeball is sufficient)
- ❌ No automated execution (alert-only, forever)
- ❌ No self-learning scoring adaptation (operator adapts, system stays static)
- ❌ No hardcoded news taxonomy (driven by `thesis.yaml`)
