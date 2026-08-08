# Profit Loop + New-Idea Discovery — Design Spec

**Date:** 2026-08-07
**Scope:** Four independent workstreams that close the measurement loop on profit and build a new-idea discovery engine. Restores a production-down bug, makes profit measurable/tunable, allocates the scarce daily ACTION_REQUIRED slot by merit, and stops losing paid-for off-thesis LLM rationale.

---

## Workstream 1 — Restore production (Phase 0)

`src/signal_system/data/thesis_loader.py:13` imports only `datetime`, but `Thesis.review_due: date` is annotated at line 53. Pydantic v2 fails to resolve the `date` forward ref, raising `PydanticUserError` on every `load_thesis()` call — killing `news-morning` and `advisor`. Three thesis tests in `test_smoke.py` fail in isolation.

**Design decision:** the fix is a one-line import change (`from datetime import date, datetime`). No schema change. Stale docs corrections (CLAUDE.md, ops) ship separately with zero code risk.

**Verified nuance:** the full suite is green because `test_news_classifier.py` constructs `Thesis` before `test_smoke.py` runs, caching the schema and masking the bug. Production always imports fresh, so the bug is real and live.

---

## Workstream 2 — Stop losing new-idea raw material (Phase 1)

The News Classifier pays for a per-headline LLM call that produces `pillar_name=None` (off-thesis), a `confidence`, `direction`, and `rationale` — then throws all of it away (`news_classifier.py:422-423`). Off-thesis headlines are exactly where new pillar ideas come from.

**Design decision:** classify off-thesis results as `MONITORING` signals with a `[off_thesis]` title prefix, same `alert_id` (so on-thesis and off-thesis are idempotent across a rerun), `body=rationale`, `score=confidence`, `direction` preserved, `pillar=None`. The job layer already persists `MONITORING` signals with `routing_status='MONITORING'` and skips Finnhub quote snapshots for pure-MONITORING tickers; existing readers already exclude MONITORING so no measurement pollution.

**Title-prefix taxonomy (monitoring corpus):**
- `[off_thesis]` — classifier said no pillar matched; rationale preserved
- `[parse_failure]` — messages.parse() returned no schema
- `[volume_cap]` — deduped news exceeded the newest-50 cap
- reconciled losers are persisted MONITORING with `demoted_from='reconciled'`

**Design decision:** news digest gains a one-line breakdown `Monitoring: N (off-thesis A, parse-fail B, volume-cap C, reconciled D)`, zero categories omitted. A `get_monitoring_signals_since()` repository reader exposes the corpus to future synthesis. `[` is not a SQLite LIKE wildcard, so prefix filters are safe.

---

## Workstream 3 — Close the measurement loop (Phase 2)

### Problem
Profit is unmeasurable, therefore untunable:
- `acted`/`acted_at` columns have no write path
- no SPY benchmark series (daily-close writes one SPY close as a fake signal row, and nothing reads it)
- no `weight_version` column — signals from two scoring regimes (pre-2026-05-31 single-day, post unstamped 50/30/20) are indistinguishable
- outcome backfill is built but unscheduled
- `llm_calls` is written but never read

### Design decisions

**Benchmark:** new `benchmark_prices` table (PK `(ticker, date)`). daily-close writes SPY close via `upsert_benchmark_price(source='finnhub_quote')` and retires the fake signal row — news-morning gates on the `runs` table, not the signal row, so this is safe. One-time `backfill-benchmark` command backfills SPY daily closes from `get_earliest_signal_date()` to now via yfinance (`source='yfinance_backfill'`). `get_benchmark_close_on_or_after()` mirrors the outcome-backfill convention (first trading day ≥ target).

**Acted-recording CLI:** `log-action <prefix> --acted|--passed [--note]` and `list-signals [--days N]`. Prefix = first 4+ chars of alert_id; ambiguity guard (0 → exit 2, >1 → list + exit 2). No heartbeat/runs row — ad-hoc operator commands. Unacted nag appended to news-morning digest body at send-composition time (DigestPayload is frozen) when `count_unacted_delivered_older_than(7) > 0`.

**weight_version:** `_ensure_column` + `insert_signal` + `Signal` field, stamped `"xsect-50/30/20-v2"` on every Discovery signal. NULL = legacy-mixed regime. Iteration 3.3 bumps to `"xsect-50/30/20-absgate-v3"` in the same commit as the absolute gates.

**review-report:** new pure-computation `review/report.py` + standard job `jobs/review_report.py` (runs row, heartbeat, print + Telegram). Metrics per agent per horizon (30d/90d), never aggregated:
- `raw_ret = outcome/snapshot - 1`; `excess = raw - spy_ret`; missing-benchmark rows excluded and counted
- news direction: `signed_excess = -excess` if negative; neutral excluded from hit-rate/IC, counted
- hit-rate, mean/median signed_excess, **Spearman IC via `pandas.Series.corr(method='spearman')`** (no scipy), suppressed when n < 10
- router validation: DELIVERED vs SUPPRESSED excess; flag when SUPPRESSED beats DELIVERED
- acted-vs-ignored split; per-`weight_version` discovery breakdown; `llm_calls` telemetry + prompt-caching-warning

---

## Workstream 4 — Scoring/routing quality (Phase 3)

### 3.1 Demote-before-suppress across runs
`alert_router.py:82-83` suppresses an AR signal when the cross-run AR cap is exhausted — contradicting the spec's demote-before-suppress ordering (`suppressed-signals-spec.md:104-116`). Delete that branch: AR losers always demote into the INFO pool carrying `demoted_from`, and the INFO-full case already emits `cap_exhausted`. Deliberate live behavior change, isolated in its own commit.

### 3.2 Unified competition scale
Discovery scores 0-100; News confidence 0-1. The router compares them raw (`alert_router.py:68,72`), so cross-agent AR competition is meaningless. Add `competition_score(signal)` to stdlib-only `models.py` (`score×100` for news, raw otherwise) and use it in all three sort keys. Display keeps native scales (`format_score` untouched).

### 3.3 Absolute momentum gates
Discovery is pure cross-sectional rank — top of a falling universe still scores ~100. Gate on absolute `momentum_20d`: AR requires ≥ 0.05 (else downgrade to INFO with `gate=abs_mom_ar` in body); INFO requires > 0.0 (else not emitted). Bump `WEIGHT_VERSION` to `"xsect-50/30/20-absgate-v3"` in the same commit so measurement stays clean. Intended consequence: zero discovery alerts in broad downtrends.

### 3.4 (Optional) Advisor funnel
`get_delivered_discovery_signals` → `get_routable_discovery_signals` (include SUPPRESSED so the delivery budget doesn't starve analysis).

---

## Workstream 5 — New-ideas engine (Phase 4)

### 4.1 GATE
Empirically probe Finnhub `general_news()` (general news feed) on the production key. **403 → skip 4.2 entirely.** The rest of Phase 4 is independent.

### 4.2 Macro/general news
`fetch_general_news()` in finnhub_client (rate-limited, retried, paid-tier codes → `[]`). news-morning ingests up to `_MAX_GENERAL_HEADLINES = 20`, capped independently of the 50-company cap, with a shared dedup set (`article_dedup_key` is ticker-independent). Sentinel design: `Signal.ticker=None` end-to-end — `compute_alert_id` normalizes None → `'_'`, snapshot-fetch skipped for None tickers, outcome backfill's `ticker IS NOT NULL` guard keeps macro rows out of price measurement, reconciler groups by `(ticker or "__MACRO__", pillar)`, digest renders `MACRO`.

### 4.3 Universe builder
`data/universe.py`: hardcoded `K1_DENYLIST = frozenset({"USO","UNG","DBC","GSG"})` skipped regardless of CSV flag (the code-level backstop CLAUDE.md demands). New `data/universe_builder.py` merges committed constituent CSVs (`data/constituents/{sp500,nasdaq100}.csv`) with operator `universe.csv`: operator columns preserved verbatim, new tickers get defaults, denylist forced `k1_etf=1`, sector/industry enrichment (bundle → `ticker_meta` table → yfinance stragglers, cached forever). Atomic write via `os.replace` with `.bak`. `build-universe [--apply]` command, dry-run by default. New `ticker_meta` table (ticker PK, sector, industry, source, updated_at).

### 4.4 Industry momentum rollup
`score_universe()` returns `ScoreResult(signals, composite_by_ticker)`; pure `industry_rollup()` computes median composite per industry (min 3 members, top 5) rendered into the discovery digest. Renders nothing pre-4.3 (universe.csv lacks industry columns).

### 4.5 Weekly synthesis job
`weekly_synthesis/weekly_synthesis.py` (pure) + `jobs/weekly_synthesis.py` (standard job). Corpus: MONITORING `[off_thesis]` + `[volume_cap]` from past 7 days (excludes `[parse_failure]`), prefix-stripped, deduped, capped at 200 newest, **every title re-sanitized** (stored titles are only encoding-fixed; `<`/`>` and control chars survive storage — re-sanitize before embedding). ONE `messages.parse` call with `SynthesisResult(themes, pillar_candidates)` (candidates truncated to 3 post-parse), temperature 0, untrusted-content directive, `insert_llm_call(job='weekly_synthesis')`. Rendering: per-candidate ready-to-paste `pillars:` YAML block prefixed "Review and hand-edit before pasting". Empty corpus → explicit message **without** an LLM call (no-silent-day principle), report row still written. Held-set drift warning shared helper. Does NOT touch the router. Scheduled Mondays 8:00 AM ET.

### 4.6 Final docs sweep
CLAUDE.md: new commands, MONITORING prefix taxonomy, new tables, weight_version semantics, competition-scale rule. architecture.md: weekly slot implemented, general-news status per 4.1 probe.

---

## Constraints

- All SQL in `state/repository.py` (schema via `CREATE TABLE IF NOT EXISTS` + `_ensure_column`); sequential jobs, heartbeat-wrapped; no new runtime deps beyond declaring `pandas` (already installed transitively, imported directly by `discovery_agent.py`); Telegram delivery; Windows Task Scheduler.
- TDD per task; one behavior change per commit; full suite green before/after each task.
- `Signal` is frozen/slots — append `weight_version` last; repo tests construct via kwargs.
- Router purity pinned by `test_route_signals_is_pure_no_db_writes`; `competition_score` lives in stdlib-only `models.py`.
- Benchmark entry mismatch (intraday snapshot vs SPY close) is a stated approximation in report output.
