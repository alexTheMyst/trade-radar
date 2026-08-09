# Trade Radar — Batch 2: Reliability, Exit Sentinel, Measurement Depth, Wash-Sale, Eval

## Context

PR #8 closed the measurement loop and built the new-ideas engine. This batch covers the seven highest-leverage remaining changes, chosen in review and approved by the operator: (1) per-job healthchecks UUIDs — the shared UUID lets one job's success mask another's death, defeating the system's core "no silent failure" promise; (2) weekly WAL-safe DB backup — the signal log is the most valuable artifact and lives as one file on one Windows box; (3) a mechanical **exit sentinel** on held positions — the "Mechanical" third signal type `signal-log-schema.md` always reserved; entries are covered, exits are not; (4) an offline **weight-replay CLI** so quarterly Discovery tuning is empirical instead of wait-a-quarter; (5) **confidence calibration** for the News Classifier — the AR slot is priced off unvalidated LLM confidence (0.85/0.60 still carry `TODO(operator)`); (6) wiring the dormant **wash_sale ledger** — brokers don't track wash sales across the 4 accounts, IRA wash sales destroy losses permanently; (7) a **golden-set classifier eval** to run before any `ANTHROPIC_MODEL` change.

**Operator decisions (locked)**: all 7, phased as below. Sentinel attention posture: normal triggers cap at INFORMATIONAL; only severe breaches (≥15% drawdown or ≥8% one-day drop) are AR-eligible.

**Baseline**: PR #8 head `655c9e34b0dd761da455969163900186f03c8f9a` (branch from it, or from main once PR #8 merges — the working tree on main is stale). All cited line numbers refer to that commit.

**Constraints**: no new dependencies; ALL SQL in `state/repository.py`; every scheduled job heartbeat-wrapped + sequential; measurement per signal type, never aggregated; new config vars `_optional` only (conftest sets exactly the 6 required vars); TDD per task, one behavior change per commit, full suite green per phase. This file (docs/superpowers/plans/2026-08-08-reliability-sentinel-measurement.md) is the plan doc — commit it as part of the implementation branch.

---

## Phase 1 — Reliability floor

### 1.1 Per-job heartbeat UUIDs
- `config.py`: `healthchecks_uuid_for(job: str | None) -> str` — slug `HEALTHCHECKS_UUID_<JOB.upper().replace('-','_')>` via `_optional(...)`, fallback shared `HEALTHCHECKS_UUID`. Call-time lookup (tests use plain `monkeypatch.setenv`, no reload).
- `monitoring/heartbeat.py`: `_ping(path, *, job=None)` builds URL from `healthchecks_uuid_for(job)` (replaces direct read at :27); `heartbeat(job: str | None = None)` threads `job` into all three pings. Default `None` = today's behavior bit-for-bit.
- Migrate 7 call sites, each passing its `insert_run` job name literal: `daily_close.py:15`, `discovery.py:57`, `news_morning.py:228`, `outcome_backfill.py:214` (module-attr style) and `advisor.py:89`, `review_report.py:102`, `weekly_synthesis.py:22` (direct-import style — same function object, one signature covers both).
- **Test migration (mandatory)**: the `_noop_heartbeat` stubs in `tests/test_advisor_job.py:24-26` and `tests/test_job_orchestration.py:106-108` become arg-tolerant (`def _noop_heartbeat(*_a, **_k): yield`) or every patched job test raises TypeError. Daily-close smoke tests (real heartbeat + global `patch("httpx.post")`) pass unchanged via fallback.
- New `tests/test_heartbeat.py` (first direct heartbeat tests): shared-UUID ping sequence; per-job env var used; fallback when unset/empty; `/fail` on exception uses the job UUID and re-raises; network failure swallowed; slug-rule unit tests on `healthchecks_uuid_for`.

### 1.2 Weekly backup job
- `repository.py`: `backup_database(dest_dir: Path) -> tuple[Path, int]` — **`VACUUM INTO ?`** (WAL-safe; plain copy can miss -wal pages), file `signals-YYYYMMDD.db` (ET date), `unlink(missing_ok=True)` first (VACUUM INTO refuses overwrite), sanity `SELECT COUNT(*) FROM signals` on the backup, return (path, count). Plus `count_signals()` for the live-side comparison (SQL stays in repository).
- `config.py`: `BACKUP_DIR = _optional("BACKUP_DIR", "state/backups")` (already gitignored via `/state/`).
- New `jobs/backup.py`: canonical job shape, `heartbeat.heartbeat("backup")`; `_resolve_backup_dir()` anchors relative paths at repo root (`repository.DB_PATH.parents[1]` — Task Scheduler cwd untrustworthy); raise RuntimeError if backup count is 0 while live DB has rows; `_prune()` keeps newest 8 matching `^signals-\d{8}\.db$` only (never deletes foreign files). Register `"backup"` in `JOBS`.
- New `tests/test_backup.py`: dated file + count agreement; WAL content captured; same-day rerun overwrites; prune keeps 8 + ignores `notes.txt`; job success + heartbeat URL for job `backup`.

### 1.3 Phase-1 ops/env docs (deploy-blocking subset)
- `.env.example`: commented optional block for all 9 per-job UUIDs + `BACKUP_DIR`.
- `ops/windows-task-scheduler.md`: per-job healthchecks setup with schedules/grace; **rollout order: create the check in healthchecks.io FIRST, then set the env var** (pings to a nonexistent check 404 silently); backup schedule Sundays 8:00 AM ET; restore procedure (stop tasks → copy backup over `state/signals.db` → delete stale `-wal`/`-shm` → re-enable).

---

## Phase 2 — Exit sentinel

### 2.1 Identity + scale contract
- `models.py`: `EXIT_SENTINEL_AGENT = "exit_sentinel"` beside the other constants. **No `competition_score` change** — sentinel emits native 0–100 scores (raw passthrough); comment the constant: an agent not on 0–100 loses every slot competition.

### 2.2 Pure trigger engine — new `sentinel/exit_sentinel.py`
- Constants: `DRAWDOWN_INFO=0.10`, `DRAWDOWN_SEVERE=0.15`, `DAY_DROP_INFO=0.05`, `DAY_DROP_SEVERE=0.08`, `BELOW_COST_INFO=0.05`, `BELOW_COST_SCORE_NORM=0.15`, `WEIGHT_VERSION="sentinel-v1"` (regime stamp), `HISTORY_DAYS=26`, `COOLDOWN_TRADING_DAYS=5`, trigger priority `("drawdown_20d","day_drop_1d","below_cost")`.
- `compute_exit_triggers(df, cost_basis)` — pure, `df.tail(21)`, ≥5 rows else None; raw magnitudes for drawdown-from-20d-high, 1-day drop, below-cost.
- `score_breaches(triggers)` → `(score, primary, breached)` — **score = 100 × min(1, max(magnitude/severe_norm over breached))**; norms 0.15/0.08/0.15 (12% drawdown → 80; 5% day-drop → 62.5; severe → 100, deliberately dominant). None if nothing ≥ its INFO threshold.
- `severity_for(triggers)` — **AR iff drawdown ≥ 0.15 or day_drop ≥ 0.08; else INFORMATIONAL** (weight amplifier deliberately unused — rule-determined severity).
- `build_signal(...)` — **one Signal per ticker per run** (three triggers on a crash day must not triple-bill the shared 3-INFO budget; secondary breaches ride in the body): `direction="negative"` always, `weight_version="sentinel-v1"`, `alert_id = compute_alert_id(ticker, date_iso, f"exit:{primary}", EXIT_SENTINEL_AGENT)`, snapshot from quote with candle-close fallback (**must be non-null** or the row is invisible to `list_signals_for_review`'s snapshot filter).
- `dedupe_holdings(holdings)` — per-ticker share-weighted average cost basis across accounts (documented choice).

### 2.3 Cooldown
- `repository.py`: `get_signal_meta_by_alert_ids(alert_ids)` — IN-list select of `{alert_id, severity, demoted_from, routing_status, timestamp}`, excluding MONITORING; empty input → `[]`.
- Algorithm: prior 5 trading dates from the ticker's own candle index; reconstruct `compute_alert_id` per date × same `exit:{primary}` rule; any routable prior match → downgrade to `[cooldown]` MONITORING (persist directly, `routing_status="MONITORING"` — MONITORING must never enter the router), **unless** new severity is AR and no prior row was severe (`severity='ACTION_REQUIRED'` or `demoted_from='ACTION_REQUIRED'`) — escalation always gets through. Trigger-shift re-alerts are accepted (documented fail-open).

### 2.4 Job — new `jobs/sentinel.py`
- Canonical shape, `heartbeat.heartbeat("sentinel")`, `require_non_empty_holdings()` (loud on missing), ONE `fetch_history` batch, `fetch_quotes` for triggered tickers only, cooldown partition, `route_signals` → `compute_score_deltas` **before** `persist_routed_signals` → `render_digest(job_name="sentinel", ..., monitoring_breakdown={"cooldown": n})` → `validate_digest_payload` → send → confirm. `jobs/common.py`: add `("cooldown","cooldown")` to `_MONITORING_CATEGORY_LABELS`. Register `"sentinel"` in `JOBS`.

### 2.5 Measurement integration (same PR — never ship an unmeasured agent)
- `repository.list_signals_for_review` (:951): add `'exit_sentinel'` to the agent IN-list (outcome backfill needs nothing — no agent filter there).
- `review/report.py`: `_AGENTS` += `"exit_sentinel"`; `_signed_excess` flips for `agent in ("news_classifier","exit_sentinel") and direction=="negative"` (success = price fell after the alert); neutral-exclusion stays news-only.
- `jobs/review_report.py:28`: replace binary label ternary with an `_AGENT_LABELS` dict (third agent currently mislabels as "News Classifier").

### 2.6 Tests + docs
- New `tests/test_exit_sentinel.py`: threshold boundaries (breach is `>=`), score pins (80/62.5/100 cap), severity mapping (14.9% → INFO, 15% → AR), tie priority, weighted basis, short-history None.
- New `tests/test_sentinel_job.py` (job-module attr patching + db fixture): happy path row (`agent/direction/weight_version/snapshot`), zero-breach digest line, cooldown downgrade, escalation bypass, missing holdings → failed run + `/fail` ping.
- `tests/test_review_report.py`: sentinel rows via the raw-SQL seeder — sign flip, per-agent separation, discovery/news numbers unchanged.
- Ops: `sentinel` weekdays 5:00 PM ET + `HEALTHCHECKS_UUID_SENTINEL`.

---

## Phase 3 — Measurement depth

### 3.1 Shared Spearman — new `review/stats.py`
- Move `_spearman_ic` verbatim as public `spearman_ic(scores, excesses, *, min_n=10)`; `report.py` imports it (existing tests stay green untouched).

### 3.2 Universe accessor
- `data/universe.py`: `get_all_tickers() -> list[str]` = `sorted(get_position_weights())` (all non-K1, no rotation) + test.

### 3.3 Replay engine + CLI — new `replay/weight_replay.py`
- Operator command `replay-weights` (**no heartbeat, no runs row, no DB access** — SPY from the same yfinance batch): args `--days 180`, repeatable `--weights 50/30/20`, `--horizons 5,20`, `--gates on|off` (hand-rolled parsing per house style).
- Pure `run_replay(history, spy, weight_sets, horizons, gates_on)`: per trading day, reuse **imported** `_compute_factors`/`_rank_values` (factors/ranks computed once per day, shared across weight sets); composite = exact live formula; gates on = drop pairs with `momentum_20d < ABS_GATE_INFO_MOMENTUM_20D` (constants imported from discovery_agent, never re-declared); forward excess vs SPY over matching calendar dates (missing SPY dates dropped + counted); per cell: `spearman_ic`, hit-rate over composite ≥ 60 pairs (the would-be-emitted set), `n_pairs`/`n_emittable`.
- Output: stdout table + **mandatory caveat lines** (survivorship bias — today's universe; pooled-pairs IC across days).
- New `tests/test_weight_replay.py`: synthetic monotone history → IC ≈ 1; gates n-difference; `parse_weights` errors; hit-rate threshold; SPY-gap handling; CLI exit-2 paths.

### 3.4 Confidence calibration (news only)
- `review/report.py`: buckets `[0.60,0.70) [0.70,0.80) [0.80,0.85) [0.85,1.00]` (+ `calibration_out_of_range` count — amplifier can deliver sub-0.60 confidences); per bucket n / hit-rate / mean signed excess (30d, existing signed-excess pipeline, neutral excluded); `AgentMetrics.calibration`, `ReportResult.calibration_flags`; MISCALIBRATION flag when top bucket's hit rate < a lower bucket's with both n ≥ 10.
- `jobs/review_report.py`: "Confidence calibration (news)" section + `!` flag lines.
- Tests in `tests/test_review_report.py`: boundary assignment, flag n-gating, agent isolation, out-of-range counting.

---

## Phase 4 — Wash-sale ledger

### 4.1 Schema + helpers (`repository.py`)
- `_ensure_column(cursor, "wash_sale", "side", "TEXT")` (signed-quantity encoding rejected — opaque to sqlite3 inspection); **`cost_basis` reused as per-share trade price**, documented at DDL and helper.
- `WASH_SALE_ACCOUNTS = ("schwab_main","schwab_secondary","roth_ira","hsa")` constant mirroring the CHECK literals.
- `insert_wash_sale_trade(*, ticker, account, trade_date, side, quantity, price, notes=None)` — friendly ValueError on bad side/account (IntegrityError is backstop, not UX). `get_recent_trades(ticker, *, since, side=None)` — **no account filter, cross-account by design** (docstring cites the IRS cross-account rule).

### 4.2 CLI `log-trade`
- `log-trade <TICKER> --buy|--sell --account <literal> --qty N --price P [--date YYYY-MM-DD] [--note]`; default date = ET today; validation exits 2 with the literal list; on `--sell`, warn if any-account buy within prior 30 days (potential wash sale if sold at a loss). Tests in `tests/test_cli_commands.py`.

### 4.3 Advisor integration
- `verdict_engine.compute_verdict(..., recent_loss_sale: bool = False)` — Overlay 4 before the final return: `verdict=="BUY" and recent_loss_sale → flags.append("wash_sale_window")` (covers held add-to and new-buy; PASS discards flags — documented, acceptable: the warning rides on the actionable case). Docstring: `wash_sale_caution` = sell side, `wash_sale_window` = repurchase side, complementary; "recent sale" = any logged in-window sale (conservative — ledger lacks the original basis).
- `advisor_agent.produce_advice(..., get_recent_sales=None)` injected-callable seam; wire lambda in `jobs/advisor.py` (`repository.get_recent_trades(t, since=since, side="sell")`) + same lookup in `advise_ticker`; `rationale.py` template mentions the flag.
- New `tests/test_wash_sale.py` (round-trip, cross-account, side filter, idempotent migration, window boundary) + verdict/advisor test extensions (default-False pins 4 call sites; `extended` demotion ordering: demoted HOLD does **not** get the flag).

---

## Phase 5 — Golden-set eval + docs sweep

### 5.1 Fixtures (committed)
- `eval/thesis-eval.yaml` — frozen copy of thesis.example.yaml with `review_due: 2099-01-01` (passes the staleness gate forever; accuracy comparable across models).
- `eval/golden_headlines.yaml` — `set_version: 1` + 20 cases `{id, headline, expected_pillar (name|null), expected_direction (…|null=unchecked)}`: ~9 on-thesis split pos/neg/neutral, ~4 plausible off-thesis, **2 prompt-injection probes** expecting null pillar.

### 5.2 Harness — new `evals/classifier_eval.py` + classifier extensions
- `news_classifier.py`: public alias `build_system_prompt = _build_system_prompt`; keyword-only `llm_job: str = "news_classifier"` on `classify_headline`, threaded into both `insert_llm_call` sites (default keeps news-morning telemetry byte-identical).
- Harness: repo-root-anchored paths; `yaml.safe_load` only; pure `grade_case` (pillar match; direction checked only when non-null; `[parse_failure]` → FAIL); `run_eval` builds the prompt once, calls the real API with `ticker=None, weights={}, llm_job="eval_classifier"`; prints per-case PASS/FAIL + `Accuracy: X/20 — model — set v1`; exit 0 always (readout, not gate). CLI `eval-classifier` — **no heartbeat, no runs row**.
- New `tests/test_eval_classifier.py` — API fully mocked (loading, grading matrix, accuracy math, `llm_job`/`ticker=None` assertions); the live run is an operator action.

### 5.3 Docs sweep
- CLAUDE.md: new commands (`sentinel`, `backup`, `replay-weights`, `eval-classifier`, `log-trade`); third-agent section with **explicit sign-flip semantics** (sentinel hit = price fell); per-job heartbeat rule; backup/restore; wash_sale semantics (side column, price-in-cost_basis, cross-account).
- `ops/windows-task-scheduler.md`: full 9-job schedule table (adds sentinel 5:00 PM ET weekdays, backup Sun 8:00 AM); eval-classifier runbook (run before/after any `ANTHROPIC_MODEL` change, compare); `replay-weights` before each quarterly weight review.
- `architecture.md` job table; `.env.example` final check.

---

## Risks
1. **Router budget now has 3 competitors** (biggest live change): a severe sentinel breach at 5:00 PM can find the AR slot consumed by discovery (~4:15 PM) and arrive as demoted INFO (visible via `demoted_from`). If unacceptable in practice, swap the sentinel/discovery schedule order — don't change budgets.
2. **Measurement invariants**: `_signed_excess`/`_AGENTS`/SQL widening must leave discovery/news numbers bit-identical (existing test_review_report tests pin this — run before/after 2.5). Sentinel hit-rate semantics must be labeled in the report or it reads backwards.
3. **Heartbeat rollout order**: check-first-then-env, else that job's pings silently go to a 404 while the shared check goes quiet.
4. **Sentinel data lag**: if yfinance lacks today's candle at 5 PM ET, triggers evaluate on yesterday's close — documented limitation.
5. Signature growth (`compute_verdict`, `produce_advice`, `classify_headline`, `heartbeat`) is all default-preserving — pinned by tests.
6. Replay caveat lines are part of the deliverable — without them the numbers will be over-trusted.

## Verification
- Per task: named test files; per phase: full `uv run pytest` green. Phase-level manual smokes: `backup` (path + count printed), `sentinel` (with populated holdings.csv), `review-report` (third section + calibration), `replay-weights --days 40 ...` (caveats print), `log-trade` round-trip + warning, `eval-classifier` live once (~20 API calls; `llm_calls` shows `eval_classifier`).
- **Operator deployment checklist** (Windows box): create 9 healthchecks checks, then set per-job env vars; add Task Scheduler entries for `sentinel` (5:00 PM ET weekdays) and `backup` (Sun 8:00 AM ET); run `backup` once manually and verify the file restores; keep holdings.csv current (sentinel scans holdings, not core_holding flags); log all trades via `log-trade` from day one — the wash-sale check is only as good as the ledger.

## Critical files
`state/repository.py` · `monitoring/heartbeat.py` + `config.py` · `review/report.py` + `jobs/review_report.py` · `discovery/discovery_agent.py` (pure factors/ranks + gate constants reused by replay) · `advisor/verdict_engine.py` + `advisor/advisor_agent.py` + `jobs/advisor.py` · `__main__.py` · new: `jobs/backup.py`, `sentinel/exit_sentinel.py`, `jobs/sentinel.py`, `review/stats.py`, `replay/weight_replay.py`, `evals/classifier_eval.py`, `eval/*.yaml`, `tests/{test_heartbeat,test_backup,test_exit_sentinel,test_sentinel_job,test_weight_replay,test_wash_sale,test_eval_classifier}.py`
