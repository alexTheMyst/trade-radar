# Signal System — Activity Analysis & Findings

**Date:** 2026-05-30
**Source:** Live `state/signals.db` (34 runs, 2,197 signals, 392 LLM calls) + code inspection
**Scope:** Health of the alert-only signal pipeline; prioritized improvement backlog.

---

## Summary

The pipeline runs end-to-end and the **Alert Router budget works exactly** (1 ACTION_REQUIRED
+ 3 INFORMATIONAL delivered on every active day). Prompt caching is highly effective
(~97% cache-read hit rate). However, two issues defeat core goals of the system, and several
others degrade signal quality and operability.

**Critical issue #1 (price snapshot) has been fixed in this task** — see [Fix applied](#fix-applied-critical-1).

---

## Run history (chronological)

- 2026-05-17 → 2026-05-30, three jobs: `daily-close`, `discovery`, `news-morning`.
- **Failures:** 2× `daily-close` on 05-17 (initial setup), `news-morning` failed 05-18 and 05-28.
- **`discovery` scans only ~24–29 tickers/day and has emitted `sig=0` on every run.**
- **No runs recorded for Friday 2026-05-29** (a market day) — silent gap.
- The 05-30 `news-morning` run is stuck in `status='running'`.

## Signal breakdown

| Dimension | Counts |
|---|---|
| By agent | `news_morning` 1,880 · `news_classifier` 306 · `DAILY_CLOSE` 11 |
| By severity | `MONITORING` 2,019 · `INFORMATIONAL` 151 · `ACTION_REQUIRED` 27 |
| By routing_status | `MONITORING` 2,019 · `SUPPRESSED` 139 · `DELIVERED` 28 · `NULL` 11 |

- All 28 delivered signals come from the news classifier; **discovery delivered nothing**.
- Delivered alerts are dominated by NVDA/GOOGL/AVGO/MSFT, almost all `ai_semi` / `other_growth`.

---

## Findings

### 🔴 Critical

**1. Outcome measurement was structurally broken — `signal_price_snapshot` NULL on all 28 delivered signals.** ✅ *Fixed in this task.*
The spec mandates price-at-signal for outcome backfill / IC measurement. It was only set in
`discovery_agent.py:132`, which emits zero signals; the news classifier path never captured it,
so `outcome_price_30d/90d` could never be computed. `outcome_backfill.py` exists but has never run.

**2. A full trading day was silently missed — Fri 2026-05-29 has no run record.**
This is precisely the failure mode the heartbeat exists to catch ("silent failure is
indistinguishable from no alerts today"). Verify Task Scheduler fired and that healthchecks.io
actually paged. Separately, clear the stuck `status='running'` row from the 05-30 news run.
*(Operational — not a clean code fix.)*

### 🟠 High

**3. Discovery pillar is dead weight — 0 signals in 11 runs.** ✅ *Fixed (see [below](#fix-applied-3)).*
Root cause was **not** threshold calibration. The scoring formula's volume factor (30% weight) reads
`quote["v"]`, but Finnhub free-tier `/quote` returns `c, d, dp, h, l, o, pc, t` — **no volume**. So
`fetch_quote`'s guard `if ... or v is None` rejected **every** ticker → `raw_quotes` empty → early
return → 0 signals on every run. `tickers_scanned=24-29` was just the input count, masking that 0
quotes survived. A Monte Carlo confirmed that with spread ranks, 100% of days *should* produce ≥1
signal — proving the inputs were collapsing, not the thresholds. (Every unit test mocked `fetch_quote`
with synthetic quotes containing `v`, so the suite stayed green while production was 100% broken.)

**4. Severe alert concentration / fatigue risk.**
Nearly every delivered alert is the AI-semis narrative (mostly NVDA). The budget caps *volume* but
nothing enforces ticker/theme diversity, so the single daily ACTION_REQUIRED slot is almost always
another NVDA headline.

**5. Duplicate article delivered as two signals.** ✅ *Fixed (see [below](#fix-applied-5)).*
"Nvidia's $5.7 Trillion Market Cap…" was delivered twice on 05-20 (NVDA *and* MSFT), consuming 2 of
3 INFORMATIONAL slots with one story. Dedup was keyed on (ticker, headline), not article identity.

### 🟡 Medium

**6. Mojibake in delivered alerts.** Smart quotes corrupted (`Nvidia�s`, `BofA�s`). Headlines
(U+2019 apostrophes) aren't handled as UTF-8 somewhere in fetch→store→deliver. The control-char
stripping in `_sanitize_headline` doesn't cover this.

**7. Two agent names for one job → DB bloat + broken time semantics.** ✅ *Fixed (see [below](#fix-applied-7)).*
The news job wrote volume-cap overflow as `agent='news_morning'`, `MONITORING` (timestamped with
**article publication time**) *and* classified candidates as `agent='news_classifier'` (run time).
This is why "weekend" rows existed (05-23: 197, 05-24: 198) and 05-29 showed 525 rows with no run.

**8. `DAILY_CLOSE` signals bypass the router** (11 rows, `routing_status=NULL`). They are just SPY
close markers but CLAUDE.md requires all signals to funnel through the router. Consider a separate
`market_state` table instead of polluting `signals`.

**9. Schema drift from spec.** CLAUDE.md mandates `candidate_signals`, `daily_budget`, and
`weight_version` — none exist. Severity `MONITORING` (2,019 rows) is stored in the *severity*
column, conflating severity with routing status.

### ⚪ Also noted (test/infra)

- **`test_smoke.py` has 26 pre-existing failures** unrelated to signals: `_make_test_thesis()`
  builds `Pillar(...)` with an old `keywords` field, but the `Pillar` model now requires
  `tickers`/`positive_signals`/`negative_signals` (changed in commit fa97f43). Tests were not updated.
- **`conftest.py` env setup isn't applied** in this environment and `.env` lacks `ANTHROPIC_API_KEY`,
  so `uv run pytest` fails at import unless env vars are supplied on the command line.

---

## What's working well ✅

- **Prompt caching:** 600,006 cache-read vs 16,587 cache-create tokens (~97% hit rate).
- **Router budget enforcement:** exactly 1 AR + 3 INFO every active day, no overflow.
- **Efficient batching:** 392 LLM calls produced 2,000+ classified items.

---

## Fix applied (Critical #1)

Captured the unadjusted price-at-signal in the news classifier so outcome backfill / IC
measurement is possible for the signals that actually deliver.

- **`src/signal_system/classifier/news_classifier.py`**
  - Added `_fetch_price_snapshot(ticker)` (uses `fetch_quotes`, never raises; returns `None`
    on missing/non-positive close — signal still emits, just without a snapshot).
  - `classify_headlines()` now stamps `signal_price_snapshot` on routable signals via
    `dataclasses.replace`, fetching the quote **once per ticker** and **only when at least one
    routable signal exists** (off-thesis tickers don't waste a Finnhub call). MONITORING exhaust
    is intentionally left unstamped (no outcome measurement).
  - Mirrors how `discovery_agent` stamps its own snapshot — keeps "agents emit complete Signals"
    as the invariant. Price flows unchanged through `route_signals` → `persist_routed_signals`
    → `repository.insert_signal` (which already persists the column).
- **`tests/test_news_classifier.py`** (new): routable signal carries snapshot; no quote call for
  off-thesis headlines; missing quote still emits a signal with `None` snapshot. **3 passed.**
- Full suite: no new regressions (the 26 `test_smoke.py` failures are the pre-existing Pillar drift).

> Note: this fixes capture going forward. The 28 already-delivered signals remain NULL and cannot
> be backfilled (the price-at-signal moment has passed).

### Fix applied (Finding #3) {#fix-applied-3}

Unblocked the Discovery data layer so the pillar produces signals at all.

- **`src/signal_system/data/finnhub_client.py`** — `fetch_quote` no longer requires `v` (volume),
  which Finnhub free-tier `/quote` never returns. It now validates only the fields the scorer uses
  (`dp` for momentum, sane `h`/`l` for range).
- **`src/signal_system/discovery/discovery_agent.py`** — the composite now includes the volume
  factor **only when every quote carries a numeric `v`** (e.g. a paid tier); otherwise it drops
  volume and **renormalises** the surviving weights so the composite stays 0–100 and the 60/80
  thresholds remain meaningful. `body`/`sub_scores` reflect the factors actually used.
- **`tests/test_discovery_agent.py`** — added T-02b (quote accepted without `v`) and T-02c
  (volume-less universe still emits a renormalized signal). 23 discovery tests pass.
- **Verified live:** a real Finnhub run now emits 5 signals (was 0), e.g. `MSFT 88.9 ACTION_REQUIRED
  weights=35/25/10`.

> Follow-up: Discovery is Phase A (log-only → MONITORING; router not connected), so these now flow
> to the monitoring log — exactly the intuition-building data Phase A was designed to collect. The
> Monte Carlo suggests 60/80 is loose for a ~25-ticker slice (~8/day clear 60); recalibrate (or move
> to percentile/top-K emission) before the Phase A→B transition that connects the router.

### Fix applied (Finding #5) {#fix-applied-5}

Dedup now collapses the same article surfaced under multiple tickers, so one story alerts once.

- **`src/signal_system/classifier/news_classifier.py`** — added `article_dedup_key(item)`: a
  ticker-independent identity preferring Finnhub's stable article `id`, falling back to the
  normalized headline when no id is present.
- **`src/signal_system/jobs/news_morning.py`** — `_dedupe_and_cap_headlines` now keys on
  `article_dedup_key(item)` instead of `(ticker, headline)`. The most-recent occurrence wins and
  later duplicates under other tickers are dropped (before the 50-headline cap and the classifier).
- **`tests/test_job_orchestration.py`** — added 3 tests: same headline across tickers collapses;
  same `id` across tickers collapses even with differing headline text; distinct articles for one
  ticker are both kept. Existing same-ticker cap/dedup behavior is unchanged.

### Fix applied (Finding #7) {#fix-applied-7}

Unified the news pillar under one agent name and corrected the overflow timestamp semantics.

- **`src/signal_system/classifier/news_classifier.py`** — added `NEWS_CLASSIFIER_AGENT =
  "news_classifier"` and routed all three classifier signal/alert-id sites through it, so the
  `agent` dimension can't drift again.
- **`src/signal_system/jobs/news_morning.py`** — volume-cap overflow signals now use
  `agent=NEWS_CLASSIFIER_AGENT` (was `'news_morning'`) and `timestamp=generated_at` (the run time,
  was the article's publication time). The article publication time is preserved in the body for
  audit. This stops the phantom weekend / no-run rows.
- **`tests/test_job_orchestration.py`** — the overflow test now asserts the rows are written under
  `news_classifier`, that the `timestamp` equals the run time (not the article time), and that
  **zero** rows are written under the legacy `news_morning` agent.

> Scope note: this is a forward-looking correctness fix. Historical rows keep `agent='news_morning'`
> with article-time timestamps — a one-off backfill/migration could relabel them, and the monitoring
> exhaust itself (an audit trail of capped headlines) is by design, so it was left in place rather
> than pruned.

---

## Suggested priority order (remaining)

1. ~~Verify the 05-29 miss + heartbeat~~ — **resolved:** Windows host was restarted; nothing to do.
2. **Schedule `outcome_backfill`** so the newly-captured snapshots turn into measured outcomes.
3. **Recalibrate Discovery thresholds** (or percentile/top-K) before connecting the router (Phase B).
4. Fix the UTF-8 mojibake; add article-level dedup; add ticker/theme diversity to the router.
5. Clean up the `news_morning`/`news_classifier` split and `timestamp` semantics.
6. Fix `test_smoke.py` Pillar construction and the `conftest`/`.env` test-env gap.
