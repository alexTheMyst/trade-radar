# Plan 09 — News Coverage & Volume Cap

**Type:** Fix · **Priority:** P1 · **Est. effort:** ~1 day · **Pairs with:** Plan 03

## Problem

On 2026-08-10, **339 of ~389 headlines (87%) were never classified.** They were written
straight to MONITORING as `[volume_cap]` without ever reaching the model. The system paid
Finnhub for them, stored them, and looked at none of them.

Worse, the discard is not uniform. It is biased toward whichever ticker generates the most
press.

### Evidence

```
ticker | classified | volume_capped
  NVDA |         30 |           187
 GOOGL |          6 |            44
 MACRO |         16 |            21
  MSFT |          7 |            29
  PLTR |          3 |            19
  AVGO |          2 |            13
  CRWV |          4 |             9
   BTG |          1 |             7
   LMT |          1 |             4
   NEM |          2 |             2
   SMH |          1 |             3
   NOC |          1 |             1
```

NVDA took 30 of 50 company slots. `BTG`, `LMT`, `NOC`, `SMH` got one apiece. If a material
event had broken on Northrop Grumman on Friday evening, its odds of reaching the classifier
were roughly one in five.

### Cause 1 — the cap is global, not per ticker

```python
_MAX_NEWS_HEADLINES = 50

kept_items, overflow_items = _dedupe_and_cap_headlines(candidates, seen=shared_seen)
```

`candidates` is every headline for **all 16 core tickers combined**. `_dedupe_headlines`
sorts by publication time descending and `[:cap]` takes the newest 50 overall. Ticker
identity plays no part. A high-volume name mathematically starves a low-volume one.

This is the core defect. The window width below makes it worse but is not the root cause.

### Cause 2 — the window is not always overnight

```python
previous_close_date = repository.get_latest_successful_run_date("daily-close")
```

The news window runs from the last **successful** `daily-close` to now. On a Monday that is
Friday 16:00 → Monday 09:00, ~65 hours, against a cap sized for a ~17-hour overnight window.

Two consequences worth separating:

- **Mondays are structurally the weakest day of the week.** Every Monday, ~3× the news
  volume competes for an unchanged cap.
- **`daily-close` failures silently widen the window further.** `daily-close` has 4
  lifetime failures. Each consecutive failure adds another day of news to a fixed-size cap,
  degrading news coverage as a side effect of an unrelated job failing. The coupling is
  invisible.

### Cause 3 — held tickers are not scanned at all

Owned by Plan 03, restated here because it compounds: 19 of 35 held tickers are not
`core_holding`, so no window or cap change helps them. `MU` is the live example — three
Micron headlines on 2026-08-10, all surfaced incidentally through NVDA's feed, all
misattributed (Plan 07), all suppressed.

## Goals

1. No ticker can starve another out of classification.
2. The cap adapts to window width rather than assuming overnight.
3. `daily-close` failure degrades news coverage **loudly** or not at all.

## Non-goals

- Eliminating the cap. A cap is correct — it bounds cost and latency. The defect is its
  shape, not its existence.
- Changing attribution (Plan 07) or which tickers are in the universe (Plan 03).

---

## Design

### 9a. Per-ticker cap with a global ceiling

Replace one global cap with a two-stage allocation:

```python
_MAX_PER_TICKER = 8        # newest N per ticker — guarantees a floor for quiet names
_MAX_TOTAL_COMPANY = 120   # global ceiling — bounds cost
_MAX_GENERAL = 20          # unchanged
```

1. Dedup globally first (unchanged — the shared `seen` set is correct and should stay).
2. Bucket by query ticker; take the newest `_MAX_PER_TICKER` from each.
3. If the union exceeds `_MAX_TOTAL_COMPANY`, drop from the largest buckets first until it
   fits. Never drop a ticker's last remaining headline.
4. Everything not kept becomes `[volume_cap]` MONITORING, as today.

With 16 core tickers this yields up to 128 candidates capped at 120, versus 50 today.
Against 2026-08-10's distribution every ticker would have cleared its backlog except NVDA,
GOOGL and MSFT — which is the correct outcome, since those three genuinely had more news.

### 9b. Window-aware sizing

Scale the per-ticker cap by window width in trading days:

```python
window_days = max(1, count_trading_days(previous_close, now_et))
per_ticker_cap = _MAX_PER_TICKER * min(window_days, 3)   # clamp so an outage can't explode cost
```

A Monday gets 3× per ticker. The clamp at 3 means a `daily-close` outage cannot produce an
unbounded classification bill — it degrades gracefully instead.

### 9c. Make window widening visible

Add to the digest, printed only when the window exceeds one trading day:

```
Window: 2026-08-07 16:00 → 2026-08-10 09:00 (3 trading days — extended)
```

And add a watchdog condition (Plan 01): if the news window exceeds 3 trading days,
`daily-close` has been failing and news coverage is degrading. Alert on it. This converts a
silent coupling into a stated one.

### 9d. Cost check before enabling

2026-08-10 baseline, from `llm_calls`:

| Metric | Value |
|---|---|
| Calls | 70 |
| Input tokens | 3,534 |
| Output tokens | 7,030 |
| Cache-read tokens | 171,948 |

Raising the effective cap from 70 to ~140 classified items roughly doubles calls and output
tokens. Input cost stays near flat because `thesis.yaml` is served from the prompt cache
(the 171,948 cache-reads confirm caching is working). Model the new figure and confirm it
is acceptable before merging — this is a deliberate spend increase, not an accident.

### 9e. Retention

653 MONITORING rows accumulated in 7 days at the current cap. Raising the cap moves volume
from `[volume_cap]` into classified signals rather than adding rows, so total growth should
be roughly flat — but confirm after one week. `weekly-synthesis` reads at most 200 of them;
the remainder is write-only. If growth is material, add a retention policy for
`volume_cap`-reason rows older than 90 days. Do not delete classified MONITORING rows —
they are the pillar-candidate corpus.

---

## Testing

- **Unit:** per-ticker allocation gives a 1-headline ticker its headline even when another
  ticker has 200.
- **Unit:** global ceiling drops from the largest buckets first and never empties a bucket.
- **Unit:** `window_days` clamp holds at 3 for a 10-day window.
- **Unit:** trading-day counting handles a weekend and a market holiday correctly, in
  `America/New_York`.
- **Integration:** replay 2026-08-10's candidate set through the new allocator and assert
  every core ticker receives at least one classified headline. Attach the before/after
  per-ticker table as the acceptance artifact.
- **Regression:** overflow rows still carry `[volume_cap]` and the run-time timestamp, not
  the article timestamp — the phantom-weekend-row bug documented in
  `_make_overflow_monitoring_signal`.

## Acceptance criteria

- [ ] Every core ticker with ≥1 windowed headline gets ≥1 headline classified.
- [ ] No ticker consumes more than its per-ticker cap while another is starved.
- [ ] Digest states the window span whenever it exceeds one trading day.
- [ ] Watchdog alerts when the window exceeds 3 trading days.
- [ ] Post-change `llm_calls` cost is within the modelled estimate from §9d.
- [ ] Before/after per-ticker distribution table attached to the phase summary.
