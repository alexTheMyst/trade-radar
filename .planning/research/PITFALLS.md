# Pitfalls Research — Investment Signal System

**Note:** Pitfalls already documented in CLAUDE.md (prompt injection, K-1 exclusions, tzdata, wash-sale account column, Finnhub free-tier gaps) are not repeated here. This covers additional domain-specific pitfalls.

---

## Critical Pitfalls

### Pitfall 1: LLM Classification Non-Determinism

**What goes wrong:** Same headline returns different severity or pillar assignment on different runs. At default temperature (>0), Claude is non-deterministic. Using a model alias like `claude-sonnet-latest` instead of a pinned version ID means a mid-quarter model update silently changes classifier behavior — old signals become incomparable to new ones.

**Consequences:** Signal quality measurement becomes meaningless. The 7-day operator annotation cycle compares apples to oranges. Pillar-delta scoring loses calibration.

**Prevention:**
- Pass `temperature=0` to every Claude API call in the classifier
- Pin exact model ID string (e.g., `claude-sonnet-4-6`) in `config.py`, never a floating alias
- Log `model_version` and `thesis_version_hash` on every row in the `signals` table

**Warning signs:** Same headline shows different severity in audit log across runs. No `model_version` column in `signals` table. Weekly alert counts vary highly with no corresponding news-volume change.

**Phase:** News Classifier agent implementation.

---

### Pitfall 2: Structured Output / JSON Parse Failures

**What goes wrong:** Claude returns prose, includes text before/after JSON, or refuses a headline it deems financial advice. A bare `json.loads` with a silent `except` turns a parse failure into a missing alert — while heartbeat pings success.

**Consequences:** Thesis-relevant events silently dropped. The heartbeat reports green while material signals are lost — the worst failure mode for this system.

**Prevention:**
- Use Anthropic's tool-use API (`tools=` with defined schema) rather than asking Claude to "return JSON" — tool-call responses are schema-validated before delivery
- If falling back to text parsing: validate with `jsonschema`, retry once with a correction prompt, then on second failure INSERT a MONITORING-severity row with `raw_response` captured
- Unit test with edge-case headlines (very short, all-caps tickers, headlines with XML-like strings) asserting 100% parse success

**Warning signs:** Gaps in `signals` table for `agent='news_classifier'` on known high-news days. No `parse_error` / `raw_response` column in schema. Classifier exceptions caught at job level, masking parse errors.

**Phase:** News Classifier agent implementation.

---

### Pitfall 3: LLM API Cost Runaway

**What goes wrong:** No per-job token budget. On a high-news day, the news-morning job processes 5–10x normal headline volume. The thesis.yaml system prompt is large and re-sent per headline without caching.

**Prevention:**
- Use Anthropic prompt caching for the thesis.yaml system prompt prefix (`cache_control: {type: "ephemeral"}`)
- Log `input_tokens` and `output_tokens` from every API response into the `runs` table
- Hard headline cap per job run (e.g., 50 max); excess headlines become MONITORING rows with "volume cap reached" note
- Daily dollar ceiling check at job start — if yesterday's cost > threshold, heartbeat /fail and abort

**Warning signs:** No token counts in any log or DB row. thesis.yaml system prompt assembled fresh inside a per-headline loop. Weekly API invoice grows >20% with no corresponding feature addition.

**Phase:** News Classifier agent implementation.

---

### Pitfall 4: Universe Rotation State Corruption

**What goes wrong:** Storing the partition cursor in SQLite ("last ticker scanned was X"). A missed Task Scheduler run, mid-scan crash, or operator rerun corrupts rotation — the same third gets scanned twice, another third goes unscanned for 6 days instead of 3.

**Consequences:** Core coverage gaps. The system silently skips tickers for days.

**Prevention:**
- Deterministic, stateless partition: `hashlib.md5(ticker.encode()).digest()[0] % 3 == day_of_year % 3`
- **Never use `hash(ticker) % 3`** — Python's built-in `hash()` randomizes per process; `hashlib.md5` is deterministic across days and restarts
- Core holdings always bypass the partition — scanned every day regardless
- Log which tickers were in scope for each run to the `runs` table

**Warning signs:** `last_scanned_ticker` or `scan_cursor` column exists in DB. Replaying on the same day scans different tickers. Core holdings appear in the rotation pool.

**Phase:** Discovery Agent implementation.

---

### Pitfall 5: Duplicate Alert Firing on Rerun / Retry

**What goes wrong:** Task Scheduler retries a failed job, or operator manually reruns it. Same ticker, same day, same alert fires twice. Daily budget counters double-decrement.

**Prevention:**
- `alert_id = sha256(ticker + date + rule_id + agent)` as a UNIQUE column in `signals` table
- `INSERT OR IGNORE` for all signal writes — silently deduplicates
- Alert router reads budget from DB count of today's non-MONITORING signals, not in-memory counter — survives restarts
- Task Scheduler: "If task is already running, do not start a new instance"

**Warning signs:** No UNIQUE constraint on `alert_id`. Operator receives duplicate emails. Budget appears exhausted on light news days.

**Phase:** Alert Router implementation.

---

### Pitfall 6: Look-Ahead Bias in Signal Outcome Backfill

**What goes wrong:** Outcome backfill fetches adjusted-close from Finnhub. Adjusted-close is retroactively adjusted for splits and dividends. A post-signal stock split makes the "30-day outcome" incomparable to the signal-time price.

**Prevention:**
- Capture `signal_price_snapshot` (unadjusted) at signal generation time
- Use consistent price type for both `signal_price_snapshot` and `outcome_price_*` — never mix adjusted and unadjusted
- If a split occurs between signal date and outcome date, flag the row for manual review

**Warning signs:** No `signal_price_snapshot` column in `signals` table. Outcome backfill calls Finnhub without specifying price type. Quarterly review shows extreme outlier returns.

**Phase:** Discovery Agent / signal outcome backfill job.

---

## Moderate Pitfalls

### Pitfall 7: thesis.yaml Schema Drift and Runtime Mutation

**What goes wrong:** Operator edits `thesis.yaml` while a job is running. YAML syntax error causes classifier to crash after heartbeat /start but before /fail. Renamed pillars corrupt historical signal comparisons.

**Prevention:**
- Load `thesis.yaml` exactly once at job start — never re-read mid-run
- Validate against Pydantic model on load; hard-fail (heartbeat /fail) on schema error
- `review_due` gate must be a hard abort with heartbeat /fail, not a warning log
- Store `thesis_version_hash` (SHA256 of file contents) on every signal row

**Warning signs:** `thesis.yaml` read inside a per-headline loop. `review_due` check produces only a log warning. No `thesis_version_hash` column in `signals` table.

**Phase:** News Classifier agent implementation.

---

### Pitfall 8: Windows Task Scheduler Silent Failure Modes

**What goes wrong:** The job never runs, but healthchecks.io gets no ping at all (worse than /fail — the process never started). Failure modes:
- "Run only when user is logged on" default — silently skips if operator not logged in
- `uv` / Python not on SYSTEM PATH — exits silently with code 2
- DST schedule shift — trigger stored as wall-clock local time, Windows adjusts it
- Missed-run policy not set — machine was off, job silently skipped for the day
- No single-instance enforcement — overlapping runs cause double-scanning

**Prevention:**
- "Run whether user is logged on or not" + stored credentials for all tasks
- "Run task as soon as possible after a scheduled start is missed" policy
- Absolute paths in all Task Scheduler command fields
- Trigger timezone set explicitly to "Eastern Time"
- "If task is already running, do not start a new instance"
- Produce a tested `.xml` task export as reference configuration (not just prose)

**Warning signs:** healthchecks.io shows no ping at all on a given day. Task history shows result code 0x1. Job runs at wrong time after DST change.

**Phase:** Windows Task Scheduler integration docs.

---

### Pitfall 9: Email as Primary Reliability Signal

**What goes wrong:** Gmail progressively classifies identical daily "0 alerts today" digests as promotional/spam. Operator stops receiving emails.

**Prevention:**
- healthchecks.io is the canonical "did the job run" signal — configure SMS/push on missed ping, not email
- Include date, ticker count, and alert count in digest subject lines to reduce spam-pattern matching
- Document a Gmail filter (`from:GMAIL_USERNAME`) → never send to spam, in the setup guide

**Phase:** Operational setup docs.

---

### Pitfall 10: Survivorship Bias in Signal Measurement

**What goes wrong:** Operator only annotates `acted` on signals they remember vividly. Delisted/acquired tickers stop appearing and are never revisited.

**Prevention:**
- At quarterly review, query `signals` for all rows where `acted IS NULL` and age > 7 days — prompt annotation or "expired" mark
- `ticker_delisted_at` column in universe table; backfill outcome prices as of delisting date
- Measure per signal type, never aggregate (IC is different between Discovery and News Classifier)

**Phase:** Signal outcome backfill / quarterly review tooling.

---

### Pitfall 11: SQLite Operational Decay

**What goes wrong:** WAL mode enabled (correctly) but no periodic checkpoint — WAL file grows unboundedly. Overlapping jobs cause busy-timeout errors.

**Prevention:**
- `PRAGMA wal_checkpoint(TRUNCATE)` at end of each job, inside `repository.py`
- Weekly `VACUUM` as a separate lightweight Task Scheduler entry
- Weekly backup: `sqlite3 state/signals.db .dump > backups/signals_YYYYMMDD.sql`, rolling 4 weeks
- `PRAGMA busy_timeout = 30000` on every connection open in `repository.py`

**Warning signs:** `state/signals.db-wal` grows steadily. No backup files. Job exits with "database is locked."

**Phase:** Operational hardening.

---

### Pitfall 12: Alert Router Budget Reset and Edge Cases

**What goes wrong:** Budget resets at "midnight" but uses UTC or Windows local time instead of `America/New_York`. Score ties are non-deterministic on rerun. Demotion cascade silently discarded.

**Prevention:**
- Budget reset boundary uses `America/New_York` midnight explicitly
- Tiebreaker: deterministic fallback (ticker alphabetically) when scores are equal
- Every demotion writes `demoted_from` field on the MONITORING row — audit trail
- Digest caps MONITORING section at top-5 by score with "and N more suppressed" footer

**Warning signs:** Budget reset uses `datetime.utcnow()` or timezone-naive `datetime.now()`. No test for dual ACTION_REQUIRED on same day. MONITORING rows have no `demoted_from` field.

**Phase:** Alert Router implementation.

---

## Minor Pitfalls

### Pitfall 13: Finnhub Mock Mismatch in Tests

**What goes wrong:** Unit tests mock Finnhub using hand-written dicts assumed from docs. When the real free-tier endpoint returns a different schema or a 403 for a paid endpoint, mocks pass but production fails silently.

**Prevention:**
- `tests/fixtures/finnhub_responses/` directory with real captured responses from actual free-tier API calls
- `tests/integration/test_finnhub_live.py` (skipped by default, run manually) that exercises each endpoint and asserts response shape

**Phase:** Discovery Agent implementation (when adding new Finnhub endpoints).

---

### Pitfall 14: Calibration Overconfidence in Discovery Phase A

**What goes wrong:** The 2-week Phase A logs-only window coincides with an abnormally quiet or volatile period. Weights calibrated during earnings season over-fire in normal markets.

**Prevention:**
- Log market condition metadata during Phase A: VIX proxy, average daily news volume, number of tickers crossing each threshold
- Plan a second calibration pass at end of first full quarter
- Make "35/30/25/10 are initial guesses" explicit in Phase A output — operator should not anchor on first calibration

**Phase:** Discovery Agent Phase A / calibration.

---

## Phase-Specific Warning Summary

| Phase | Pitfall | Mitigation |
|-------|---------|------------|
| News Classifier | Non-deterministic classification (#1) | temperature=0, pin model ID string |
| News Classifier | Structured output parse failure (#2) | Tool-use API, MONITORING row on parse error |
| News Classifier | Cost runaway (#3) | Prompt caching, headline cap, token logging |
| News Classifier | thesis.yaml schema drift (#7) | Load once, Pydantic validation, hard-fail on review_due |
| Discovery Agent | Universe rotation corruption (#4) | hashlib.md5 deterministic partition, no stored cursor |
| Discovery Agent | Look-ahead bias in outcomes (#6) | signal_price_snapshot at signal time |
| Discovery Agent | Finnhub mock mismatch (#13) | Real captured responses as fixtures |
| Discovery Agent | Calibration overconfidence (#14) | Log market conditions during Phase A |
| Alert Router | Duplicate alerts on rerun (#5) | alert_id UNIQUE + INSERT OR IGNORE |
| Alert Router | Budget reset timezone (#12) | America/New_York midnight, demotion audit trail |
| Windows TS Docs | Scheduler silent non-starts (#8) | Tested .xml export, absolute paths, missed-run policy |
| Operational | Email as reliability proxy (#9) | healthchecks.io as canonical signal, Gmail filter |
| Operational | Survivorship bias (#10) | Quarterly unannotated-signal sweep |
| Operational | SQLite WAL decay (#11) | wal_checkpoint after each job, weekly backup |
