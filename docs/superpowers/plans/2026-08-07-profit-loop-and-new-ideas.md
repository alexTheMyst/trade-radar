# Trade Radar — Profit Loop + New-Idea Discovery Improvements

## Context

Full project review (3 explore passes + file-level verification) of the live alert-only signal system found the core plumbing solid (router, digests, heartbeat, suppressed-signals visibility) but three structural problems blocking the operator's goals of higher profit and finding new ideas/pillars:

1. **Production-down bug**: `src/signal_system/data/thesis_loader.py:13` imports only `datetime`, but line 53 annotates `review_due: date`. Pydantic raises `PydanticUserError` on every `load_thesis()` → **news-morning and advisor jobs are dead**; 3 tests in `tests/test_smoke.py` fail.
2. **Profit is unmeasurable, therefore untunable**: `acted`/`acted_at` columns have no write path; no SPY benchmark series (daily-close writes one SPY close as a fake signal row — verified: **no readers**); no `weight_version` column (signals from two scoring regimes indistinguishable — regime changed at commit `5136501`, 2026-05-31); outcome-backfill built but unscheduled (deferral expired ~mid-June); zero analysis tooling (no IC/hit-rate anywhere); `llm_calls` written but never read. Plus: Discovery is pure cross-sectional rank (top of a falling universe still scores ~100, no absolute gate); router compares Discovery 0–100 vs News 0–1 confidence raw (`alert_router.py:68,72`) so cross-agent AR competition is meaningless; cross-run AR-cap exhaustion suppresses without attempting demotion (`alert_router.py:82-83`), contradicting CLAUDE.md:61 and `docs/suppressed-signals-spec.md:104-116`.
3. **New-idea discovery is architecturally impossible**: off-thesis classifications silently discarded (`news_classifier.py:422-423` — paid-for LLM rationale thrown away, no audit trail); no macro/general news feed (company news for core holdings only); MONITORING lane is write-only (the weekly-digest reader in architecture.md was never built); Discovery can only rank the ~70–90 hand-typed tickers in gitignored `universe.csv` (no builder); K-1 denylist (USO/UNG/DBC/GSG) exists nowhere in code.

**Operator decisions (locked)**: all four workstreams, phased; new pillars proposed via a weekly synthesis job; universe builder + industry momentum rollup; add Finnhub general-news feed (validate free tier first).

**Methodology**: TDD per task (superpowers), one behavior change per commit, full-suite green before/after each task. When implementation starts, first commit this spec/plan into `docs/superpowers/specs/` + `docs/superpowers/plans/` per repo convention (`2026-08-07-profit-loop-and-new-ideas-*.md`).

**Constraints**: all SQL in `state/repository.py` (schema via `CREATE TABLE IF NOT EXISTS` + `_ensure_column`); sequential jobs, heartbeat-wrapped; no new runtime deps (pandas is already installed transitively — declare it); Telegram delivery; Windows Task Scheduler.

---

## Phase 0 — Restore production

### 0.1 Fix thesis_loader import (do first; suite is red until this lands)
- `src/signal_system/data/thesis_loader.py:13` → `from datetime import date, datetime`. Nothing else.
- Red tests already exist (the 3 thesis failures in `test_smoke.py`).
- Verify: `uv run pytest tests/test_smoke.py -k thesis`, then **full `uv run pytest` to establish the green baseline**.

### 0.2 Correct stale docs (zero code risk)
- `CLAUDE.md`: remove "Phase A log-only" wording and the ~1,500-ticker / 9-18-min claims; remove `candidate_signals`/`daily_budget` from "tables needed" (never built — router is pure over `count_delivered_today()`); mark `llm_calls`/`wash_sale` built; amend the httpx rejection row (`monitoring/heartbeat.py` legitimately uses it).
- `ops/windows-task-scheduler.md`: drop the stale Gmail SMTP mention.

---

## Phase 1 — Stop losing new-idea raw material (land immediately so the corpus accumulates)

### 1.1 Persist off-thesis classifications as MONITORING signals
- `news_classifier.py` `classify_headline()`: replace `if parsed.pillar_name is None: return None` (lines 422-423) with a MONITORING Signal: same precomputed `alert_id`, `title=f"[off_thesis] {_fix_encoding(raw)[:120]}"`, `body=parsed.rationale`, `score=parsed.confidence`, `direction=parsed.direction`, `pillar=None`, model/thesis stamps. Update the docstring.
- No job change needed: `news_morning.py:175-179` already persists any severity==MONITORING signal with `routing_status="MONITORING"`; the snapshot-fetch guard already skips Finnhub quotes for pure-MONITORING tickers. Existing readers (`get_previous_score`, `get_recent_signals`) already exclude MONITORING — no pollution.
- Tests: rewrite `test_smoke.py::test_classify_returns_none_when_pillar_name_none` → asserts MONITORING signal with `[off_thesis]` prefix; update `test_news_classifier.py::test_no_quote_call_for_offthesis_headlines` (keep the no-quote assertion, expect 1 MONITORING signal); add alert_id-stability test (same headline → same alert_id on- or off-thesis).

### 1.2 Monitoring breakdown in the news digest
- `jobs/common.py` `render_digest()`: new kwonly `monitoring_breakdown: dict[str,int] | None = None` → renders `Monitoring: N (off-thesis A, parse-fail B, volume-cap C, reconciled D)`, zero categories omitted. `validate_digest_payload` unaffected.
- `news_morning.py` `_classify_kept_headlines()`: return per-category counts (tally by title prefix); `run()` composes the dict.
- Tests: extend `tests/test_job_orchestration.py` digest tests.

### 1.3 Repository read path for the MONITORING corpus
- `repository.py`: `get_monitoring_signals_since(since, *, title_prefixes=("[off_thesis]", "[volume_cap]"), limit=500)` — prefix LIKE filters (safe: `[` is not a SQLite LIKE wildcard), newest first.
- Tests: new `tests/test_repository_measurement.py` (copy the `monkeypatch DB_PATH` fixture pattern from `test_alert_router.py`).

---

## Phase 2 — Close the measurement loop

### 2.1 `benchmark_prices` table + accessors
- `repository.init_db()`: `CREATE TABLE IF NOT EXISTS benchmark_prices (ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL, source TEXT, PRIMARY KEY (ticker, date))`.
- Accessors: `upsert_benchmark_price(...)` (INSERT OR REPLACE — yfinance backfill may overwrite quote-sourced rows), `get_benchmark_close_on_or_after(ticker, date_iso)` (first trading day ≥ target, mirrors outcome-backfill convention), `get_earliest_signal_date()`.

### 2.2 daily-close writes the benchmark; retire the fake SPY signal
- `jobs/daily_close.py`: replace the `insert_signal` block with `upsert_benchmark_price(ticker="SPY", ..., source="finnhub_quote")`. Keep fetch, Telegram message (drop the Alert ID line), heartbeat, runs bookkeeping — news-morning gates on the **runs** table, not the signal row. Historical `agent='DAILY_CLOSE'` rows stay (inert).
- Tests: update the two `daily_close` smoke tests (assert benchmark row, assert no signals row; benchmark row persists on Telegram failure).

### 2.3 One-time SPY history backfill
- `jobs/outcome_backfill.py`: new `backfill_benchmark(*, ticker="SPY")` — from `get_earliest_signal_date()` to now via existing `yahoo_client.fetch_history`, upsert each dated close (`source="yfinance_backfill"`).
- `__main__.py`: special-case command `backfill-benchmark` (no heartbeat, like `advise`). review-report (2.6) reports "N signals skipped: missing benchmark" as the completeness check.
- Tests: `tests/test_outcome_backfill.py` conventions (fabricated DataFrame; assert overwrite of a `finnhub_quote` row).

### 2.4 Acted-recording CLI + unacted nag
- `repository.py`: `find_signals_by_alert_prefix(prefix)` (validate `^[0-9a-f]{4,64}$`, ValueError otherwise), `update_signal_acted(alert_id, *, acted, user_note)` (stamps `acted_at` ET now), `list_recent_delivered_signals(days=14)`, `count_unacted_delivered_older_than(days=7)`.
- `__main__.py`: `list-signals [--days N]` and `log-action <prefix> --acted|--passed [--note "..."]` — manual argv parsing like `advise`; no heartbeat/runs row. Ambiguity guard: 0 matches → error exit 2; >1 → print candidates, exit 2. Refactor handlers into `_cmd_*` functions so tests call them directly.
- Nag: in `news_morning.run()`, when `count_unacted_delivered_older_than(7) > 0`, append the nag line **at send-composition time** (`DigestPayload` is frozen — compose `digest.body + nag` into `_send_digest_once`, after `validate_digest_payload`).
- Tests: `tests/test_repository_measurement.py` + new `tests/test_cli_commands.py`.

### 2.5 `weight_version` stamping
- `repository.py`: `_ensure_column(signals, "weight_version", "TEXT")`; add to `insert_signal` columns/params. `models.py`: `weight_version: str | None = None` appended to `Signal`. `discovery_agent.py`: `WEIGHT_VERSION = "xsect-50/30/20-v2"`, stamped on every emitted signal; docstring documents NULL = mixed legacy regimes (pre-2026-05-31 single-day + unstamped 50/30/20).
- Tests: schema assertion in `test_smoke.py::test_init_db_idempotent_and_new_schema`; stamp assertion in `test_discovery_agent.py`.

### 2.6 `review-report` job — the tuning readout
- New `src/signal_system/review/report.py` (pure computation) + `jobs/review_report.py` (standard job shape: runs row, heartbeat, print + Telegram) + `JOBS["review-report"]`.
- `repository.py`: `list_signals_for_review()` (agents `discovery_agent`/`news_classifier`, routing DELIVERED/SUPPRESSED, snapshot NOT NULL — excludes legacy DAILY_CLOSE rows by construction) and `get_llm_call_summary()` (first-ever reader of `llm_calls`).
- Metrics, **per agent per horizon (30d/90d), never aggregated**:
  - `raw_ret = outcome/snapshot − 1`; `spy_ret` from `get_benchmark_close_on_or_after(date)` → `(date+Xd)`; `excess = raw − spy`; rows missing either benchmark lookup counted as `missing_benchmark`, excluded.
  - Direction adjustment (news): `signed_excess = −excess if direction=='negative' else excess`; neutral rows excluded from hit-rate/IC (reported as count). Discovery: signed = excess.
  - `hit_rate = share(signed_excess > 0)`; mean/median signed_excess; **Spearman IC = `frame["score"].corr(frame["signed_excess"], method="spearman")`** (pandas only, no scipy), suppressed when n<10.
  - Router validation: mean/median excess_30d DELIVERED vs SUPPRESSED per agent; flag when SUPPRESSED beats DELIVERED.
  - Acted-vs-ignored split among DELIVERED with `acted IS NOT NULL`; unacted count.
  - Per `weight_version` breakdown for discovery (NULL bucket = `legacy-mixed`).
  - `llm_calls` section: per job calls/tokens/cache sums; warn "prompt caching INACTIVE" when news_classifier has ≥20 calls and cache_read+cache_creation == 0 (2-pillar system prompt is likely under the ~1024-token cacheable minimum).
- `pyproject.toml`: declare `pandas` (already installed transitively via yfinance; `discovery_agent.py` imports it directly today).
- Tests: new `tests/test_review_report.py` with hand-computed fixtures (excess math, sign flip, neutral exclusion, IC vs hand-ranked expectation, n<10 floor, missing-benchmark count, splits, cache warning).

### 2.7 Schedule measurement jobs (docs)
- `ops/windows-task-scheduler.md`: `outcome-backfill` daily 5:30 PM ET weekdays (overdue); `review-report` monthly (first Saturday 9:00 AM ET + on-demand pre-quarterly-review); one-time `backfill-benchmark` bootstrap; `log-action`/`list-signals` usage.

---

## Phase 3 — Scoring/routing quality (allocate the scarce AR slot by merit)

### 3.1 Router: demote-before-suppress across runs
- `alert_router.py`: delete the `elif ar_remaining == 0 and ar_used >= BUDGET_AR:` suppress branch (lines 82-83) — AR losers always demote into the INFO pool carrying `demoted_from` (INFO-full case already emits `cap_exhausted` + demoted_from). Update docstring.
- Tests: rewrite `test_alert_router.py::test_cross_run_ar_full` (deliberate expectation change, per spec); add both-budgets-exhausted case.

### 3.2 Unified competition scale (display keeps native scales)
- `models.py`: add `NEWS_CLASSIFIER_AGENT`/`DISCOVERY_AGENT` constants + `competition_score(signal)` → `score×100` for news, raw otherwise (stdlib-only module so the router doesn't import the anthropic-heavy classifier).
- `news_classifier.py`: re-import `NEWS_CLASSIFIER_AGENT` from models (all existing import sites keep working). `alert_router.py`: all three sort keys use `-competition_score(s)`.
- `format_score`/`format_score_delta` untouched.
- Tests: cross-agent AR competition (news 0.92 beats discovery 88), tie → ticker tiebreak.

### 3.3 Absolute momentum gates + weight_version bump (same commit)
- `discovery_agent.py`: `ABS_GATE_AR_MOMENTUM_20D = 0.05`, `ABS_GATE_INFO_MOMENTUM_20D = 0.0`. After `adjusted_severity`: AR with `momentum_20d < 0.05` → downgrade to INFORMATIONAL, append ` gate=abs_mom_ar` to body; INFORMATIONAL with `momentum_20d <= 0.0` → not emitted (existing `continue` path). Bump `WEIGHT_VERSION` to `"xsect-50/30/20-absgate-v3"`.
- Tests: negative-momentum top ticker emits nothing; +3% caps at INFO with gate marker; +6% fires AR; v3 stamp.

### 3.4 (Optional) Widen the advisor's discovery funnel
- `repository.get_delivered_discovery_signals` → `routing_status IN ('DELIVERED','SUPPRESSED')`, rename `get_routable_discovery_signals` (staff-review Improvement 3: delivery budget shouldn't starve analysis). Update `jobs/advisor.py` call site + advisor tests.

---

## Phase 4 — New-ideas engine

### 4.1 GATE: empirically probe Finnhub `/news` (general) on the production key
- `finnhub-python` has `general_news(category, min_id=0)` (verified in installed client). Run the probe with the real key; record fields/recency in ops docs. **403 → skip 4.2 entirely** (rest of Phase 4 is independent).

### 4.2 Macro/general news into news-morning
- `finnhub_client.py`: `fetch_general_news()` — `_acquire_slot()`, retry decorator, paid-tier codes → `[]` (mirrors company-news wrapper).
- `news_morning.py`: `_MAX_GENERAL_HEADLINES = 20` capped **independently** of the 50-company cap (macro can never crowd out held-position coverage) with a **shared dedup set** (`article_dedup_key` is ticker-independent); general overflow → volume-cap MONITORING rows with `ticker=None`.
- Sentinel design (resolved): `Signal.ticker=None` end-to-end. `classify_headline(s)` accept `ticker: str | None`; dedup namespace `ticker or "GENERAL"`; `compute_alert_id` already normalizes None → `'_'`; skip price-snapshot fetch when ticker is None (outcome backfill's existing `ticker IS NOT NULL` guard keeps macro rows out of price measurement — correct). `_weight_adjusted_severity` already handles it via pillar `holdings_exposed` lookup (news_classifier.py:245-251).
- `reconciler.py`: group by `(sig.ticker or "__MACRO__", sig.pillar)` so contradictory macro headlines net out.
- `jobs/common.py`: render `signal.ticker or "MACRO"` in delivered/suppressed lines (currently "UNKNOWN").
- Tests: fetch wrapper cases; macro classification (no quote call, `'_'`-prefixed alert_id, cross-feed dedup); macro reconciliation; digest MACRO rendering; independent caps.

### 4.3 Universe builder + hardcoded K-1 denylist + `ticker_meta` cache
- `data/universe.py`: `K1_DENYLIST = frozenset({"USO","UNG","DBC","GSG"})`; all three loaders skip denylisted tickers **regardless of CSV flag** (the code-level backstop CLAUDE.md:87 demands); add `get_industries()` reader.
- New `data/universe_builder.py` + committed seed CSVs `data/constituents/{sp500,nasdaq100}.csv` (`ticker,name,sector,industry` — checked-in snapshots; no runtime scraping). Merge logic: existing `universe.csv` rows keep `core_holding`/`weight_pct`/`k1_etf` **verbatim**; new tickers get defaults; denylist forced `k1_etf=1`; sector/industry enrichment priority: bundle → `ticker_meta` table → yfinance `Ticker.info` for stragglers only (1s sleep, cached forever). Atomic write (`universe.csv.new` → `os.replace`) with `.bak`; comment rows dropped (documented in command output).
- `repository.py`: `ticker_meta` table (`ticker PK, sector, industry, source, updated_at`) + upsert/get accessors.
- `__main__.py`: `build-universe [--apply]`, default dry-run printing the diff.
- Tests: new `tests/test_universe_builder.py` (operator columns preserved, denylist forced, dedup, dry-run writes nothing, .bak on apply); extend K-1 smoke tests (unflagged USO row still excluded).

### 4.4 Industry momentum rollup in the discovery digest
- `discovery_agent.py`: `score_universe()` returns `ScoreResult(signals, composite_by_ticker)` (rollup needs sub-threshold scores); pure `industry_rollup(composite_by_ticker, industries, *, min_members=3, top_n=5)` — median composite per industry, sorted desc.
- `jobs/discovery.py`: adapt to `ScoreResult`; append "Industry momentum (median composite, n>=3)" section to the digest; renders nothing when universe.csv lacks industry columns (graceful pre-4.3).
- Tests: rollup math; digest section present/absent.

### 4.5 Weekly synthesis job — pillar candidates from the corpus
- `repository.py`: `get_pillar_routing_counts_since(since)`, `get_agent_signals_since(agent, since)`, `synthesis_reports` table (`run_id, timestamp, week_start, corpus_size, themes_json, candidates_json, model_version`) + insert.
- New `synthesis/weekly_synthesis.py`: Pydantic `SynthesisResult(themes: list[Theme], pillar_candidates: list[PillarCandidate])`; candidates truncated to 3 post-parse. Corpus: `get_monitoring_signals_since(now−7d)` — `[off_thesis]` + `[volume_cap]` only (`[parse_failure]` excluded), prefix-stripped, deduped, **capped at 200 newest**, **every title re-sanitized through `_sanitize_headline`** (stored titles are only encoding-fixed — control chars/angle brackets survive; private import has precedent: `jobs/advisor.py` imports `_compute_sma`), each wrapped in `<headline>` tags inside `<item>` wrappers with off-thesis rationale (truncated 200 chars). Trusted context outside tags: per-pillar delivered/suppressed counts + the week's industry rollup. ONE `messages.parse` call, max_tokens 2000, untrusted-content directive in system prompt; `insert_llm_call(job="weekly_synthesis", ...)`; `parsed_output is None` → raise (heartbeat /fail — broken synthesis must be loud).
- Rendering: themes; per candidate a **ready-to-paste `pillars:` YAML block** via `yaml.safe_dump(..., sort_keys=False)` prefixed "Review and hand-edit before pasting — promotion is manual" (architecture.md:71). Empty corpus → explicit "corpus empty" message **without** an LLM call (no-silent-day principle), synthesis_reports row still written.
- Held-set drift warning: shared helper `holdings_universe_drift()` in `data/holdings.py` (holdings.csv tickers lacking core_holding=true) — appended here and as a review-report section.
- New `jobs/weekly_synthesis.py` (standard job shape, does NOT touch the router) + `JOBS["weekly-synthesis"]`; ops doc: Mondays 8:00 AM ET (the slot architecture.md reserves).
- Tests: new `tests/test_weekly_synthesis.py` (mocked Anthropic per `test_smoke.py` pattern): 200-cap, re-sanitization (embedded `</headline>` arrives escaped), empty-corpus → zero API calls, ≤3 candidates, YAML round-trips through `yaml.safe_load` into a valid `Pillar`, DB rows written, drift warning.

### 4.6 Final docs sweep
- `CLAUDE.md`: new commands, MONITORING title-prefix taxonomy, new tables, weight_version semantics, competition-scale rule. `architecture.md`: weekly slot implemented; general-news status per 4.1 probe.

---

## Risks / watch items
- Suite is red until 0.1 — land it first, full-suite green baseline before anything else.
- Deliberate live-behavior changes, each isolated in its own commit: 3.1 (demoted AR can consume an INFO slot), 3.2 (news can now win AR over discovery), 3.3 (zero discovery alerts in broad downtrends — intended; v3 stamp isolates measurement).
- Finnhub general feed may be paid-only — 4.1 gates 4.2 only.
- `Signal` is frozen/slots — append `weight_version` last; repo tests construct via kwargs (verified safe).
- Router purity pinned by `test_route_signals_is_pure_no_db_writes`; `competition_score` lives in stdlib-only `models.py`.
- Telegram 4096 chunking already handled (`_split_message`); synthesis/review messages can be long.
- Benchmark entry mismatch (intraday snapshot vs SPY close) is a stated approximation in report output.

## Verification
- Per task: the named test files; per phase: `uv run pytest` full suite green.
- Local checkout has no `universe.csv`/`thesis.yaml`/`.env` (all gitignored) — jobs can't run locally; `tests/conftest.py` handles env. Job-level verification happens on the Windows runner.
- **Operator deployment checklist** (Windows box, after merge): `git pull` + `uv sync`; confirm next `news-morning` heartbeat success (0.1); run `uv run python -m signal_system backfill-benchmark` once (2.3); create Task Scheduler entries: `outcome-backfill` daily 5:30 PM ET, `review-report` monthly, `weekly-synthesis` Mon 8:00 AM ET (2.7/4.5); run the 4.1 general-news probe and record the result; run `build-universe` dry-run, review diff, then `--apply` (4.3); run `review-report` on demand to sanity-check metrics over existing ~2.5 months of signals.

## Critical files
`src/signal_system/state/repository.py` · `src/signal_system/classifier/news_classifier.py` · `src/signal_system/jobs/news_morning.py` · `src/signal_system/router/alert_router.py` · `src/signal_system/discovery/discovery_agent.py` · `src/signal_system/jobs/daily_close.py` · `src/signal_system/jobs/outcome_backfill.py` · `src/signal_system/data/universe.py` · `src/signal_system/models.py` · `src/signal_system/__main__.py` · new: `review/report.py`, `jobs/review_report.py`, `data/universe_builder.py`, `synthesis/weekly_synthesis.py`, `jobs/weekly_synthesis.py`
