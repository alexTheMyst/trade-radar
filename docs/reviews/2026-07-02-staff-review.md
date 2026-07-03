# Staff Review — 2026-07-02

Scope: full read of the signal path (~3,900 LOC) — repository, alert router, reconciler,
news classifier, discovery agent, weight amplifier, advisor (verdict engine, agent, job),
outcome backfill, data clients, jobs, delivery, config. Lens: clarity and effectiveness of
signals for a solo operator whose core value is "never miss a material thesis-relevant event
on a held position."

Overall: the architecture is sound — heartbeat discipline, explicit no-signal days,
fail-loud loaders, INSERT OR IGNORE idempotency, deterministic verdicts with LLM-cosmetic
rationale. The critical findings cluster in the two places that matter most for a signal
system: the **outcome measurement loop** and the **advisor's input hygiene**.

---

## Bugs (ranked by severity)

### CRITICAL

#### B1 — Outcome backfill records today's price as both the 30d and 90d outcome

`src/signal_system/jobs/outcome_backfill.py:69` (and `:108` for the advice mirror)

When a candidate row is "due" (`timestamp <= now - 30d/90d`), the fill value is
`fetch_quote(ticker)` — **the price at whatever moment the backfill job happens to run**,
not the price 30/90 days after the signal.

Consequences:

- Any signal first measured ≥ 90 days after emission gets
  `outcome_price_30d == outcome_price_90d == today's close` — two identical "outcomes."
- MEAS-02 intentionally activates the job ~30 days post-go-live
  (`ops/windows-task-scheduler.md`), so the very first activation run measures a backlog
  late. Any subsequent downtime skews measurements further.
- Every downstream number in the quarterly IC review inherits this bias silently.

**Fix:** fetch historical daily closes via `yahoo_client.fetch_history` and select the close
on the first trading day ≥ `signal_date + 30/90`; never fill from a same-day quote. Only
fill a horizon once that horizon's dated close actually exists.

#### B2 — Advisor news axis is diluted by non-news signals

`src/signal_system/jobs/advisor.py:96`

The advisor calls `repository.get_recent_signals(ticker, since)` with **no `agent` filter**.
The query then returns:

- `DAILY_CLOSE` rows — SPY, one per trading day, `direction=None`, `score` = SPY *price*.
- `discovery_agent` rows — `direction=None`, score 0–100.

In `compute_news_net = sum(direction × confidence) / len(signals)`
(`advisor/advisor_agent.py:43`), each such row contributes 0 to the numerator and +1 to the
denominator. For SPY, ~10 daily-close rows per 14-day lookback guarantee the news axis is
effectively always neutral; any held ticker that appears in discovery scans is diluted the
same way. The verdict matrix's news axis is disabled for exactly the most-watched tickers.

**Fix:** pass `agent=NEWS_CLASSIFIER_AGENT` — the repository function already supports it
(`state/repository.py:472`). Same fix applies to the on-demand path at `jobs/advisor.py:155`.

#### B3 — Reconciliation losers leak back into the advisor

`src/signal_system/jobs/news_morning.py:228` + `src/signal_system/state/repository.py:490`

Reconciled losers are persisted with `routing_status='MONITORING'` but keep their
**original severity** (e.g. INFORMATIONAL). `get_recent_signals` filters on
`severity != 'MONITORING'`, so the losing direction of a contradicted story re-enters
`news_net` and partially cancels the winner — undoing reconciliation exactly where it was
meant to help. The docstring ("Excludes MONITORING signals — parse failures, off-thesis
exhaust") does not match behavior.

**Fix:** filter on `routing_status != 'MONITORING'` (or additionally exclude
`demoted_from = 'reconciled'`). Note margin-guard-downgraded winners *are* excluded
(their severity is replaced to MONITORING, `reconciler.py:113-117`) — the inconsistency is
only for plain losers.

### HIGH

#### B4 — Weight amplifier makes ACTION_REQUIRED unreachable for small held positions

`src/signal_system/scoring/weight_amplifier.py:72-77`

Below-median position weight raises thresholds by up to 20 points
(clamp 0.25 → `10 × log2(0.25) = −20` shift). News classifier AR base is 85
(`news_classifier.py:33`), so the effective AR threshold becomes **105 on a 0–100 scale**:

- A confirmed thesis-break headline (confidence 1.0 → score 100) on any held position with
  weight ≤ median/4 can **never** be ACTION_REQUIRED.
- The same position needs confidence ≥ 0.80 just to reach INFORMATIONAL.
- Inversion: a ticker *absent* from `universe.csv` gets base thresholds (shift 0,
  `weight_amplifier.py:66-67`) — better treatment than a small held position.

This directly contradicts the project's core value. Discovery has the same ceiling (base 80
→ 100): only a ticker ranked #1 on all three factors can fire AR.

**Fix:** cap raised thresholds at < 100 (or clamp negative shift to −10), and give
thesis-break signals (direction=negative, confidence ≥ 0.85) a severity floor of
ACTION_REQUIRED regardless of weight.

#### B5 — Telegram failure permanently loses delivered alerts

`src/signal_system/jobs/common.py:24-42`; call order at `news_morning.py:229→245`,
`discovery.py:38→53`

Signals are persisted with `routing_status='DELIVERED'` **before** `send_message` runs. If
the Telegram call raises (outage, bad token, timeout), the job fails loudly (heartbeat
/fail — good), but the DB already claims DELIVERED. On re-run, `count_delivered_today`
counts the phantom rows, the router treats the budget as consumed, and the same signals are
SUPPRESSED. The operator never receives them, and nothing ever re-sends them.

**Fix options (pick one):**
- Persist as `PENDING`, send, then flip to `DELIVERED` (schema-compatible; router counts
  `PENDING` + `DELIVERED` against budget to stay conservative), or
- On re-run, re-send today's `DELIVERED` rows that lack a send confirmation marker.

#### B6 — Router suppresses outscored ACTION_REQUIRED signals instead of demoting

`src/signal_system/router/alert_router.py:63-71`

CLAUDE.md: "if two agents compete for ACTION_REQUIRED, higher-scored wins, **others
demote**." The implementation marks losers `SUPPRESSED` outright. An AR signal that loses
the single daily slot does not compete for the 3 INFORMATIONAL slots — the #2 opportunity of
the day silently disappears from delivery. (Also doc drift: CLAUDE.md says suppressed rows
are "tagged MONITORING"; code writes `SUPPRESSED`.)

**Fix:** demote outscored AR signals to INFORMATIONAL, let them compete for INFO slots, and
record `demoted_from='ACTION_REQUIRED'`. Tests in `tests/test_alert_router.py` pin the
current behavior and would be updated deliberately.

### MEDIUM

#### B7 — Job re-runs can re-send already-delivered alerts

`src/signal_system/jobs/common.py:30` — `persist_routed_signals` ignores the boolean from
`insert_signal()` (False = row already existed). On a same-day manual re-run with INFO
budget remaining, the same top-ranked signals route DELIVERED again and are re-sent to
Telegram; the DB row is unchanged (INSERT OR IGNORE), so there is no audit trail of the
duplicate. **Fix:** skip delivery (and digest inclusion) when `insert_signal` returns False.

#### B8 — `fetch_spy_close` has no retry

`src/signal_system/data/finnhub_client.py:119-126` — the only fetcher without
`_RETRY_DECORATOR`. A transient 429/connection error fails `daily-close`, and a failed
daily-close blocks the next morning's `news-morning` run
(`news_morning.py:190-194` requires a prior successful daily-close). **Fix:** apply the
same retry decorator used by quotes/news.

#### B9 — Cross-day duplicate alerts after a missed daily-close

`src/signal_system/classifier/news_classifier.py:170-174, 351-354` — `headline_dedup_key`
and the alert_id embed the **run date** (`datetime.now(_ET).date()`). Dedup is otherwise
in-memory per run. If daily-close fails one evening, the next morning's news window extends
back past already-classified headlines; the same stories are re-classified (paid API calls)
and re-alerted under fresh alert_ids. **Fix:** key dedup/alert_id on the article's published
date or Finnhub article id (already used by `article_dedup_key`) rather than run date.

#### B10 — Discovery momentum windows are mislabeled

`src/signal_system/discovery/discovery_agent.py:74-79` — over 20 rows, `momentum_20d` spans
19 trading-day returns; `momentum_5d` (`idx = n−5`) is a 4-day return. Cross-sectional
ranking is unaffected (consistent bias), but the labels the operator reads in alert bodies
overstate the window by one day.

### LOW / DOC DRIFT

- **B11** — CLAUDE.md still documents Gmail SMTP + `delivery/email_sender.py`; the actual
  delivery layer is Telegram (`delivery/telegram_sender.py`). Documented discovery weights
  35/30/25/10 vs actual 50/30/20 (the deviation is explained in the module docstring, but
  CLAUDE.md was never updated). Router suppression tagging drift noted under B6.
- **B12** — Off-thesis classifications persist nothing (`classify_headline` returns None,
  `news_classifier.py:396-397`), so classifier misses cannot be audited later;
  `get_recent_signals`'s docstring references "off-thesis exhaust" MONITORING rows that are
  never written.
- **B13** — `data/thesis_loader.py:78` uses `date.today()` (machine-local timezone) for the
  `review_due` staleness check, violating the ET-everywhere convention. Cosmetic docstring
  rot in `finnhub_client.fetch_quote` (`:100-106`): describes a volume-factor
  renormalization that no longer exists in the scorer.

---

## Three improvements to maximize signal clarity & effectiveness

### 1. Fix the measurement loop end-to-end

Beyond B1's date-anchored historical closes, two design gaps keep the feedback loop from
ever becoming trustworthy:

- **Selection bias:** `list_outcome_backfill_candidates` gates on `acted IS NOT NULL`
  (`state/repository.py:374`) — only operator-reviewed rows are measured. You cannot know
  whether ignoring a signal was correct unless ignored signals are measured too. Measure
  **all routable signals** (DELIVERED and SUPPRESSED); keep `acted` as an annotation, not a
  filter.
- **Activation:** the job is not wired into `__main__.JOBS`. Register an
  `outcome-backfill` command (still schedule it per MEAS-02 timing) so activation is a
  scheduler change, not a code change.

Every threshold in this system — 0.85/0.60 confidence bands, 80/60 discovery cutoffs,
amplifier shifts, the verdict matrix — is a documented "initial guess." Without clean
outcomes, the quarterly review can only re-guess.

### 2. Make ACTION_REQUIRED mean one thing: "act today"

Today the scarce daily AR slot can be claimed by two incomparable things:

- **Discovery's composite is a pure cross-sectional rank** (`discovery_agent.py:122-137`):
  the best of ~500 tickers scores ~100 *by construction*, every scan day, even when the
  entire universe is falling. "Best of a bad lot" is AR-eligible daily.
- **Scores live on different scales** — news 0–1, discovery 0–100 — so CLAUDE.md's
  "higher-scored wins" slot competition is unimplementable across agents. In practice the
  9:00 AM news job wins the AR slot by schedule timing, not merit.

Fixes: (a) add absolute gates to discovery AR — e.g. require `momentum_20d > 0` and price in
the upper half of its 20d range — so rank alone can't fire AR; (b) give news thesis-breaks an
explicit severity floor (pairs with the B4 threshold cap); (c) normalize both agents onto a
single 0–100 scale so slot competition, `get_recent_signals` consumers, and the quarterly
review compare like with like.

### 3. Feed the advisor clean, unstarved inputs

The two-axis verdict engine is only as good as `news_net` and the candidate funnel:

- Apply the B2 agent filter and B3 routing-status filter so `news_net` reflects only valid,
  reconciled, on-thesis news.
- **Widen the new-buy funnel:** `get_delivered_discovery_signals`
  (`state/repository.py:511`) reads only DELIVERED rows — at most 4/day *shared with news* —
  so the advisor's candidate pool is starved by a **delivery** budget that was never meant to
  constrain **analysis**. Read candidates from all routable discovery signals
  (DELIVERED + SUPPRESSED); the alert budget should govern what interrupts the operator,
  not what the advisor is allowed to think about.

---

## Suggested fix order

| Order | Items | Why first |
|-------|-------|-----------|
| 1 | B2, B3, B8 | One-liners with existing test coverage (`test_advisor_job.py`, `test_reconciler.py`); immediate signal-quality gain |
| 2 | B1 (+ Improvement 1) | Must land before MEAS-02 activation or early outcomes are permanently polluted |
| 3 | B4, B5, B6 | Small design decisions (threshold cap value, PENDING status, demotion semantics) — worth a short discuss-phase each |
| 4 | B7, B9, B10, docs | Operational hardening and drift cleanup |
