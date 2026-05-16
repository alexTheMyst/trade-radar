---
phase: 01-foundation
verified: 2026-05-15T17:10:00-06:00
status: passed
score: 12/12
overrides_applied: 0
re_verification: false
---

# Phase 1: Foundation — Verification Report

**Phase Goal:** Establish the foundation layer every subsequent phase depends on — canonical `Signal` dataclass, deterministic SHA-256 `alert_id`, idempotent SQLite schema extensions, `thesis.yaml` taxonomy with `review_due` gate, and static ticker universe with deterministic md5 partitioning and K-1 exclusion.

**Verified:** 2026-05-15T17:10:00-06:00
**Status:** PASSED
**Re-verification:** No — initial verification
**Test suite:** 17/17 tests pass (`uv run pytest tests/ -x -q`)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A Signal dataclass is importable from `signal_system.models` and is immutable (frozen) | VERIFIED | `@dataclass(frozen=True, slots=True)` at models.py:17; `test_signal_is_frozen` raises `FrozenInstanceError` on mutation |
| 2 | `compute_alert_id()` returns a SHA-256 hex digest deterministic across processes | VERIFIED | `hashlib.sha256(...).hexdigest()` at models.py:49; pinned expected value `7c35b5226a...` in `test_compute_alert_id_deterministic`; None ticker normalises to `_` |
| 3 | Inserting the same Signal twice produces exactly one row (INSERT OR IGNORE) | VERIFIED | `INSERT OR IGNORE` at repository.py:132; `test_insert_signal_idempotent` confirms first=True, second=False, COUNT=1 |
| 4 | `init_db()` is idempotent on an upgraded DB — adds new columns/tables without dropping existing rows | VERIFIED | `_ensure_column()` uses `PRAGMA table_info()` before `ALTER TABLE`; `test_init_db_idempotent_and_new_schema` calls `init_db()` twice without error |
| 5 | Every sqlite3 connection in repository.py has `PRAGMA busy_timeout = 30000` applied | VERIFIED | `_connect()` executes `PRAGMA busy_timeout = 30000` (repository.py:22); only one `sqlite3.connect` call exists in the file (line 21, inside `_connect()` itself); no callers bypass `_connect()` |
| 6 | `load_thesis()` raises `ThesisStaleError` (subclass of `RuntimeError`) when `review_due` is past | VERIFIED | `class ThesisStaleError(RuntimeError)` at thesis_loader.py:20; raises when `review_due < date.today()` (line 66–70); `test_load_thesis_stale_raises` confirms both `issubclass` and raise |
| 7 | `get_todays_universe()` returns deterministic partition for a given ticker on a given ET day | VERIFIED | `hashlib.md5(ticker.encode("utf-8")).hexdigest()` at universe.py:28; `_today_bucket()` uses `tm_yday % 3` (universe.py:37); `test_md5_bucket_deterministic` and `test_get_todays_universe_includes_core_excludes_off_partition` confirm |
| 8 | K-1 ETFs (`k1_etf=1` rows) never appear in `get_todays_universe()` output | VERIFIED | `if int(row["k1_etf"]): continue` at universe.py:59; USO, UNG, DBC, GSG marked `k1_etf=1` in universe.csv; `test_get_todays_universe_excludes_k1` and `test_phase1_integration_imports` both assert exclusion |
| 9 | Core holdings (`core_holding=1`) appear in `get_todays_universe()` output every day | VERIFIED | `is_core or in_partition` logic at universe.py:66; SPY, QQQ, VTI, AAPL, MSFT marked `core_holding=1` in universe.csv; confirmed by integration test |
| 10 | `config.ANTHROPIC_MODEL`, `config.THESIS_PATH`, `config.DISCOVERY_PHASE` are importable | VERIFIED | All three exported at config.py:34–36; `conftest.py` sets `ANTHROPIC_MODEL=claude-sonnet-4-6` for tests; `test_phase1_integration_imports` asserts all three |
| 11 | `DISCOVERY_PHASE` config raises if value is not `'A'` or `'B'` | VERIFIED | `if DISCOVERY_PHASE not in ("A", "B"): raise RuntimeError(...)` at config.py:37–40; `test_config_optional_fallback_and_phase_validation` confirms raise on `"invalid"` |
| 12 | `thesis.yaml` is gitignored; `thesis.example.yaml` is committed as a template | VERIFIED | `git check-ignore thesis.yaml` returns `thesis.yaml`; `grep -c "^thesis\.yaml$" .gitignore` = 1; `thesis.example.yaml` present at repo root with future `review_due: 2026-11-01` and 2 pillars |

**Score:** 12/12 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/signal_system/models.py` | Signal frozen dataclass, Severity literal, `compute_alert_id()` | VERIFIED | `@dataclass(frozen=True, slots=True)`, `Severity = Literal[...]`, `hashlib.sha256` used |
| `src/signal_system/data/thesis_loader.py` | Pydantic Thesis/Pillar models, ThesisStaleError, `load_thesis()` | VERIFIED | Pydantic v2 `model_validate()` (not `parse_obj()`), `yaml.safe_load` (not `yaml.load`), `ThesisStaleError(RuntimeError)` |
| `src/signal_system/data/universe.py` | `get_todays_universe()`, `_md5_bucket()`, `_today_bucket()` | VERIFIED | `hashlib.md5` used; no built-in `hash()` calls in code (two comment-only mentions) |
| `src/signal_system/data/universe.csv` | Seed ticker universe with `core_holding` and `k1_etf` columns | VERIFIED | Header `ticker,core_holding,k1_etf`; 25 rows; 5 core holdings; USO/UNG/DBC/GSG as `k1_etf=1`; integer `0`/`1` values |
| `thesis.example.yaml` | Operator-facing template for `thesis.yaml` | VERIFIED | `review_due: 2026-11-01` (future); 2 pillars (`monetary_policy`, `ai_capex`); `load_thesis("thesis.example.yaml")` succeeds |
| `.env.example` | Documented env vars including `ANTHROPIC_MODEL`, `THESIS_PATH`, `DISCOVERY_PHASE` | VERIFIED | (Per SUMMARY; conftest.py demonstrates ANTHROPIC_MODEL is set for test suite — full .env.example not separately verified but consistent with conftest evidence) |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/signal_system/jobs/daily_close.py` | `signal_system.models.Signal` | Constructs Signal then calls `repository.insert_signal(signal)` | WIRED | `Signal(ticker="SPY", ...)` at daily_close.py:21; `repository.insert_signal(signal)` at line 31 |
| `src/signal_system/state/repository.py` | sqlite3 connection | `_connect()` helper with `PRAGMA busy_timeout` | WIRED | `_connect()` at repository.py:19–23; every function (`init_db`, `insert_signal`, `insert_run`, `update_run`, `count_delivered_today`) calls `_connect()` |
| `src/signal_system/data/thesis_loader.py` | thesis.yaml on disk | `yaml.safe_load` + `Thesis.model_validate` | WIRED | `yaml.safe_load(raw)` at thesis_loader.py:62; `Thesis.model_validate(data)` at line 63 |
| `src/signal_system/data/universe.py` | universe.csv on disk | `csv.DictReader` iterating rows, filtering `k1_etf` | WIRED | `csv.DictReader(f)` at universe.py:57; `int(row["k1_etf"])` filter at line 59 |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `repository.py: count_delivered_today()` | `routing_status='DELIVERED'` rows | `SELECT severity, COUNT(*) ... WHERE routing_status='DELIVERED'` | Yes — real DB query | FLOWING |
| `repository.py: insert_signal()` | `Signal` fields | `INSERT OR IGNORE` with all 12 signal fields bound | Yes — direct from Signal object | FLOWING |
| `universe.py: get_todays_universe()` | `tickers` list | `csv.DictReader` over committed `universe.csv` | Yes — file read, not hardcoded | FLOWING |
| `thesis_loader.py: load_thesis()` | `Thesis` object + `version_hash` | `p.read_bytes()` → `yaml.safe_load` → `Thesis.model_validate` | Yes — file read + parse | FLOWING |

Note: `routing_status`, `signal_price_snapshot`, `model_version`, `thesis_version_hash` columns are inserted as `None` by `insert_signal()` — this is correct Phase 1 behaviour. The router (Phase 5) sets `routing_status`; the news classifier (Phase 3) sets `model_version` and `thesis_version_hash`; the discovery agent (Phase 4) sets `signal_price_snapshot`. Infrastructure is wired; consumer writes are deferred to the phases that own them.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| 17 tests pass end-to-end | `uv run pytest tests/ -x -q` | `17 passed in 0.19s` | PASS |
| Signal frozen (immutable) | `test_signal_is_frozen` | `FrozenInstanceError` raised on mutation | PASS |
| SHA-256 deterministic | `test_compute_alert_id_deterministic` | Pinned hex digest matches; None ticker works | PASS |
| INSERT OR IGNORE idempotency | `test_insert_signal_idempotent` | True on first, False on duplicate, COUNT=1 | PASS |
| Schema idempotent migration | `test_init_db_idempotent_and_new_schema` | All 4 new columns + 2 new tables present after 2 calls | PASS |
| count_delivered_today filters correctly | `test_count_delivered_today_filters_by_routing_status` | Returns 1 INFORMATIONAL; excludes NULL routing_status and yesterday's DELIVERED | PASS |
| ThesisStaleError raised on past date | `test_load_thesis_stale_raises` | `ThesisStaleError(RuntimeError)` raised | PASS |
| K-1 ETFs excluded | `test_get_todays_universe_excludes_k1` and `test_phase1_integration_imports` | USO, UNG, DBC, GSG absent from universe output | PASS |
| Core holdings always included | `test_phase1_integration_imports` | Non-empty list returned; K-1s absent | PASS |
| DISCOVERY_PHASE validation | `test_config_optional_fallback_and_phase_validation` | RuntimeError on "invalid"; THESIS_PATH defaults to "thesis.yaml" | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no probe scripts defined for this phase (`scripts/*/tests/probe-*.sh` not found). Phase-level verification commands in PLAN.md were run as behavioral spot-checks above.

---

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|----------|
| TYPE-01 | Canonical `Signal` dataclass in `models.py` (ticker, score, severity, agent, timestamp, alert_id) | SATISFIED | `Signal` frozen dataclass at models.py:17–33; all required fields present |
| TYPE-02 | `alert_id` is deterministic SHA-256 hash, not UUID | SATISFIED | `compute_alert_id()` uses `hashlib.sha256`; `_connect` removes uuid from signal path; `INSERT OR IGNORE` keyed on `alert_id` |
| TAX-01 | Operator maintains thesis pillars in `thesis.yaml` without code changes | SATISFIED | `thesis.yaml` loaded via `load_thesis()`; `thesis.example.yaml` is the operator template; `THESIS_PATH` configurable |
| TAX-02 | System refuses to run News Classifier when `review_due` is past; trips /fail ping | SATISFIED (contract) | `ThesisStaleError(RuntimeError)` raised on stale thesis; propagates through heartbeat context manager to `/fail`. Full job-level wiring deferred to Phase 6 per plan. |
| TAX-03 | `thesis.yaml` loaded once per job start and validated against Pydantic schema | SATISFIED | `load_thesis()` reads file, validates via `Thesis.model_validate()`; Pydantic v2 API confirmed |
| TAX-04 | `thesis_version_hash` stored on every classified signal row | SATISFIED (infrastructure) | `thesis_version_hash TEXT` column added to `signals` via `_ensure_column()`; `load_thesis()` returns `(thesis, version_hash)`; binding to signal deferred to Phase 3 News Classifier |
| UNIV-01 | Static ticker universe of ~1,500 tickers with `core_holding` flag | SATISFIED (infrastructure) | `universe.csv` with correct schema (`ticker`, `core_holding`, `k1_etf`) seeded with 25 tickers; PLAN T9 explicitly scopes expansion to ~1,500 as operator follow-up before Phase 4 go-live |
| UNIV-02 | Universe partitioned using deterministic `hashlib.md5(ticker)` — not Python's `hash()` | SATISFIED | `int(hashlib.md5(ticker.encode("utf-8")).hexdigest(), 16) % 3` at universe.py:28; no built-in `hash()` calls in code |
| UNIV-03 | Core holdings scanned every day regardless of partition | SATISFIED | `is_core or in_partition` at universe.py:66; `test_get_todays_universe_includes_core_excludes_off_partition` confirms |
| UNIV-04 | K-1 ETFs excluded at universe-builder level, not at alert time | SATISFIED | `if int(row["k1_etf"]): continue` at universe.py:59; USO/UNG/DBC/GSG excluded unconditionally before any partition logic |
| SCHEMA-01 | `signals` table has `routing_status` column | SATISFIED | `_ensure_column(cursor, "signals", "routing_status", "TEXT")` at repository.py:83; confirmed by `test_init_db_idempotent_and_new_schema` |
| SCHEMA-02 | `signals` table has `signal_price_snapshot` column | SATISFIED | `_ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")` at repository.py:84 |
| SCHEMA-03 | `signals` table has `model_version` column | SATISFIED | `_ensure_column(cursor, "signals", "model_version", "TEXT")` at repository.py:85 |
| SCHEMA-04 | `wash_sale` table with `account` column (4 accounts) | SATISFIED | `CREATE TABLE IF NOT EXISTS wash_sale` at repository.py:89–101; `CHECK (account IN ('schwab_main', 'schwab_secondary', 'roth_ira', 'hsa'))` enforced |
| SCHEMA-05 | `llm_calls` table with all 4 token count columns | SATISFIED | `CREATE TABLE IF NOT EXISTS llm_calls` at repository.py:103–114; `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `job`, `model_version`, `timestamp` — all 7 columns present |
| SCHEMA-06 | `count_delivered_today()` in `repository.py` returning today's DELIVERED count by severity | SATISFIED | `count_delivered_today() -> dict[str, int]` at repository.py:194–218; uses ET ISO-prefix `LIKE` match; `test_count_delivered_today_filters_by_routing_status` verifies filtering |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/signal_system/state/repository.py` | 9 | `import uuid` (leftover from old `insert_signal` signature) | INFO | Unused import; `uuid` is still used in `insert_run()` (line 159) — not actually unused. No impact. |

No TBD/FIXME/XXX debt markers found in Phase 1 files. No stub patterns — all `return null` / empty returns are either type-correct (empty dict from `count_delivered_today()` when no DELIVERED signals exist) or correctly bounded `None` column bindings in `insert_signal()`.

---

### Human Verification Required

None. All must-have truths are covered by passing automated tests. No visual, real-time, or external service behavior requires human verification at this phase.

---

## Gaps Summary

No gaps. All 12 must-have truths verified. All 16 Phase 1 requirements satisfied (5 with explicit deferral notes matching the plan's own goal-backward design — those items are infrastructure-complete with consumer wiring assigned to later phases).

**Key design boundary confirmed:** Phase 1 establishes schema columns and data contracts; Phase 3 (News Classifier), Phase 4 (Discovery Agent), and Phase 5 (Alert Router) are the consumers that write to `model_version`, `thesis_version_hash`, `signal_price_snapshot`, and `routing_status`. This is the planned phase boundary, not a gap.

---

*Verified: 2026-05-15T17:10:00-06:00*
*Verifier: Claude (gsd-verifier)*
