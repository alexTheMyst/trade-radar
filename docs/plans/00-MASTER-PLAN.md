# Master Plan — Re-sequenced Backlog (2026-08-10)

**Supersedes the sequencing in [README.md](README.md) as of 2026-08-09.** The five plans
from that review remain valid; this document re-prioritizes them against evidence
gathered from a live verification run on 2026-08-10 and adds five new plans (06–10).

---

## 1. Why this exists

The 2026-08-09 review concluded that the system's weakest link is **silent failure and
unused surface area, not missing capability**. A day later, a verification pass against
the live database confirmed that diagnosis and found it was worse than described: the
system is not merely failing silently, it is **reporting success while producing wrong
output**.

Three classes of problem, in order of severity:

| Class | Meaning | Example found |
|---|---|---|
| **A — Wrong output, reported as success** | The job runs green and delivers a confidently-worded alert that is factually mis-labelled | A Microchip Technology story delivered as `NVDA — HELD` |
| **B — Silent capability loss** | A working agent stops producing output and nothing reports it | Discovery delivered 0 of 68 signals on 2026-08-10; last delivery 2026-08-07 |
| **C — Unmeasurable** | Output exists but cannot be evaluated | 92 signals >30d old with `outcome_price_30d = NULL` |

Class A is the most dangerous because it actively degrades operator judgment. A dead job
teaches you nothing; a confidently mislabelled alert teaches you something false.

---

## 2. Evidence base

All figures below were read directly from `state/signals.db` and the source tree on
2026-08-10. They are reproducible; each plan restates the query that produced its numbers.

> **Note on timing.** The `discovery` job executed live at 10:30:03 ET while this review was
> in progress, adding 68 suppressed signals. Figures were re-verified after that run; the
> lifetime `discovery_agent` total is 338, not the 270 visible earlier in the day. Any
> figure that drifts with time is stated with its as-of point.

### 2.1 The daily budget is a race, not a competition

`CLAUDE.md` states cross-agent competition uses a unified 0–100 scale and "if two agents
compete for `ACTION_REQUIRED`, higher-scored wins." In practice the router reads
`count_delivered_today()` at the moment it runs, and jobs run at different clock times.
**Whichever job runs first that day takes all four slots.**

```
Delivered signals by day and agent (last 14 days)
  2026-08-10  news_classifier   4     <- news-morning runs 09:00
  2026-08-09  news_classifier   4
  2026-08-07  discovery_agent   4     <- discovery runs 10:30
  2026-08-06  discovery_agent   4
  2026-08-05  discovery_agent   4
  ... (discovery 4/4 every day back through July)
```

On 2026-08-10 Discovery scanned 186 tickers, signaled 68, and **all 68 were suppressed** —
including a score of 99.6, which lost to a News Classifier confidence of 0.87 (competition
score 87) purely because news-morning ran 90 minutes earlier.

The causal chain matters: `news-morning` was dead from 2026-06-08 to 2026-08-09. During
that outage Discovery took 4/4 slots daily. **Repairing news-morning silently switched
Discovery off.** Both jobs report `success`. Nothing in the digest, the watchdog, or the
health checks says a word about it.

This is the single highest-impact finding and it is not covered by any existing plan.

### 2.2 Ticker attribution is the query ticker, not the story's subject

`jobs/news_morning.py` buckets headlines by the ticker it *queried Finnhub with*
(`headlines_by_ticker[ticker]`), and `classifier/news_classifier.py` passes that straight
through to `Signal.ticker` (lines 437, 469). `ClassificationResult` has no ticker field —
the model is never asked what the story is about.

Delivered 2026-08-10:

| Delivered as | Story is actually about |
|---|---|
| `NVDA — INFORMATIONAL — HELD` | Microchip Technology (MCHP) — not held |

Suppressed the same run: `"Why Micron Stock Plummeted 28.7%"` → tagged NVDA;
`"Why Is TSM Stock Rising Premarket?"` → tagged GOOGL; `"Cold Jobs Report… Airbnb Steals
the Show"` → tagged AVGO; `"JPMorgan Raises S&P 500 Target"` → tagged GOOGL.

### 2.3 `HELD` is not a check

`news_morning.py` sets `held_tickers = set(tickers)` where `tickers` is
`get_core_holdings()` — the **universe**, not `holdings.csv`. Consequences:

- Every company-news alert is `HELD` by construction, so the tag carries zero information.
- `NEM`, `RTX` and `SMH` are core-scanned but absent from `holdings.csv`. They would print
  `HELD` falsely.

### 2.4 Digest counts do not reconcile to the database

Digest reported `Monitoring: 389 (off-thesis 17, volume-cap 339, reconciled 5)`.

| | Digest | Database |
|---|---|---|
| MONITORING total | 389 | **385** |
| off_thesis | 17 | **15** |
| volume_cap | 339 | 339 |
| reconciled | 5 | 5 |
| below-threshold on-thesis | *(no bucket)* | **26** |

Two independent defects:

1. `_persist_monitoring_signal()` discards the `bool` return of
   `repository.insert_signal()`, which uses `INSERT OR IGNORE`. `alert_id` collisions are
   counted in `monitoring_total` but never stored — 4 rows lost on 2026-08-10, 2 of them
   `off_thesis`.
2. `monitoring_breakdown` is initialised with only `off_thesis` and `parse_failure`, then
   gets `volume_cap` and `reconciled`. There is **no bucket for low-confidence MONITORING
   signals**, so the breakdown structurally cannot sum to the total.

`validate_digest_payload()` cannot catch either, because it compares the digest against
`status_counts` — the same in-memory counters that produced the digest. It never reads the
database back. The "fail closed" guard is self-referential.

### 2.5 The volume cap discards most of the news, unevenly

The cap is `newest 50` across **all company news for all 16 core tickers combined**, plus
`newest 20` general, with dedup sorted by recency descending and applied globally. A noisy
ticker crowds out a quiet one entirely.

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

NVDA consumed 30 of the 50 company slots. `BTG`, `LMT`, `NOC`, `SMH` got one each. In
total **339 of ~389 headlines (87%) were never classified** — they were written to
MONITORING unread.

Two compounding causes: the cap is global rather than per-ticker, and the window on a
Monday is Friday 16:00 → Monday 09:00 (`previous_close` = last successful `daily-close`),
roughly 3× a normal overnight window against an unchanged cap.

### 2.6 Jobs that have never run

`runs` table, lifetime:

| Job | Runs | Task Scheduler XML in `ops/tasks/` |
|---|---|---|
| `sentinel` | **0** | **missing** |
| `watchdog` | **0** | present, evidently not imported |
| `outcome-backfill` | **0** | **missing** |
| `review-report` | **0** | **missing** |
| `weekly-synthesis` | 1, **failed** (2026-08-10 08:00) | missing |
| `backup` | 1, success (2026-08-09) | missing |

`jobs/sentinel.py` and `jobs/watchdog.py` are both implemented and registered in
`__main__.JOBS`. The Exit Sentinel — the only agent that watches your actual positions for
mechanical exit triggers — **has never emitted a signal.** The watchdog, whose entire
purpose is to catch exactly this, has never run either.

`weekly-synthesis` failed 08:00:10 → 08:00:39 with **zero rows in `llm_calls`**, so it died
before reaching the Anthropic call — the fault is in corpus build, prompt assembly, or
client construction, not the model. `synthesis_reports` is empty; the 653-item MONITORING
corpus is being written and never read.

### 2.7 Measurement is not possible today

- **92** DELIVERED signals older than 30 days have `outcome_price_30d = NULL`.
- **136** DELIVERED signals are unreviewed, oldest 2026-05-19 (~12 weeks). The digest
  reported 132 at 09:00; four more crossed the 7-day line during the day.
- The `agent` column carries both `news_classifier` (5206 rows) and `news_morning` (1880
  rows) for the same signal type, plus `DAILY_CLOSE` (44) in a different case convention.
  `CLAUDE.md` requires measurement per signal type and never aggregated; a split label
  silently halves each cohort.

---

## 3. Re-sequenced backlog

| # | Plan | Type | Priority | Effort | Status |
|---|------|------|----------|--------|--------|
| **06** | [Cross-agent budget arbitration](06-cross-agent-budget.md) | Fix | **P0** | ~1.5 d | New |
| **01** | [Meta-monitor job (`watchdog`)](01-meta-monitor.md) | Add | **P0** | ~0.5 d | **Code done, never scheduled** |
| **08** | [Digest integrity](08-digest-integrity.md) | Fix | **P0** | ~1 d | New |
| **07** | [News subject-ticker attribution](07-news-attribution.md) | Fix | **P0** | ~2 d | New |
| **02** | [Preflight / doctor command](02-preflight-doctor.md) | Add | P1 | ~1 d | Unchanged |
| **03** | [Holdings ↔ universe reconciliation](03-holdings-universe-reconciliation.md) | Add | P1 | ~1 d | Unchanged |
| **09** | [News coverage & volume cap](09-news-coverage-volume-cap.md) | Fix | P1 | ~1 d | New |
| **10** | [Measurement repair](10-measurement-repair.md) | Fix | P1 | ~1.5 d | New |
| **11** | weekly-synthesis defect *(folded into 10)* | Fix | P2 | ~0.5 d | New |
| **04** | [Remove the Advisor agent](04-remove-advisor.md) | Remove | P2 | ~0.5 d | Unchanged |
| **05** | [Retire the wash-sale ledger](05-retire-wash-sale-ledger.md) | Remove | P2 | ~0.5 d | Unchanged |

**Total: ~11 days of work.** Phases 0–1 (~3.5 days) close every Class-A defect.

### What changed from the 2026-08-09 ordering, and why

- **01 is no longer "first, always."** It is still P0, but it is now *code that exists and
  has never executed*. Its remaining work is operational (import the XML, set per-job
  UUIDs), not development. Do that inside Phase 0 rather than treating it as a build task.
- **06 is new and outranks everything.** Plan 01 detects a job that *fails*. Nothing
  detects a job that *succeeds and delivers nothing because another job took its budget*.
  That is the state Discovery has been in since 2026-08-09, and the watchdog will report it
  green.
- **07 and 08 are new P0s** because they are Class A — they make the digest actively
  misleading, which is worse than the digest being absent.
- **03 was P1 for coverage; it stays P1**, but note its scope now overlaps 07 (both touch
  the holdings/universe split). Build the holdings loader once, in 03, and have 07 consume
  it.
- **04 and 05 are unchanged at P2.** Removing carrying cost is still right, but not before
  the system tells the truth.

---

## 4. Phasing

### Phase 0 — Restore visibility (~1 day, no code changes to agents)

Nothing else is trustworthy until the system reports its own state honestly.

1. Import `ops/tasks/watchdog.xml` into Windows Task Scheduler; confirm a `runs` row
   appears.
2. Author and import missing Task Scheduler XMLs: `sentinel`, `outcome-backfill`,
   `weekly-synthesis`, `backup`, `review-report`. Follow `ops/windows-task-scheduler.md`.
3. Set every per-job `HEALTHCHECKS_UUID_<JOB>` in `.env`. Any job still falling back to the
   shared UUID re-creates the masking failure documented in Plan 01.
4. Run `python -m signal_system sentinel` manually once and confirm it emits.
5. Update `watchdog.EXPECTED_MAX_AGE_HOURS` — `outcome-backfill` and `review-report` are
   currently `None` (skipped); set real SLAs once scheduled.

**Exit criteria:** every job in `__main__.JOBS` either has a `runs` row within its SLA or is
explicitly and deliberately mapped to `None` in the watchdog.

### Phase 1 — Stop lying (~3 days)

6. **Plan 06** — cross-agent budget arbitration.
7. **Plan 08** — digest counts reconcile to the DB; `validate_digest_payload` reads back.
8. **Plan 07** — subject-ticker attribution and a real `HELD` check.

**Exit criteria:** every field in a delivered alert is either verifiably correct or absent.
No alert asserts a ticker or a holding status it has not checked.

### Phase 2 — Close coverage gaps (~2 days)

9. **Plan 03** — holdings ↔ universe reconciliation (19 held tickers currently unscanned).
10. **Plan 09** — per-ticker volume cap and window-aware sizing.

**Exit criteria:** for every position in `holdings.csv`, a material headline published in
the window reaches the classifier.

### Phase 3 — Make it measurable (~2 days)

11. **Plan 10** — outcome backfill scheduled, agent labels unified, `weekly-synthesis`
    defect fixed, review backlog triaged.

**Exit criteria:** `review-report` produces a per-agent readout with no NULL outcome
columns for signals older than 30 days.

### Phase 4 — Reduce surface area (~1 day)

12. **Plan 02** — preflight / doctor.
13. **Plans 04 + 05** — remove Advisor, retire wash-sale ledger (coupled; 04 first).

---

## 5. Cross-cutting conventions

Inherited from `README.md` and `CLAUDE.md`; every plan honors these.

- Python 3.12+, `uv`, stdlib `sqlite3` (WAL). No new dependencies unless a plan states one
  and justifies it against the rejected-dependency table in `CLAUDE.md`.
- All DB access through `state/repository.py`. No raw SQL elsewhere.
- Scheduled jobs wrap in `heartbeat.heartbeat(job=...)` and write a `runs` row via
  `repository.insert_run()` / `update_run()`.
- Every job that can fail silently must fail **loud** — Telegram and/or its own health check.
- Timezone is always `zoneinfo.ZoneInfo("America/New_York")`.
- Headlines embedded in prompts are stripped of control characters, capped at ~500 chars,
  and wrapped in `<headline>...</headline>`.
- **Any change to classifier prompts or output schema requires `eval-classifier` against
  the golden set before rollout**, per `CLAUDE.md`. Plan 07 is the only plan that trips this.
- **Execution must be entered through a GSD command** (`/gsd-quick`, `/gsd-debug`, or
  `/gsd-execute-phase`) so planning artifacts stay in sync. These plans are documentation;
  they are not authorization to edit source directly.

---

## 6. Definition of done for the whole cycle

A run is correct when all of the following hold:

1. **Attribution** — no delivered alert names a ticker the story is not about, and `HELD`
   is true only for tickers in `holdings.csv`.
2. **Reconciliation** — every count in the digest is read back from the database after
   persistence, and the monitoring breakdown sums to the monitoring total.
3. **Budget fairness** — on a day when both News and Discovery produce candidates, the
   delivered set reflects score, not clock order. Verifiable by replaying a day's
   candidates through the router.
4. **Liveness** — the watchdog has a `runs` row within SLA for every scheduled job, and a
   deliberately induced failure of any one job produces a Telegram message within one
   watchdog cycle.
5. **Coverage** — `preflight` reports zero held tickers absent from the news scan set.
6. **Measurement** — `review-report` runs per agent with no NULL 30-day outcomes older
   than 30 days.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Plan 07 changes the classifier output schema | Golden-set regression; classification quality drift | Run `eval-classifier` before and after; do not merge on a worse score. Pin `model_version` and record it in `signals`. |
| Plan 06 changes routing semantics | Historical signals become non-comparable across the change | Stamp a routing-regime version on `signals` the way `weight_version` already stamps Discovery scoring. Do not backfill. |
| Scheduling five new jobs at once | A burst of new Telegram traffic; possible Finnhub 60/min pressure | Stagger start times in the XMLs. `sentinel` at 17:00, `backup` Sun 08:00, `weekly-synthesis` Mon 08:00, `outcome-backfill` off-peak. |
| Raising the volume cap (Plan 09) | Higher Anthropic spend; more tokens per run | Today's run cost 70 calls / 7,030 output tokens with 171,948 cached reads. Model the new cap against that baseline before enabling; prompt caching already absorbs the thesis. |
| Fixing coverage (Plan 03) adds 19 tickers | Finnhub call volume roughly doubles | Existing token-bucket paces sequentially; measure wall time, not rate-limit errors. |
| Working the 136-signal review backlog | Hindsight bias contaminates the sample | Mark backfilled reviews with a distinguishing `user_note` so they can be excluded from forward-looking stats. |

---

## 8. Open questions for the operator

These are decisions this plan cannot make. Each blocks the plan noted.

1. **Budget arbitration model (blocks 06).** Per-agent floors, time-windowed budgets, or
   deferred end-of-day dispatch? Deferred dispatch is the only option that delivers true
   cross-agent competition as `CLAUDE.md` describes, but it delays morning news to the
   evening. Recommendation and trade-offs in Plan 06 §3.
2. **Is Discovery still wanted at its current volume (blocks 06)?** It signaled 68 of 186
   tickers scanned on 2026-08-10 — a 37% signal rate, up from ~33% on a 24-ticker universe.
   If the universe grew via `build-universe`, the score gates may need re-tuning before
   Discovery competes for slots at all. `replay-weights` exists for exactly this.
3. **Should `NEM`, `RTX`, `SMH` be held, or removed from core (blocks 03)?** They are
   scanned as core but absent from `holdings.csv`. Either the CSV is stale or the universe
   is.
4. **Retention policy for MONITORING (informs 09).** 653 rows in 7 days at the current cap.
   Raising the cap raises that. `weekly-synthesis` caps its corpus read at 200; the rest is
   write-only storage.
