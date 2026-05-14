# Roadmap — Weeks 2+

## Week 2 — News Classifier

Once the tracer bullet is reliable, this is the simpler of the two agents to add.

### Tasks

- [ ] Add Anthropic Claude client wrapper (`src/signal_system/data/anthropic_client.py`) — thin layer over the official `anthropic` SDK
- [ ] Add `thesis.example.yaml` → operator copies to `thesis.yaml` and customizes (gitignored)
- [ ] Add `src/signal_system/jobs/news_classifier.py` that:
  - Reads `thesis.yaml` via `pyyaml`
  - Fetches Finnhub general news + per-ticker news for holdings (`/company-news`)
  - Sends single Claude API call with the prompt from `architecture.md`
  - Parses the JSON response (use `pydantic` for schema validation — worth the dep)
  - Proposes signals to Alert Router based on `|delta| ≥ 2` threshold
- [ ] Register the new job in `__main__.py` JOBS dict
- [ ] Add the three Task Scheduler entries (9 AM, 12 PM, 4:15 PM)
- [ ] Add Healthchecks.io check for the morning run (most critical — pre-market)
- [ ] Test: deliberately put a pillar-relevant headline in front of it and confirm classification

### Cost expectation

- ~3 runs/day × 22 trading days = 66 Claude API calls/month
- Each prompt ~3K input tokens, ~500 output tokens
- At Sonnet pricing, well under $5/month
- Set up a budget alert in Anthropic console anyway

### Risks

- **Stale `thesis.yaml`:** if you don't maintain it, classifier output becomes confidently wrong. Calendar reminder, quarterly.
- **Prompt injection from news headlines:** sanitize headlines before injecting into prompt (strip control chars, length-cap each headline). Worst case is a malicious headline tells Claude to ignore instructions and you get junk output for a day — annoying, not dangerous.

## Week 3–4 — Discovery Agent

The harder agent. Build incrementally.

### Phase A: Universe + ranking (~week 3)

- [ ] Add `src/signal_system/data/universe_builder.py` — pulls `/stock/symbol?exchange=US`, filters by market cap and volume
- [ ] Store universe in SQLite `universe` table with `last_scanned` timestamp
- [ ] Add daily rotation: scan a third of the universe per day, core holdings every day
- [ ] Add scorer modules under `src/signal_system/scoring/`: `technical.py`, `fundamental.py`, `insider.py`, `thesis_alignment.py`
- [ ] Combine with weighted composite per `architecture.md` in `scoring/composite.py`
- [ ] Output: top 10 ranked tickers per run, written to a `discovery_runs` table

**Don't alert yet.** Just log rankings for two weeks. Build intuition for what the scores actually mean before letting them fire alerts.

**This is where pandas earns its keep.** Universe filtering, scoring, ranking, and rolling-window calculations are all DataFrame one-liners. Add `pandas` as a dep when you start Phase A.

### Phase B: Alert integration (~week 4)

- [ ] Connect Discovery Agent to Alert Router
- [ ] Set initial alert threshold (suggested: composite > 80, with insider buy or RSI < 35 as freshness catalyst)
- [ ] Add slot competition logic to Alert Router (only one ACTION_REQUIRED per day)

### Risks

- **Scoring weights are guesses.** The 35/30/25/10 split is a starting point, not validated. Plan to revisit after 90 days of logged signals.
- **Finnhub free tier coverage may bite here.** Insider sentiment endpoint specifically may be paid-tier. Verify before building Phase A.

## Month 2+ — Quality of Life

These are improvements, not features. Add only when the underlying system has been stable for 30+ days.

- [ ] **Push notifications** — replace email for `ACTION_REQUIRED` only. Pushover ($5 one-time) or ntfy.sh (free). Keep email for INFORMATIONAL.
- [ ] **Multi-account state** — extend `wash_sale` table with `account` column. Track wash sale windows across main Schwab, secondary Schwab, Roth IRA, HSA. This is a real tax risk — don't defer indefinitely.
- [ ] **Backtest harness** — feed historical signals into a script that computes hypothetical P&L. Lets you tune scoring weights without waiting another quarter. Trivial in pandas: load signals + price history, compute forward returns, group by signal characteristics. One-day project once Phase A is logging cleanly.
- [ ] **Weekly digest formatting** — pretty Monday-morning summary instead of plain text
- [ ] **`acted` field auto-prompt** — script that scans recent unresolved signals and asks "did you act on these?" via email reply or simple web form

## Quarterly Rituals

Calendar these. Non-negotiable.

### Quarterly Review (every 3 months)

1. Pull signal log from SQLite
2. Compute hit rate per signal type per the schema in `signal-log-schema.md`
3. Compare to SPY total return for the same period
4. Decide: keep, tune, or kill each agent based on the numbers
5. Update `thesis.yaml` if pillars have shifted

### Annual Review (January)

1. Year-over-year hit rate
2. Total cost (Anthropic API + any paid Finnhub tier upgrades)
3. Estimated alpha vs SPY (acted-on signals only)
4. Honest answer: did this beat the cost of building and maintaining it?

If after a full year the answer is no — kill the project or rebuild a different way. Sunk cost is not a reason to keep running a system that isn't earning its keep.

## What This Roadmap Does NOT Include

- ❌ Auto-execution. Forever a non-goal.
- ❌ A web UI. CLI + email is sufficient.
- ❌ Multi-user support. This is a personal tool.
- ❌ "AI portfolio manager" mode where the system makes decisions. The system is a check on the operator, not a replacement.
