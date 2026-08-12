# Plan 06 — Cross-Agent Budget Arbitration

**Type:** Fix · **Priority:** P0 · **Est. effort:** ~1.5 day · **Blocks:** nothing · **Blocked by:** operator decision (§3)

## Problem

`CLAUDE.md` describes the Alert Router as a cross-agent competition:

> All signals from the three agents (Discovery, News Classifier, Exit Sentinel) funnel
> through the router before delivery. Slot competition: if two agents compete for
> `ACTION_REQUIRED`, higher-scored wins, others demote to INFORMATIONAL.

That is true *within a single `route_signals()` call*. It is false *across the day*.
`route_signals()` reads `repository.count_delivered_today()` once at the top and treats
already-delivered signals as immovable. Since the agents run in separate scheduled jobs at
separate clock times, the earliest job takes every slot it can fill and later jobs compete
only for the remainder.

The three agents never meet. There is no batch in which they compete.

### Evidence

`news-morning` runs 09:00 ET; `discovery` runs 10:30 ET; `sentinel` is scheduled for 17:00
ET and has never run at all.

```sql
SELECT date(timestamp), agent, count(*)
FROM signals WHERE routing_status='DELIVERED'
  AND timestamp >= date('now','-14 day')
GROUP BY 1,2 ORDER BY 1 DESC;
```

```
2026-08-10  news_classifier   4
2026-08-09  news_classifier   4
2026-08-07  discovery_agent   4
2026-08-06  discovery_agent   4
2026-08-05  discovery_agent   4
2026-08-04  discovery_agent   4
2026-08-03  discovery_agent   4
2026-07-31  discovery_agent   4
...
```

Never a mixed day. Always 4/4 to whichever agent ran first.

On 2026-08-10 Discovery scanned 186 tickers, signaled 68, and **all 68 were suppressed**.
The top suppressed Discovery score was **99.6**. It lost to a News Classifier signal with
confidence 0.87 → competition score 87. The 99.6 did not lose on merit; it never entered
the comparison.

### Why this is a P0 and not a nuisance

The 2026-06-08 → 2026-08-09 `news-morning` outage masked this completely. With News dead,
Discovery took 4/4 daily and looked healthy. **Repairing news-morning on 2026-08-09
silently switched Discovery off**, and every observability surface reports green:

- `runs` shows `discovery / success`.
- The watchdog (Plan 01) checks liveness, not delivery, so it will report green too.
- The healthcheck pings `/success`.
- The digest is sent by `news-morning` and never mentions Discovery.

A capability was lost as a *side effect of a bug fix*, and the system is architecturally
incapable of noticing. That is the same class of failure as the original outage, one layer
up.

### A second-order effect

`competition_score()` in `models.py` maps News confidence × 100 onto Discovery's raw 0–100
scale. That mapping has never actually been exercised in production — no batch has ever
contained both agents. Whatever calibration problems it has are undiscovered.

## Goals

1. Make the delivered set a function of **signal quality**, not job start time.
2. Make Discovery's (or any agent's) total exclusion **visible**, whatever the arbitration
   model.
3. Exercise `competition_score()` for real, and record enough state to evaluate it.

## Non-goals

- Changing the daily caps (1 AR / 3 INFO). The budget size is a separate decision.
- Changing how any agent scores its own signals. `replay-weights` owns that.
- Backfilling historical routing decisions.

---

## 3. Design options — **operator decision required**

### Option A — Per-agent floors (minimal)

Reserve one INFO slot per producing agent; the remainder stays first-come.

```python
BUDGET_AR: int = 1
BUDGET_INFO: int = 3
INFO_FLOOR_PER_AGENT: int = 1   # each agent that produces candidates gets >= 1 INFO slot
```

- **Pro:** small change, no new job, no delay to morning news, preserves timeliness.
- **Con:** still not a real competition. A 99.6 Discovery signal and a 0.61 News signal can
  both deliver. Arbitrary.
- **Effort:** ~0.5 day.

### Option B — Time-windowed budgets

Split the day into windows with independent budgets: morning (news), midday (discovery),
close (sentinel).

- **Pro:** each agent is guaranteed its own airtime; alerts stay timely; no cross-agent
  score comparison needed, which sidesteps the unvalidated `competition_score()` mapping.
- **Con:** the total daily alert count rises unless per-window budgets shrink, which
  weakens the "1 AR/day" discipline that makes an AR mean something.
- **Effort:** ~1 day.

### Option C — Deferred end-of-day dispatch (as designed)

All jobs write candidates to the DB with `routing_status='CANDIDATE'`. A new `dispatch` job
at ~17:30 ET pools the day's candidates, runs `route_signals()` once over the whole set,
and sends one digest.

- **Pro:** the only option that implements what `CLAUDE.md` actually describes. True
  cross-agent competition on a unified scale. One digest per day instead of several.
- **Con:** a 09:00 material news event on a held position is not delivered until 17:30.
  For an alert-only system whose stated core value is *"never miss a material
  thesis-relevant event"*, that is a real cost.
- **Effort:** ~1.5 day, plus a new `candidate_signals` staging concept that `CLAUDE.md`
  currently states does not exist and is not needed. That statement would need revising.

### Option D — Hybrid: immediate AR, deferred INFO ← **recommended**

`ACTION_REQUIRED` dispatches immediately on generation, as today. `INFORMATIONAL` signals
are staged as candidates and dispatched once at ~17:30 by a `dispatch` job that pools all
agents.

- **Pro:** preserves urgency exactly where urgency matters — an AR is by definition the
  thing you must not wait on. Everything below that competes fairly on merit at the end of
  the day. Collapses 2–3 digests into one evening digest plus rare AR interrupts.
- **Con:** two routing paths to reason about and test. AR budget is still first-come, but
  with a cap of 1/day and rule-determined severity, that is defensible and rare.
- **Effort:** ~1.5 day.

**Recommendation: Option D.** It is the only option that resolves the fairness problem
without trading away the timeliness the system exists to provide. Option A is worth doing
in the same week as a stopgap if D will not land immediately.

---

## 4. Implementation (assuming Option D)

### 4a. Schema

Add to `signals`:

| Column | Purpose |
|---|---|
| `routing_regime` | Version stamp, e.g. `deferred-info-v1`. NULL = pre-change first-come. Mirrors the existing `weight_version` convention so historical comparisons stay honest. |

Extend the `routing_status` vocabulary with `CANDIDATE`. Add to `repository.py`:

- `insert_candidate_signal(signal)` — writes `routing_status='CANDIDATE'`.
- `list_candidates_for_date(date)` — reads back for dispatch.
- `mark_candidates_routed(alert_ids, outcome)` — transitions CANDIDATE → DELIVERED /
  SUPPRESSED / MONITORING in one transaction.

Guard: a `CANDIDATE` row older than one day that was never dispatched is a dispatch
failure. The watchdog should flag it (see 4d).

### 4b. Router

`route_signals()` itself needs **no change** — it is already pure over a batch and a budget
read. The change is in who calls it and with what.

- Agent jobs call a new `router.stage_signals(signals)` which routes `ACTION_REQUIRED`
  immediately via the existing path and stages the rest as `CANDIDATE`.
- The new `jobs/dispatch.py` loads the day's candidates, calls `route_signals()` once over
  the pooled set, persists outcomes, and renders a single digest.

Preserve the existing guard that `MONITORING` severity never enters the router.

### 4c. Digest

The evening digest must state, explicitly, **what each agent contributed**:

```
Dispatch Digest — 4 alerts
Candidates pooled: 72 (news_classifier 4, discovery_agent 68, exit_sentinel 0)
Delivered:  2 news_classifier, 2 discovery_agent
Suppressed: 68 (outscored 68, cap_exhausted 0)
```

An agent contributing zero candidates and an agent contributing 68 that all lost must look
different on the page. This line is the fix for the invisibility problem, independent of
which arbitration option is chosen — **add it even under Option A.**

### 4d. Watchdog integration

Plan 01's watchdog checks liveness. Add a delivery-share check:

- Flag any agent that produced candidates on ≥5 consecutive scheduled days and delivered
  **zero** across all of them. That is the exact signature of the current Discovery state
  and it must be loud.
- Flag any `CANDIDATE` row older than 36 hours (dispatch died).

## 5. Testing

- **Unit:** `route_signals()` over a synthetic mixed batch — one Discovery 99.6, one News
  0.87 — asserts the 99.6 takes the AR slot. This test would fail today if the batch could
  ever occur; it cannot, which is the bug.
- **Unit:** `stage_signals()` routes AR immediately and stages INFO.
- **Unit:** dispatch over an empty candidate set still sends the zero-alert digest with the
  required `"Scanned N tickers, 0 alerts"` line.
- **Integration:** replay 2026-08-10's actual 72 candidates (4 news + 68 discovery) through
  the pooled router and diff the delivered set against what was actually sent. This is the
  acceptance artifact — attach the diff to the phase summary.
- **Regression:** `competition_score()` over a mixed batch, asserting the News ×100 mapping
  lands where intended. First real exercise of this function.

## 6. Rollout

1. Ship behind the `routing_regime` stamp so pre- and post-change signals never get
   aggregated in `review-report`.
2. Run one week with dispatch enabled and compare delivered-set composition against the
   prior week.
3. Do **not** backfill. `weight_version` set the precedent: NULL means legacy, and legacy
   stays legacy.

## 7. Acceptance criteria

- [ ] A day on which both agents produce candidates results in a delivered set ordered by
      `competition_score()`, verifiable from the DB.
- [ ] The digest names every contributing agent and its candidate/delivered counts.
- [ ] An agent shut out for 5 consecutive days produces a watchdog alert.
- [ ] The 2026-08-10 replay diff is attached to the phase summary.
- [ ] `routing_regime` is populated on every new signal; historical rows remain NULL.
