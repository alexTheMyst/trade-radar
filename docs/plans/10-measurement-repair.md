# Plan 10 — Measurement Repair

**Type:** Fix · **Priority:** P1 · **Est. effort:** ~1.5 day

## Problem

`CLAUDE.md` commits to measuring every signal:

> System backfills `outcome_price_30d` and `outcome_price_90d` via cron. Measurement is per
> signal type (Discovery, News Classifier, Mechanical) — never aggregate.

None of that is currently true. The system has emitted 7,400 signals and can evaluate none
of them.

### Evidence

**1. Outcome backfill has never run.**

```sql
SELECT count(*) FROM runs WHERE job='outcome-backfill';           -- 0
SELECT count(*) FROM signals
 WHERE routing_status='DELIVERED' AND outcome_price_30d IS NULL
   AND julianday('now') - julianday(timestamp) > 30;              -- 92
```

`jobs/outcome_backfill.py` exists and is registered in `__main__.JOBS`. There is no Task
Scheduler XML for it in `ops/tasks/`, and `watchdog.EXPECTED_MAX_AGE_HOURS` maps it to
`None` — explicitly excluded from monitoring on the grounds that it isn't scheduled. The
exclusion is self-fulfilling.

**2. The agent label is split.**

```sql
SELECT agent, count(*) FROM signals GROUP BY agent;
--  DAILY_CLOSE       44
--  discovery_agent  338
--  news_classifier 5206
--  news_morning    1880
```

`news_classifier` and `news_morning` are the same signal type under two labels — 1,880 rows
are silently excluded from any per-agent cohort. `DAILY_CLOSE` uses a different case
convention again. Since measurement is required to be per-type and never aggregated, a
split label does not merely blur the result; it halves the sample and no one is told.

**3. The review backlog is stale beyond usefulness.**

```sql
SELECT count(*) FROM signals WHERE routing_status='DELIVERED' AND acted IS NULL
  AND julianday('now') - julianday(timestamp) > 7;               -- 136 (as of 2026-08-10 midday)
SELECT min(timestamp) FROM signals WHERE routing_status='DELIVERED' AND acted IS NULL;
-- 2026-05-19T09:23:54
```

`CLAUDE.md` gives the operator 7 days to fill `acted`. The oldest unreviewed signal is ~12
weeks old. The digest reminder fires every run and has been ignored 136 times, which makes
it noise rather than a control.

**4. `weekly-synthesis` fails before it starts.**

```sql
SELECT * FROM runs WHERE job='weekly-synthesis';
-- 2026-08-10T08:00:10 -> 08:00:39, failed
SELECT count(*) FROM llm_calls WHERE job LIKE '%synth%';   -- 0
SELECT count(*) FROM synthesis_reports;                    -- 0
```

Zero `llm_calls` rows means it died **before** the Anthropic call — in `build_corpus`,
`build_system_prompt`, or client construction. Not a model problem. The 29-second runtime
suggests a retry loop or a slow corpus query rather than an immediate import error.

Meanwhile 653 MONITORING rows accumulated in 7 days. The corpus `CLAUDE.md` describes as
"the new-idea raw material" is write-only.

**5. Benchmark data is thin.**

`benchmark_prices` holds 57 rows, most recent `2026-08-07`. Excess-return measurement needs
SPY closes spanning every signal's 30- and 90-day horizon. 57 rows will not cover a signal
log reaching back to May. `backfill-benchmark` exists as a one-time bootstrap and appears
not to have been run to completion.

## Goals

1. Every delivered signal older than 30 days has an outcome price.
2. Per-agent cohorts are whole.
3. `weekly-synthesis` runs, and its output is read.
4. The review reminder is a control again, not noise.

## Non-goals

- Changing the scoring weights. `replay-weights` owns that and should run *after* this plan
  makes its inputs trustworthy.
- Adding new metrics. Fix the ones that were promised first.

---

## Design

### 10a. Schedule and monitor outcome backfill

1. Author `ops/tasks/outcome-backfill.xml`. Daily, off-peak (suggest 18:30 ET, after
   `sentinel` at 17:00 and `daily-close` at 17:15).
2. Set `HEALTHCHECKS_UUID_OUTCOME_BACKFILL`.
3. Change `watchdog.EXPECTED_MAX_AGE_HOURS["outcome-backfill"]` from `None` to `30`.
4. Run once manually to clear the 92-row arrears; confirm Finnhub free tier serves the
   historical candles required. **`CLAUDE.md` flags this as an open question** — *"Does
   `/stock/candle` support `adjusted=False` on Finnhub free tier?"* Validate empirically
   before relying on it, and if unavailable, record the fallback rather than skipping
   silently.

Same treatment for `review-report` (weekly, suggest Fri 18:00) and `weekly-synthesis`
(Mon 08:00, already running from somewhere — check in an XML so it is reproducible).

### 10b. Unify the agent label

Constants already exist (`NEWS_CLASSIFIER_AGENT`). The `news_morning` rows predate or
bypass them.

1. Audit every `Signal(agent=...)` construction site; route all through module constants.
   Add a test asserting the set of distinct `agent` values in the DB is a subset of the
   declared constants — this is the guard that stops it recurring.
2. **Do not rewrite history.** Instead add a canonical mapping used by
   `review/stats.py` and `replay/weight_replay.py`:

```python
AGENT_ALIASES = {"news_morning": "news_classifier", "DAILY_CLOSE": "daily_close"}
```

Rewriting 1,880 historical rows would be defensible, but the signal log is the audit trail
and an alias map is reversible where an `UPDATE` is not. Document the alias in
`signal-log-schema.md`.

### 10c. Diagnose `weekly-synthesis`

Reproduce with the real corpus:

```bash
uv run python -m signal_system weekly-synthesis
```

Prime suspects, in order:

1. `build_corpus(since, cap=200)` — 653 rows in the window; check the query and any
   assumption about `title` prefixes that Plan 08's `monitoring_reason` change will
   supersede anyway.
2. `_sanitize_body` / `render_corpus_item` — `CLAUDE.md` requires every title be re-
   sanitized before prompt embedding; a `None` body or a control character could raise.
3. Client construction or `config` access at 08:00 before some dependency is available.

The 29-second runtime with zero `llm_calls` is the strongest clue — capture the traceback
before theorising further. Fix, then confirm a row lands in `synthesis_reports`.

Once it runs, decide what happens to its output: a pillar candidate nobody reads is the
same failure as a corpus nobody mines.

### 10d. Make the review backlog tractable

136 items at ~12 weeks old will not be reviewed retrospectively with any fidelity, and
pretending otherwise poisons the sample.

Proposal — operator decision:

1. **Bulk-close the pre-2026-08-01 backlog** with a distinguishing `user_note` (e.g.
   `"bulk-closed: unreviewed backlog, 2026-08-10"`), and **exclude that note from all
   forward-looking statistics**. Add `--bulk-close-before <date>` to `log-action`.
2. Change the digest reminder from a count to a **hard escalation**: if unreviewed signals
   older than 14 days exceed 20, the reminder becomes its own Telegram message rather than
   a digest footer. A control that is ignored 136 times is not a control.
3. Consider whether 7 days is the right SLA. If the honest answer is that review happens at
   the quarterly review, change the documented SLA to match reality rather than leaving a
   permanently-breached one in `CLAUDE.md`.

### 10e. Complete the benchmark series

Run `python -m signal_system backfill-benchmark` to completion and verify SPY coverage spans
`min(signals.timestamp)` through today plus 90 days of forward room. Add a `preflight`
check (Plan 02) for gaps in `benchmark_prices` — a missing close silently voids excess-
return math for every signal in that window.

---

## Testing

- **Unit:** `AGENT_ALIASES` folding in `review/stats.py` — a mixed-label fixture produces
  one cohort.
- **Unit:** distinct-agent-values guard fails when a new unmapped label appears.
- **Unit:** `weekly-synthesis` corpus build over a fixture containing a `None` body, a
  control character, and a 200+ row corpus.
- **Integration:** `outcome-backfill` against a fixture of signals at 29, 30 and 31 days;
  assert only the eligible ones are filled.
- **Integration:** `review-report` produces a per-agent readout with zero NULL 30-day
  outcomes for eligible signals.

## Acceptance criteria

- [ ] Zero DELIVERED signals older than 30 days with `outcome_price_30d IS NULL`.
- [ ] `outcome-backfill`, `review-report` and `weekly-synthesis` each have a Task Scheduler
      XML, a dedicated healthcheck UUID, and a non-`None` watchdog SLA.
- [ ] Distinct `agent` values in `signals` are a subset of the declared constants, enforced
      by test; aliases documented in `signal-log-schema.md`.
- [ ] `synthesis_reports` has at least one row and the failure cause is recorded in
      `.planning/debug/`.
- [ ] `benchmark_prices` spans the full signal history with no gaps.
- [ ] Unreviewed-signal count older than 14 days is under 20, and the escalation path is
      tested.
