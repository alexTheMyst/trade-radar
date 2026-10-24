# Plan 12: Weekly Review Fixes (week of 2026-10-19)

**Type:** Fix · **Priority:** P0 (12a), P1 (12b, 12c, 12e), P2 (12d, 12f) · **Est. effort:** ~5 days
**Source:** live review of `state/signals.db` on 2026-10-24 (Sat), covering Mon 2026-10-19 to Fri 2026-10-23.
**Relates to:** Plan 03 (holdings/universe), 04 + 05 (advisor / wash-sale retirement), 07 (attribution), 09 (volume cap), 10 (measurement). Where this plan overlaps an earlier one, this plan states which one wins.

---

## 1. Summary

Operationally the week was clean: **45 of 45 runs succeeded**, backups rotated, prompt caching
is working (839,804 cache-read tokens vs 16,093 uncached input). The defects are all in *what
gets delivered*, not in whether jobs run:

| # | Finding | Severity |
|---|---------|----------|
| 12a | Exit Sentinel alerts on held positions are never delivered (0 of 14 this week, 7 of them ACTION_REQUIRED). Last sentinel delivery: 2026-09-25. | **Class A/B, violates core value** |
| 12b | 74% of news headlines (973 of 1,312) never reach the classifier; NVDA alone lost 389. | Coverage gap |
| 12c | 4 of 5 delivered ACTION_REQUIRED alerts were tickerless macro headlines (3 about oil); one NVDA story was delivered twice (10-20, 10-23). | Alert quality |
| 12d | Discovery produced 374 signals and delivered 0; it is a de-facto shadow agent that still looks like a competitor. | Honesty of the digest |
| 12e | `acted` has never been filled on any signal (0 ever); 336 delivered signals older than 7 days are unreviewed; `wash_sale` has 0 rows; advisor last ran 2026-06-02. | Measurement loop dead |
| 12f | 1,266 rows written this week carry `routing_regime = NULL` and are indistinguishable from legacy rows. `outcome-backfill` runs daily but the watchdog ignores it. | Measurement hygiene |

### Evidence queries (reproducible)

```sql
-- Week routing outcome per agent
SELECT agent, severity, routing_status, count(*) FROM signals
 WHERE timestamp >= '2026-10-19' GROUP BY 1,2,3;
-- news_classifier DELIVERED 20 (5 AR, 15 INFO); exit_sentinel DELIVERED 0; discovery_agent DELIVERED 0

-- Sentinel lifetime
SELECT routing_status, count(*) FROM signals
 WHERE agent='exit_sentinel' AND routing_status != 'MONITORING' GROUP BY 1;
-- DELIVERED 12, SUPPRESSED 166; max(timestamp) of DELIVERED = 2026-09-25

-- Feedback loop
SELECT count(*) FROM signals WHERE acted IS NOT NULL;                                  -- 0
SELECT count(*) FROM signals WHERE routing_status='DELIVERED' AND acted IS NULL
  AND julianday('now') - julianday(substr(timestamp,1,19)) > 7;                         -- 336
```

---

## 2. Operator decisions (2026-10-24)

| Decision | Choice |
|----------|--------|
| Sentinel delivery model | **Option B**: sentinel gets its own ACTION_REQUIRED lane **and** one reserved INFO slot for fresh drawdown / day-drop triggers. Below-cost-only triggers keep competing in the normal pool. |
| News volume cap | **Per-ticker round-robin, held tickers first**, global ceiling raised from 50 to 120. Supersedes Plan 09 §9a; Plan 09 §9b/§9c (window sizing, window visibility) are folded in below. |
| Action logging | **Telegram inline buttons** (Acted / Passed) on every delivered alert, plus reply-to-add-note, plus a weekly re-prompt. |

### Defaults chosen by this spec (override before implementation if you disagree)

| Topic | Default | Alternative |
|-------|---------|-------------|
| Discovery | Explicit **shadow mode** (logged and measured, never delivered) | Keep competing for INFO at last priority (status quo) |
| Tickerless macro news | **Capped at INFORMATIONAL** | Map to the pillar's highest-weight held ticker |
| Trade logging / wash-sale | **Execute Plans 04 + 05 Option A** (retire advisor and ledger; Schwab is the system of record) | Plan 05 Option B (Schwab CSV importer) as a separate plan |
| Backlog of 336 unreviewed | **Bulk-close** per Plan 10 §10d with an excluding `user_note` | Review manually |

---

## 3. Cross-cutting

- All conventions in `docs/plans/README.md` §"Cross-cutting conventions" apply (stdlib only, `repository.py` owns SQL, heartbeat + `runs` row per job, ET timezone, fail loud).
- **No classifier prompt or output-schema change** in this plan, so the `eval-classifier` gate is not tripped. 12c is enforced in the router and job layer, not in the prompt.
- **Routing regime bump.** 12a, 12c and 12d change routing semantics. All rows written after rollout carry `routing_regime = 'deferred-info-priority-v2'` (see 12f). Never aggregate v1 and v2 rows.
- New schema is additive only, via the existing idempotent `_add_column` helper in `repository.py` and `CREATE TABLE IF NOT EXISTS` in `init_db()`.
- Entered through `/gsd-execute-phase` (one phase, six plans) or one `/gsd-quick` per section.

---

## 12a. Sentinel delivery (P0)

### Problem

This week the sentinel raised these and none reached the operator:

| Date | Ticker | Raised as | Trigger (title) | Outcome |
|------|--------|-----------|-----------------|---------|
| 10-19 | CAN | AR | below_cost 79.5%, drawdown_20d 48.3% | demoted → outscored |
| 10-19 | JNUG | AR | below_cost 41.6%, drawdown_20d 30.3% | demoted → outscored |
| 10-21 | CRWV | AR | drawdown_20d 33.7% | demoted → outscored |
| 10-22 | AVGO | AR | drawdown_20d 15.1% | demoted → outscored |
| 10-23 | CDE | AR | drawdown_20d 30.6% | demoted → outscored |
| 10-23 | MU | AR | drawdown_20d 20.8% | demoted → outscored |
| 10-23 | T | AR | below_cost 27.6%, drawdown 16.8%, day_drop 8.0% | demoted → outscored |

Meanwhile the operator received positive MU news alerts on 10-21 (AR) and 10-22 while MU was entering a 20.8% drawdown.

### Root causes

1. **Shared AR budget, first come.** `BUDGET_AR = 1` is global. `news-morning` (09:00) always spends it; `sentinel` (17:00) finds 0 remaining in `stage_signals()` and demotes every AR to a CANDIDATE.
2. **Dispatch priority starves the sentinel.** `AGENT_PRIORITY` ranks news first. News produced ≥3 INFO candidates every day, so all 3 INFO slots went to news. Sentinel candidates, including demoted ARs, were `outscored`.
3. **Cooldown counts undelivered alerts.** `is_cooldown()` treats any non-MONITORING prior row as "already alerted". `get_signal_meta_by_alert_ids()` returns SUPPRESSED rows, so a sentinel alert that was never sent still starts a 5-trading-day cooldown. 58 of this week's sentinel rows were `[cooldown]` downgrades, and since no sentinel signal was delivered after 2026-09-25, none of those cooldowns followed a real delivery.

### Design

**a1. Per-agent ACTION_REQUIRED lanes** (`router/alert_router.py`)

```python
BUDGET_AR_BY_AGENT: dict[str, int] = {
    NEWS_CLASSIFIER_AGENT: 1,
    EXIT_SENTINEL_AGENT: 1,
    DISCOVERY_AGENT: 0,      # shadow mode, see 12d
}
BUDGET_INFO: int = 3        # unchanged, shared
```

- `repository.count_delivered_today(agent: str | None = None)`: optional agent filter; existing callers unchanged.
- `stage_signals()`: compute `ar_remaining` per `signal.agent` lane. Winners DELIVERED, losers demoted to CANDIDATE with `demoted_from='ACTION_REQUIRED'` exactly as today.
- Within the sentinel lane, rank by the unclamped severity ratio defined in a2 (not `competition_score`, which is pinned at 100 for every severe breach), then ticker.
- Keep `BUDGET_AR` as a derived constant (`sum(BUDGET_AR_BY_AGENT.values())`) for digest validation; max ARs per day becomes 2.
- `route_signals()` (legacy, used by tests and replays) is not changed.

**a2. Reserved sentinel INFO slot** (`dispatch_candidates()`)

- New column `signals.trigger_json TEXT`: sentinel `build_signal()` writes the breached triggers and magnitudes, e.g. `{"drawdown_20d": 0.483, "below_cost": 0.795}`. NULL for other agents.
- A sentinel candidate is **fresh** iff `trigger_json` contains `drawdown_20d` or `day_drop_1d`. Below-cost-only candidates are not fresh (they are the "long-standing losers" the 2026-10-16 replay showed crowding out news).
- Dispatch order:
  1. If `info_remaining > 0` and at least one fresh sentinel candidate exists, deliver the best one first. Rank fresh candidates by: `demoted_from='ACTION_REQUIRED'` first, then **unclamped** severity ratio `max(drawdown_20d / 0.15, day_drop_1d / 0.08)` descending, then ticker. (The 0-100 score saturates at 100, so ranking by score alone degenerates into alphabetical order.)
  2. Remaining slots: existing `dispatch_rank_key()` (agent priority, then score) with the 12c news ordering.
- Constant `SENTINEL_RESERVED_INFO: int = 1`. If no fresh sentinel candidate exists, the slot falls through to news (no wasted budget).

**a3. Cooldown counts deliveries only** (`sentinel/exit_sentinel.py`)

- `is_cooldown()` ignores prior rows whose `routing_status` is not `DELIVERED` (a PENDING row may be a stuck failed send the operator never saw; amended after logic review). The repository function stays unchanged; the filter lives in the pure function so it is unit-testable.
- Escalation rule unchanged: a new AR always breaks through a prior INFO-only delivery.
- **Expected one-time effect:** for the first week after rollout roughly 10 to 15 tickers per day will trigger instead of cooling down. The lane (1 AR) plus reserved slot (1 INFO) bounds delivery at 2 sentinel alerts per day; delivered tickers then cool down and the rotation reaches the rest.

**a4. Digest visibility**

- Sentinel digest (17:00): `AR lane: delivered <TICKER> | demoted N`.
- Dispatch digest (17:40): always print a section `Held-position exits not delivered (N)` listing every sentinel candidate that was SUPPRESSED, regardless of `MAX_SUPPRESSED_LISTED`. Plus `Sentinel reserved slot: <TICKER>` or `unused (no fresh trigger)`.
- `validate_dispatch()`: assert at most `SENTINEL_RESERVED_INFO` delivered via reservation and that the section count equals the DB count of suppressed sentinel rows for the date.

### Expected result (replay of this week, ignoring the a3 cooldown change)

| Date | Sentinel AR (lane) | Sentinel INFO (reserved) | News INFO slots |
|------|--------------------|--------------------------|-----------------|
| 10-19 | CAN (drawdown 48.3%) | JNUG (drawdown 30.3%) | 2 |
| 10-20 | none | none | 3 |
| 10-21 | CRWV (33.7%) | none | 3 |
| 10-22 | AVGO (15.1%) | BTG (day_drop 7.2%) | 2 |
| 10-23 | CDE (30.6%) | MU (20.8%) | 2 |

### Files

`router/alert_router.py`, `jobs/dispatch.py`, `jobs/sentinel.py`, `sentinel/exit_sentinel.py`, `state/repository.py` (`count_delivered_today`, `trigger_json` column, candidate readback includes `trigger_json`), `models.py` (`Signal.trigger_json: str | None = None`).

### Tests

- `stage_signals`: news AR already delivered today → sentinel AR still DELIVERED; second sentinel AR same day → demoted CANDIDATE.
- `dispatch_candidates`: 5 news + 1 fresh sentinel → sentinel delivered + 2 news; 5 news + 1 below-cost-only sentinel → 3 news; two fresh sentinels at score 100 → higher unclamped ratio wins.
- `is_cooldown`: prior SUPPRESSED row → not cooldown; prior DELIVERED INFO + new INFO → cooldown; prior DELIVERED INFO + new AR → not cooldown.
- Replay test fixture built from the 10-19..10-23 candidate rows asserting the table above.
- `validate_dispatch` fails when the "not delivered" section omits a suppressed sentinel row.

### Acceptance criteria

- [ ] On any day with a sentinel AR, that AR is delivered unless another sentinel AR won the lane.
- [ ] On any day with a fresh sentinel candidate and INFO budget remaining at dispatch, one sentinel INFO is delivered.
- [ ] No `[cooldown]` row is written for a ticker with no DELIVERED sentinel row in the prior 5 trading days.
- [ ] Every suppressed sentinel candidate is named in the dispatch digest.

---

## 12b. News coverage: per-ticker round-robin, held first (P1)

### Problem

| Metric (week) | Value |
|---|---|
| News rows written | 1,312 |
| `volume_cap` (never classified) | 973 (74%) |
| Top capped tickers | NVDA 389, GOOGL 129, CRWV 124, MU 68, MSFT 53, AVGO 52, LMT 29, TSM 27 |
| Monday 10-19 | 510 rows, 440 capped (weekend window, same cap) |

Cause is unchanged from Plan 09 §Cause 1: `_dedupe_and_cap_headlines()` takes the newest 50 across all tickers combined (`_MAX_NEWS_HEADLINES = 50`), so a high-volume name starves the rest.

### Design (supersedes Plan 09 §9a, incorporates §9b and §9c)

New function in `jobs/news_morning.py`:

```python
_MAX_PER_TICKER = 8           # base, per trading day in the window
_MAX_TOTAL_COMPANY = 120      # global ceiling (was 50)
_WINDOW_MULTIPLIER_CAP = 3    # clamp so a daily-close outage cannot explode cost
_MAX_GENERAL_HEADLINES = 20   # unchanged

def allocate_headlines(
    unique: list[tuple[str | None, dict, datetime]],   # already globally deduped, newest first
    *,
    held: set[str],
    window_days: int,
) -> tuple[list, list]:                                  # (kept, overflow)
```

Algorithm:

1. Global article-identity dedup with the shared `seen` set (unchanged).
2. Bucket by query ticker, each bucket newest first.
3. `per_ticker_cap = _MAX_PER_TICKER * min(window_days, _WINDOW_MULTIPLIER_CAP)`.
4. Bucket order: tickers in `held` (from `data/holdings.load_holdings()`) first, then the rest; alphabetical within each group for determinism.
5. Round-robin passes: each pass takes the next-newest item from every bucket that is under `per_ticker_cap`, in bucket order, until the ceiling is reached or all buckets are exhausted. Held tickers therefore always get their first item before any non-held ticker gets its second.
6. Everything not kept → `[volume_cap]` MONITORING, unchanged (`_make_overflow_monitoring_signal`, run-time timestamp).

`window_days = max(1, ceil((now_et - previous_close).total_seconds() / 86400))`, both in America/New_York. Elapsed time, not trading days, is what drives headline volume (a normal overnight window is ~17h → 1; Fri 16:00 → Mon 09:00 is 65h → 3; one missed `daily-close` gives ~41h → 2). This avoids needing a market-holiday calendar, which the codebase does not have.

Digest additions:

- When `window_days > 1`: `Window: <start> → <end> (<hours>h, extended)` (Plan 09 §9c).
- Coverage line for held tickers that overflowed: `Coverage (held, classified/available): NVDA 8/97, GOOGL 8/31, ...`.
- The Plan 09 §9c watchdog condition (window > 3 trading days) stays in Plan 09.

### Cost model

Simulated against this week's per-ticker volumes: classified company headlines rise from **250 to ~400** (~1.6x). This week's telemetry was 342 classifier calls, 16,093 input, 42,265 output, 839,804 cache-read tokens. Expect roughly 1.6x calls and output tokens; uncached input stays small because `thesis.yaml` is cached. Confirm the first full week against this baseline (acceptance item below).

### Interaction with other plans

- Plan 03 (held tickers missing from the core scan set) is still required: about half of `holdings.csv` produces no news candidates because those tickers are never queried. Held-first ordering only helps tickers that are scanned.
- Plan 07 (subject attribution) unchanged; allocation uses the query ticker.

### Tests

- 1-headline held ticker keeps its headline while NVDA has 200.
- Held bucket gets item 1 before any non-held bucket gets item 2.
- Ceiling stops allocation exactly at 120; no bucket exceeds `per_ticker_cap`.
- `window_days`: 17h → 1; Fri 16:00 → Mon 09:00 (65h) → 3; 10-day window → per-ticker cap clamps at 3x.
- Replay 10-19 candidates: every scanned held ticker gets ≥1 classified headline; attach the before/after per-ticker table to the phase summary.

### Acceptance criteria

- [ ] Every scanned ticker with ≥1 windowed headline gets ≥1 classified.
- [ ] Held tickers are allocated before non-held tickers in every pass.
- [ ] First full week's `llm_calls` within 2x of this week's baseline.
- [ ] Digest shows window span when > 1 trading day, and the held coverage line when anything overflowed.

---

## 12c. News alert quality (P1)

### c1. Tickerless (macro) news cannot be ACTION_REQUIRED

Evidence: this week 4 of 5 delivered ARs were macro headlines with `ticker IS NULL` (10-19 "AI trade reversal pierces Asia's star picks-and-shovels stocks"; 10-20, 10-22, 10-23 Brent/oil moves). Last 30 days: 5 of 19 delivered ARs, 8 of 32 AR-level news signals. These cannot be acted on directly, carry no price snapshot, and are invisible to outcome backfill.

Design: in `stage_signals()`, before lane competition, any `news_classifier` signal with `severity == 'ACTION_REQUIRED'` and `ticker is None` is demoted to INFORMATIONAL CANDIDATE with `demoted_from = 'macro_cap'`. Router-level rule, so no prompt change and no eval gate. The audit trail survives in `demoted_from`.

### c2. News ordering within dispatch

Extend `dispatch_rank_key()` so that, inside the news priority band, order is:

1. held ticker (in `holdings.csv`)
2. non-held ticker
3. macro (`ticker is None`)

then `competition_score` descending, then ticker. Other agents unchanged.

### c3. Cross-day repeat-story filter

Evidence: NVDA "$35B RTX PRO 5500 order" delivered 10-20 and again 10-23 under a different headline.

Design: new pure function in `classifier/news_classifier.py` (no LLM, no new deps):

```python
def story_tokens(title: str, ticker: str) -> set[str]: ...
def is_repeat_story(candidate: Signal, recent_titles: list[str], *, threshold: float = 0.5) -> bool: ...
```

- Tokens: strip the `pillar: ` prefix, lowercase, split on `[a-z0-9$.']+`, drop stopwords and 1-char tokens, drop the ticker's own company aliases (constant `_COMPANY_ALIASES` map, e.g. `NVDA → {nvidia, nvda}`), drop the final token when the title is at the 120-char truncation limit.
- Similarity: overlap coefficient `|A ∩ B| / min(|A|, |B|)`; skip comparison when either set has fewer than 4 tokens.
- Compared against: news rows for the same ticker with `routing_status IN ('DELIVERED','PENDING')` in the last 7 calendar days (new `repository.list_recent_delivered_titles(ticker, since)`), plus higher-ranked routable signals from the same run.
- A repeat is persisted as MONITORING, `monitoring_reason='repeat'`, title prefix `[repeat] `. It never enters `stage_signals()`, so it cannot consume an AR.
- Add `repeat` to `_KNOWN_MONITORING_REASONS`, the digest breakdown, and `count_monitoring_by_reason`. Weekly synthesis is unaffected (it allow-lists `off_thesis` and `volume_cap`).

Validation already done: run against all 58 delivered tickered news signals since 2026-08-20, threshold 0.5 flags exactly 2 pairs, both true repeats (the NVDA $35B story at 0.70; an AMD "$1 Trillion Club" story at 0.55). Zero false positives; the next-highest pair scored below 0.34.

### Tests

- Macro AR → CANDIDATE with `demoted_from='macro_cap'`; tickered AR unaffected.
- Dispatch ordering: held 0.80 beats non-held 0.90 beats macro 0.95.
- `is_repeat_story`: the two known pairs → True; MU 10-21 "Micron Stock Jumps..." vs MU 10-22 "Micron Posted a $54.23 Billion Quarter..." → False; LMT 10-20 vs 10-22 → False.
- Digest monitoring breakdown sums with the new `repeat` bucket and matches the DB readback.

### Acceptance criteria

- [ ] No delivered AR has `ticker IS NULL`.
- [ ] Replay of 10-23 news marks the NVDA $35B story as `repeat`.
- [ ] No regression in the existing digest-integrity tests.

---

## 12d. Discovery: explicit shadow mode (P2)

### Problem

374 Discovery signals this week, 0 delivered, all `suppressed: outscored`. Since 2026-10-16 Discovery ranks last by design because its scores are not predictive. The digest shows it as a competitor that keeps losing, which reads as a fault.

### Design

- `config.DISCOVERY_MODE = _optional("DISCOVERY_MODE", "shadow")`, values `shadow | compete`.
- In `shadow`, `jobs/discovery.py` persists every signal directly as `routing_status='SUPPRESSED'`, `suppression_reason='shadow'`, stamped with the routing regime. Nothing is staged as CANDIDATE. Outcome backfill already covers `SUPPRESSED`, so measurement continues unchanged.
- Discovery digest: `Shadow mode: scanned N, signaled M, 0 eligible for delivery. Top 3: <ticker score>, ...`.
- Dispatch digest agent counts show `discovery_agent 0` pooled plus one line `Discovery (shadow): M logged today`.
- `BUDGET_AR_BY_AGENT[DISCOVERY_AGENT] = 0` (12a). In `compete` mode it becomes 0 for AR still and competes for INFO at last priority (status quo).
- Re-enabling is an operator decision at quarterly review using `review-report` and `replay-weights`.

### Tests

- Shadow mode: no CANDIDATE rows from discovery; all rows SUPPRESSED/`shadow`; digest validation passes against DB readback.
- Compete mode: behavior identical to today.

### Acceptance criteria

- [ ] `suppression_reason='outscored'` never appears on a discovery row in shadow mode.
- [ ] `review-report` still produces a Discovery readout from shadow rows.

---

## 12e. Action logging via Telegram buttons (P1)

### Problem

| Metric | Value |
|---|---|
| Signals with `acted` set, ever | 0 |
| Delivered, unreviewed, older than 7 days | 336 (oldest 2026-05-19) |
| `wash_sale` rows | 0 |
| Advisor last run | 2026-06-02 |

The digest footer reminder ("run `log-action <prefix>`") requires opening a terminal on the Windows box. It has been ignored for five months.

### Design

**e1. Per-alert button messages** (`delivery/telegram_sender.py`)

- New `send_alert_with_buttons(signal) -> int | None` returns the Telegram `message_id`.
- Sent after the digest send and `confirm_delivered_signals()`, one message per delivered signal (max 2 AR + 3 INFO per day):

  ```
  NVDA · news_classifier · INFORMATIONAL · HELD
  ai_semi: Nvidia GTC Berlin Keynote: ...
  ```
  `inline_keyboard`: `[[ "✅ Acted", "⏭ Passed" ]]`
- `callback_data`: `a:1:<alert_id[:24]>` / `a:0:<alert_id[:24]>` (well under Telegram's 64-byte limit; 24 hex chars is collision-safe at this volume and is resolved against DELIVERED rows only).
- A failed button send does **not** fail the job (the alert itself was already delivered in the digest). It writes a `telegram_messages` row with `status='send_failed'`; the watchdog reports any such rows from the last 24h.

**e2. Schema**

```sql
CREATE TABLE IF NOT EXISTS telegram_messages (
    chat_id     TEXT NOT NULL,
    message_id  INTEGER,
    alert_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'alert' | 'review_prompt'
    status      TEXT NOT NULL,          -- 'sent' | 'send_failed' | 'answered'
    sent_at     TEXT NOT NULL,
    answered_at TEXT,
    PRIMARY KEY (chat_id, alert_id, kind, sent_at)
);
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);   -- holds 'telegram_update_offset'
```

**e3. New job `telegram-poll`** (`jobs/telegram_poll.py`, registered in `__main__.JOBS`)

- Schedule: every 15 minutes, 07:00 to 23:00 ET, daily (Task Scheduler XML in `ops/tasks/telegram-poll.xml`, `MultipleInstancesPolicy=IgnoreNew`). Telegram keeps undelivered updates for 24 hours, so this cadence leaves wide margin.
- Wrapped in `heartbeat(job="telegram-poll")` with its own `HEALTHCHECKS_UUID_TELEGRAM_POLL`; writes a `runs` row.
- Calls `getUpdates(offset=<app_state>, timeout=0, allowed_updates=["callback_query","message"])`.
- **Authorization (mandatory):** process an update only if the chat id **and** the sender id equal `TELEGRAM_CHAT_ID` (private chat). Anything else is ignored and logged at WARNING with the sender id only.
- Callback handling: resolve the alert-id prefix to exactly one DELIVERED row (zero or multiple matches → answer "Not found" and log), call `repository.update_signal_acted(alert_id, acted=...)`, `answerCallbackQuery` with "Logged: Acted" / "Logged: Passed", then `editMessageReplyMarkup` to a single button showing the current state and offering the opposite (`↩ Change to Passed`). Re-clicks are idempotent overwrites.
- Reply-to-add-note: a text message whose `reply_to_message.message_id` matches a `telegram_messages` row sets `user_note` (prefixed `tg: `, capped at 500 chars, control characters stripped). No other free-text commands are parsed.
- Offset is persisted after each processed update (at-least-once; all handlers are idempotent).
- Preflight addition: `getWebhookInfo` must report an empty `url` (a webhook makes `getUpdates` return 409).

**e4. Weekly re-prompt** (replaces the daily footer)

- Fridays, in the dispatch job after its digest: `Unreviewed this week: N`, then re-send button messages (`kind='review_prompt'`) for up to 10 DELIVERED, `acted IS NULL` signals from the past 7 days, newest first.
- Remove the per-run `REMINDER: N delivered signal(s) ... run log-action` footer from `news-morning`.

**e5. Backlog**

- Implement Plan 10 §10d.1: `log-action --bulk-close-before 2026-10-17 --note "bulk-closed: unreviewed backlog, 2026-10-24"`. `acted` stays NULL; `review-report` excludes this note from forward-looking stats.

**e6. Trades and wash-sale**

- Buttons log decisions, not fills. Trade-level data stays in Schwab. Execute **Plans 04 + 05 (Option A)** in the same phase: remove the advisor, `log-trade`, and the ledger functions; leave the empty `wash_sale` table in place, marked deprecated. Update `CLAUDE.md` sections that still describe the ledger and advisor as active.

### Tests

- Unauthorized chat or sender id → no DB write.
- Callback for a non-DELIVERED or ambiguous prefix → "Not found", no write.
- Acted then Passed → final `acted=0`, `acted_at` updated.
- Reply-to note sets `user_note` with prefix and length cap.
- Offset advances; replaying the same update batch twice yields the same DB state.
- `callback_data` length ≤ 64 bytes for every agent's alert ids.
- Button-send failure → job success, `send_failed` row, watchdog flags it.
- HTTP layer mocked (`urllib.request`), consistent with the existing sender; no new dependency.

### Acceptance criteria

- [ ] Every delivered alert has a `telegram_messages` row (`sent` or `send_failed`).
- [ ] A button tap updates `signals.acted` within one poll interval (≤15 min, 07:00 to 23:00 ET).
- [ ] The daily reminder footer is gone; the Friday re-prompt appears.
- [ ] Backlog bulk-closed; `review-report` reports a non-empty forward sample within 2 weeks.

---

## 12f. Routing regime stamping and housekeeping (P2, implement first)

### Problem

`persist_routed_signals()` stamps `routing_regime`, but MONITORING inserts do not (`_persist_monitoring_signal`, volume-cap overflow, reconciled losers, sentinel cooldown). This week: 1,208 news MONITORING + 58 sentinel cooldown rows = 1,266 rows with `routing_regime IS NULL`, indistinguishable from legacy rows. Separately, `outcome-backfill` runs every weekday at 17:30 (5 successful runs this week) but `watchdog.EXPECTED_MAX_AGE_HOURS` still maps it to `None`.

### Design

- Move `ROUTING_REGIME` to `models.py` (stdlib-only, importable by `repository.py` without a cycle); `router` re-exports it.
- Bump to `ROUTING_REGIME = "deferred-info-priority-v2"` on the same release as 12a/12c/12d.
- `repository.insert_signal(..., routing_regime=...)` default changes from `None` to `ROUTING_REGIME`. Legacy NULL rows are **not** backfilled (existing convention).
- Watchdog: `outcome-backfill: 30` and add it to `WEEKDAY_JOBS`; add `telegram-poll: 26`.
- `CLAUDE.md`: document v2 semantics (lanes, reserved slot, macro cap, shadow discovery, repeat reason) in the Alert Router paragraph and the monitoring-reason taxonomy.

### Tests

- After a simulated news-morning + sentinel + dispatch day, `SELECT count(*) FROM signals WHERE routing_regime IS NULL AND timestamp >= <test start>` = 0.
- Watchdog flags a stale `outcome-backfill` on a weekday and not on a weekend.

### Acceptance criteria

- [ ] Zero NULL `routing_regime` rows written after rollout.
- [ ] Watchdog monitors `outcome-backfill` and `telegram-poll`.

---

## 4. Rollout

| Order | Section | Effort | Notes |
|-------|---------|--------|-------|
| 1 | 12f | 0.25 d | Regime constant + stamping first, so every later change is measured under v2. |
| 2 | 12a | 1.25 d | P0. Ship alone if needed; it is the only core-value violation. |
| 3 | 12c | 0.75 d | Router-level; pairs naturally with 12a's dispatch changes. |
| 4 | 12d | 0.25 d | Small; reduces dispatch noise before 12b raises volume. |
| 5 | 12b | 1 d | Watch the first week's `llm_calls` against the §12b baseline. |
| 6 | 12e + Plans 04/05 | 1.5 d | Needs a new Task Scheduler XML and `HEALTHCHECKS_UUID_TELEGRAM_POLL` in `.env`. |

Release steps:

1. `uv run pytest` green.
2. `python -m signal_system backup` before deploying (schema changes).
3. Deploy, then run `preflight`.
4. Import `ops/tasks/telegram-poll.xml`; set the new health-check UUID.
5. Run `python -m signal_system telegram-poll` once manually; tap a button on the next delivered alert and confirm `acted` is set.
6. After one full week, re-run this review's queries (section 1) and compare.

## 5. Definition of done

1. Sentinel: ≥1 sentinel delivery on every weekday where a fresh trigger fired; every suppressed sentinel candidate is named in the dispatch digest.
2. Coverage: every scanned held ticker with news gets ≥1 headline classified; `volume_cap` share below 50% on non-Monday days.
3. Quality: no tickerless AR delivered; no repeat story delivered within 7 days.
4. Honesty: Discovery shows as shadow, not as a loser; no NULL routing regime on new rows.
5. Feedback: `acted` filled on ≥80% of delivered alerts within 7 days, measured over the first two full weeks.

## 6. Risks

| Risk | Mitigation |
|------|------------|
| Cooldown fix (a3) floods sentinel candidates for a week | Bounded by the lane + reserved slot (≤2 sentinel deliveries/day); all others are named in the digest, not silently dropped. |
| More daily alerts (up to 2 AR + 3 INFO, plus up to 5 button messages) | Button messages replace the reminder footer; total remains small. Revisit `BUDGET_INFO` after two weeks. |
| Classifier spend rises ~1.6x (12b) | Ceiling 120 and window clamp 3 bound it; acceptance requires the week to stay within 2x baseline. |
| Repeat filter suppresses a genuinely new development with similar wording | Threshold validated at 0 false positives on 58 deliveries; repeats are stored as MONITORING with the `[repeat]` prefix, so they are auditable and the threshold can be tuned. |
| Telegram bot token compromise allows spoofed taps | Chat id and sender id both checked; the only possible write is `acted`/`user_note` on an existing delivered row. Token stays in `.env` / Credential Manager and is redacted by `redaction.py`. |
| Retiring wash-sale ledger (Plan 05) | Schwab already computes wash sales per account; cross-account (IRA) cases remain a manual check, same as today in practice since the ledger has 0 rows. |
