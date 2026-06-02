# Advisor Agent — BUY / HOLD / SELL Decision Support

**Date:** 2026-06-02
**Status:** Design approved, pending implementation plan
**Author:** Operator + Claude (brainstorming session)

## Summary

A new **Advisor Agent** that consumes the findings already produced by the
system (Discovery momentum + News Classifier deltas) plus an operator-maintained
holdings file, and emits **BUY / HOLD / SELL** verdicts with rationale. It
covers both held positions (SELL / HOLD / add) and new buy candidates.

The advisor **recommends and explains; it never executes**. This intentionally
amends the project's original "alert-only, never advice" stance to
**"no automated execution"** — the manual-execution guarantee is preserved, the
no-opinion stance is dropped.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Stance | Decision-support, not auto-execution. Recommends + explains, never trades. |
| Coverage | Both held positions (SELL/HOLD/ADD) **and** new BUY candidates. |
| Position feed | Operator-maintained `holdings.csv` (ticker, shares, cost_basis, account). |
| Verdict engine | Hybrid: deterministic rules → verdict + confidence; Claude writes rationale only. |
| Freshness | Approach B — recompute momentum fresh for the (small) holdings set; new-buys from persisted Discovery findings. |
| Delivery | Scheduled daily Advisory Digest (Telegram) **and** on-demand command. |
| Rollout | Ship in log-only **shadow mode**; graduate to actionable when quarterly IC review validates the matrix. |

## Architecture

The advisor is a **pure consumer**. It never re-runs the agents and never
touches the alert router. New module `advisor/` mirrors `discovery/` and
`classifier/`.

```
src/signal_system/
├── data/holdings.py              # NEW: load holdings.csv (mirrors universe.py)
├── data/holdings.csv             # NEW: operator file, gitignored (like universe.csv)
├── advisor/
│   ├── advisor_agent.py          # NEW: orchestration — gather inputs → verdicts
│   ├── verdict_engine.py         # NEW: deterministic rules → Verdict + confidence
│   └── rationale.py              # NEW: Claude messages.parse() → rationale text
├── jobs/advisor.py               # NEW: run() — scheduled Advisory Digest
├── jobs/outcome_backfill.py      # EXTEND: backfill `advice` rows too
├── state/repository.py           # EXTEND: get_recent_signals, insert_advice, `advice` table
└── __main__.py                   # EXTEND: "advisor" + "advise [TICKER]" subcommands
```

### Holdings file

`data/holdings.csv`, operator-maintained, **gitignored** (like `universe.csv`),
with a loud-fail loader exactly like `require_non_empty_universe`.

| column | example | purpose |
|--------|---------|---------|
| `ticker` | FCX | symbol |
| `shares` | 40 | position size |
| `cost_basis` | 38.10 | avg cost → P&L, wash-sale awareness |
| `account` | schwab_main | one of the 4 accounts (per-account, as CLAUDE.md mandates) |
| `thesis_pillar` | *(optional)* | ties holding to a `thesis.yaml` pillar for HOLD/SELL reasoning |

A ticker may appear in multiple rows (same stock across accounts). The advisor
evaluates **per (ticker, account)** because wash-sale rules are per-account, and
may roll up a combined view in the digest.

### Entry points

- `python -m signal_system advisor` — scheduled full run: every holding + top
  new-buy candidates → Advisory Digest via Telegram. Wrapped in `heartbeat()` +
  `insert_run("advisor")`. New "Advisor" Windows scheduled task after Daily Close.
- `python -m signal_system advise FCX` — on-demand single ticker (held or not),
  prints verdict to **stdout only**: no heartbeat, no Telegram, **no `advice`
  table write** (ad-hoc lookups must not pollute the measured verdict set).

## Verdict engine (deterministic core)

A **transparent two-axis decision matrix** — interpretability beats marginal
theoretical edge for a discretionary, alert-only tool. Two independent axes,
each classified bullish / neutral / bearish, combined through a fixed table.

### Axis 1 — Trend (moving-average filter)

Recomputed fresh per ticker (Approach B), fetching ~260 days of history.

**Deliberately NOT 20d/5d momentum for position decisions.** Short-horizon
momentum (especially 5-day) is noise — the robust momentum effect lives at
3–12 months, and at ~1-week horizons it reverses (short-term reversal). Driving
a SELL off 5-day strength would dump long-term winners on a wiggle.

- **Bullish (uptrend):** price > 50-day SMA > 200-day SMA
- **Bearish (downtrend):** price < 50-day SMA < 200-day SMA
- **Neutral:** anything mixed

`momentum_20d`, `momentum_5d`, and `range_vs_20d` are demoted to **timing /
extension flags only** ("extended — don't chase", "pullback to rising 50d"),
never the core direction.

### Axis 2 — News

From persisted News Classifier signals for the ticker (last ~14 days), via the
new `get_recent_signals` query: net of `direction × confidence`.

- **Bullish:** net positive past threshold
- **Bearish:** net negative past threshold
- **Neutral:** little/none

### The matrix (held positions)

| | News bullish | News neutral | News bearish |
|---|---|---|---|
| **Trend bullish** | BUY | HOLD | HOLD |
| **Trend neutral** | HOLD | HOLD | SELL |
| **Trend bearish** | HOLD | SELL | SELL |

- **Not-held candidates:** only the top-left corner fires — Trend-bullish +
  News-bullish/neutral → **BUY**, everything else → **PASS**.
- **Confidence (0–1):** how strong and aligned the two axes are (both strong &
  agreeing → high; conflicting → low). This is the IC-measurable number.
- A held SELL's rationale distinguishes **trim vs exit** by conviction, but the
  logged verdict stays one of BUY/HOLD/SELL.

### Overlays

1. **Thesis-break override (asymmetric exit discipline).** A single
   high-confidence *negative* News-Classifier hit (ACTION_REQUIRED) on the
   holding's pillar escalates to **SELL/REVIEW regardless of momentum**. Missing
   a buy costs opportunity; missing a sell costs capital — exits must be more
   responsive than entries.
2. **No chasing.** A held BUY (add) only fires when the uptrend is intact **and**
   the position is **not extended** (not jammed at its highs). Extended winner →
   ADD downgrades to HOLD. New (not-yet-held) BUYs keep a normal threshold.
3. **Wash-sale caution.** If verdict is SELL **and** the position is at a loss
   (`price < cost_basis`), the advisor **flags** "verify 30-day wash-sale window
   across accounts" — it does not compute it (full computation needs trade
   history we don't have). Honest about its limits.
4. **Thesis gate.** If `thesis.yaml` `review_due` is past, the advisor refuses to
   run (same as the News Classifier). A holding whose pillar no longer exists →
   "thesis orphan — review" note.

**All thresholds are named constants** — initial guesses, tunable at quarterly
review (same stance as Discovery weights and classifier thresholds).

## Data flow

### Scheduled `advisor` job `run()`

1. `insert_run("advisor")`, enter `heartbeat()`.
2. **Thesis gate** — load thesis; refuse loudly (heartbeat `/fail`) if
   `review_due` past.
3. **Load holdings** — `require_non_empty_holdings()`; missing/empty fails the
   run, never a green empty digest.
4. **Held side** — batch-fetch ~260d history (`fetch_history`); compute MA trend
   + extension flags; fetch current quote for price & P&L; query recent news
   signals (~14d) → engine per (ticker, account).
5. **New-buy side** — query recent persisted **Discovery DELIVERED** signals not
   in holdings → BUY/PASS corner; cap to **top ~5 by confidence**.
6. **Rationale** — `rationale.py` calls Claude `messages.parse()` (temp 0,
   cached system prompt) for a one-paragraph rationale per verdict; logs
   `llm_calls` telemetry like the classifier.
7. **Persist** — write every verdict to `advice` table.
8. **Deliver** — render Advisory Digest, validate counts, send via
   `telegram_sender`.
9. `update_run("success")`.

**No alert router.** The router's 1+3 daily budget rations *alerts*; a portfolio
review is the opposite — every holding's verdict every day. All holdings always
shown; new-buys capped at ~5. Keeps the advisor a clean non-router consumer.

### On-demand `advise TICKER`

Same engine for one ticker (held → full; not-held → BUY/PASS). **Prints to
stdout only**: no heartbeat, no Telegram, no `advice` write. Pure read.

### Error handling (degrade per-ticker, fail loud on outages)

- Ticker with no price history → verdict `NO DATA — review` shown in digest (run
  still succeeds); never silently dropped.
- Claude rationale failure → fall back to **templated rationale** from engine
  factors (verdict is deterministic and already computed; the LLM is cosmetic),
  log the failure. Same resilience as the classifier parse-failure path.
- Empty recent news → news axis = neutral (normal, not an error).

### Shadow mode

A single config flag. When on: verdicts are still computed, persisted, and
delivered, but the digest header reads **"SHADOW MODE — log only, not
actionable"** and lines are framed as observations. Flip off once IC review
validates the matrix.

## Persistence & measurement

New **`advice` table** (mirrors `signals` conventions + IC columns from
CLAUDE.md):

| column | purpose |
|---|---|
| `advice_id` | deterministic hash `ticker:date:account:advisor` |
| `run_id` | FK to `runs` |
| `timestamp` | ET ISO |
| `ticker`, `account` | account NULL for not-held candidates |
| `held` | held position vs new-buy candidate |
| `verdict` | BUY / HOLD / SELL / PASS |
| `confidence` | 0–1 |
| `mom_axis`, `news_axis` | bullish/neutral/bearish — inputs, for audit |
| `factors_json` | raw snapshot: 50d/200d SMA, price, 20d/5d/range, news-net |
| `flags` | wash_sale_caution / extended / thesis_orphan / no_data |
| `rationale`, `rationale_source` | text + `claude`\|`template` |
| `model_version`, `thesis_version_hash` | IC comparability |
| `signal_price_snapshot` | unadjusted price at advice time |
| `shadow_mode` | was this a shadow verdict |
| `outcome_price_30d`, `outcome_price_90d` | backfilled later |
| `acted`, `acted_at`, `user_note` | operator fills within 7 days |

**Repository additions** (all SQLite stays in `repository.py`): `init_db` gains
idempotent `CREATE TABLE advice` + `_ensure_column` migrations; new
`insert_advice(...)`, `get_recent_signals(ticker, since, agent=None)`; the
existing `outcome_backfill` job is **extended** to backfill `advice` rows through
the same 30/90d mechanism.

## Testing

Pytest, mirroring existing conventions; conftest handles env; **no live
network/LLM calls** (mock `Anthropic` and data clients like classifier tests).

1. **`verdict_engine` (crown jewel, table-driven):** all 9 matrix cells; MA-trend
   classification from synthetic price series; thesis-break override fires over
   bullish momentum; no-chasing downgrade (ADD→HOLD when extended); wash-sale
   flag on SELL-at-a-loss; confidence monotonicity (aligned-strong → high,
   conflicting → low). Pure, deterministic.
2. **Holdings loader:** multi-account rows, comment/blank rows, missing/empty →
   loud `EmptyHoldings` fail (mirrors universe tests).
3. **`advisor_agent` integration:** injected fake history/news/quote fixtures →
   verdicts produced & persisted; per-ticker `NO DATA` degradation keeps run
   green; `review_due`-past thesis gate refuses.
4. **Rationale fallback:** mocked Claude failure → templated rationale, verdict &
   run still succeed.
5. **Digest + on-demand:** counts match persisted; shadow-mode header present;
   `advise TICKER` prints to stdout and writes nothing to `advice`.
6. **Repository:** `insert_advice`/`get_recent_signals` roundtrip; advice rows
   picked up by outcome backfill.

## Out of scope (YAGNI)

- Automated trade execution (never).
- Position sizing / dollar amounts (manual decision).
- Schwab/brokerage API integration — positions come from the operator file.
- Self-learning / adaptive thresholds (operator tunes at quarterly review).
- Wash-sale *computation* (only a caution flag; needs trade history we lack).

## Open items for implementation

- Exact threshold constants for trend slope, news-net, and "extended" — initial
  guesses, to be set in `verdict_engine.py` and tuned at quarterly review.
- Confirm `fetch_history` supports ~260-day lookback on the current yfinance path.
- Whether new-buy candidates should also pull from recent News-Classifier
  ACTION_REQUIRED (not just Discovery) — default: Discovery only for v1.
