---
phase: 01-foundation
plan: "01"
subsystem: foundation
tags: [models, repository, config, thesis, universe, schema, sqlite]
dependency_graph:
  requires: []
  provides:
    - signal_system.models.Signal
    - signal_system.models.compute_alert_id
    - signal_system.state.repository._connect
    - signal_system.state.repository.insert_signal(Signal)
    - signal_system.state.repository.count_delivered_today
    - signal_system.state.repository.init_db (extended schema)
    - signal_system.data.thesis_loader.load_thesis
    - signal_system.data.thesis_loader.ThesisStaleError
    - signal_system.data.universe.get_todays_universe
    - signal_system.config.ANTHROPIC_MODEL
    - signal_system.config.THESIS_PATH
    - signal_system.config.DISCOVERY_PHASE
  affects:
    - All subsequent phases (2-6) import from these modules
tech_stack:
  added: [pydantic>=2.0 (pinned), pyyaml (already present)]
  patterns:
    - Frozen dataclass for immutable value objects (Signal)
    - SHA-256 content-hash for deterministic alert_id (not UUID)
    - INSERT OR IGNORE semantics for idempotent signal writes
    - PRAGMA busy_timeout=30000 on every SQLite connection
    - PRAGMA table_info() check before ALTER TABLE (no IF NOT EXISTS in SQLite)
    - hashlib.md5 for cross-process stable universe partitioning
    - yaml.safe_load only (never yaml.load) for YAML parsing
    - Pydantic v2 model_validate() (not parse_obj()) for thesis validation
key_files:
  created:
    - src/signal_system/models.py
    - src/signal_system/data/thesis_loader.py
    - src/signal_system/data/universe.py
    - src/signal_system/data/universe.csv
    - thesis.example.yaml
  modified:
    - pyproject.toml (pydantic>=2.0)
    - src/signal_system/config.py (_optional, ANTHROPIC_MODEL, THESIS_PATH, DISCOVERY_PHASE)
    - src/signal_system/state/repository.py (full refactor)
    - src/signal_system/jobs/daily_close.py (Signal API migration)
    - .env.example (three new vars documented)
    - tests/conftest.py (ANTHROPIC_MODEL setdefault)
    - tests/test_smoke.py (13 new tests, 1 legacy test removed)
decisions:
  - "Signal uses frozen dataclass (not Pydantic) — no untrusted input, simpler, faster"
  - "alert_id is SHA-256 of ticker:date:rule:agent — deterministic across reruns, enables INSERT OR IGNORE"
  - "_ensure_column uses PRAGMA table_info check — SQLite has no IF NOT EXISTS for ALTER TABLE"
  - "hashlib.md5 for universe partitioning — built-in hash() is process-salted (T-01-05)"
  - "ThesisStaleError extends RuntimeError — propagates through heartbeat context manager to /fail"
  - "yaml.safe_load only — yaml.load allows arbitrary Python object construction (T-01-01)"
metrics:
  duration: "~2.5 hours"
  completed_date: "2026-05-15"
  tasks_completed: 10
  tasks_total: 10
  tests_added: 13
  tests_removed: 1
  tests_final_count: 17
  files_created: 5
  files_modified: 8
---

# Phase 01 Plan 01: Foundation Summary

**One-liner:** Frozen Signal dataclass with SHA-256 alert_id, idempotent SQLite schema migration via PRAGMA table_info, Pydantic v2 thesis taxonomy with stale-guard, and hashlib.md5 universe partitioning with K-1 exclusion.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| T1 | Pin pydantic>=2.0 | c8c1b5c | pyproject.toml, uv.lock |
| T2 (RED) | Signal + compute_alert_id tests | 7720c42 | tests/test_smoke.py |
| T2 (GREEN) | Signal + compute_alert_id impl | b627480 | src/signal_system/models.py |
| T3 (RED) | Config validation test | 12ad72c | tests/test_smoke.py |
| T3 (GREEN) | Config extensions + conftest + .env.example | 62380da | config.py, conftest.py, .env.example |
| T4 (RED) | Repository refactor tests | 94e0cc3 | tests/test_smoke.py |
| T4 (GREEN) | Repository refactor impl | f31b7dc | src/signal_system/state/repository.py |
| T5 | daily_close Signal migration | 2c7a007 | jobs/daily_close.py, tests/test_smoke.py |
| T6 (RED) | thesis_loader tests | eecd003 | tests/test_smoke.py |
| T6 (GREEN) | thesis_loader impl | f2644fe | src/signal_system/data/thesis_loader.py |
| T7 | thesis.example.yaml + .gitignore verify | ca9cdbd | thesis.example.yaml |
| T8 (RED) | universe.py tests | 55f359a | tests/test_smoke.py |
| T8 (GREEN) | universe.py impl | 90cd3f6 | src/signal_system/data/universe.py |
| T9 | Seed universe.csv | dabbf8b | src/signal_system/data/universe.csv |
| T10 | Phase integration test + full suite green | 54e741c | tests/test_smoke.py |

## Verification Results

All phase-level verification checks passed:

1. `uv run pytest -x` — **17 tests, 0 failures**
2. All public surfaces importable (`Signal`, `compute_alert_id`, `load_thesis`, `ThesisStaleError`, `get_todays_universe`, `count_delivered_today`, `init_db`, `config.ANTHROPIC_MODEL`)
3. SQLite schema after `init_db()` shows `routing_status`, `signal_price_snapshot`, `model_version`, `thesis_version_hash` on signals table
4. `wash_sale` table with CHECK constraint (`schwab_main`, `schwab_secondary`, `roth_ira`, `hsa`)
5. `llm_calls` table with 7 columns for token telemetry
6. `git check-ignore thesis.yaml` returns `thesis.yaml` — confirmed gitignored
7. `grep -c "busy_timeout" repository.py` returns 3 (one in `_connect()` impl, one in docstring)
8. `grep -c "hashlib.md5" universe.py` returns 3 — no built-in `hash()` used in code

## Deviations from Plan

### Auto-fixed Issues

None — plan executed exactly as written.

### Observations

**T1 TDD framing:** The plan marks T1 as `tdd="true"` but the verification is a CLI import check, not a pytest test. Used a single `chore` commit (no separate RED/GREEN) as the plan's verification command is `uv sync && python -c "..."`. Documented as minor framing deviation — no behavioral impact.

**T4 grep gate on `sqlite3.connect`:** The plan's gate `grep -n "sqlite3.connect" ... | grep -v "_connect"` returns line 21 (the `sqlite3.connect` call inside `_connect()` itself). This is expected — the gate is designed to catch callers that bypass `_connect()`, and the only occurrence IS `_connect()`'s own implementation. All four original bypass callers (`init_db`, old `insert_signal`, `insert_run`, `update_run`) are correctly routed through `_connect()`.

**T8 hashlib.md5 docstring:** The plan's grep gate for `hash(` (excluding `hashlib`) returns one hit — inside a docstring explaining why `hash()` is not used. No actual code use of built-in `hash()` exists in `universe.py`.

## Known Stubs

None — all data flows are wired. `count_delivered_today()` returns an empty dict when no DELIVERED signals exist (router not yet implemented — Phase 5); this is correct behavior, not a stub.

## Threat Flags

No new threat surface beyond the plan's threat model. All T-01-01 through T-01-07 mitigations implemented:
- T-01-01: `yaml.safe_load` in `thesis_loader.py`
- T-01-02: `PRAGMA busy_timeout=30000` in `_connect()`
- T-01-03: `ThesisStaleError(RuntimeError)` propagates through heartbeat
- T-01-04: `thesis.yaml` in `.gitignore`, `thesis.example.yaml` committed
- T-01-05: `hashlib.md5` in `universe.py`, no `hash()` usage
- T-01-06: SHA-256 `alert_id` + `INSERT OR IGNORE` in `repository.py`
- T-01-07: K-1 exclusion at universe-builder level in `universe.py`

## Operator Follow-up Required

1. **Before running `news-morning` (Phase 3):** Copy `thesis.example.yaml` → `thesis.yaml` at repo root and customize pillars + keywords. Set `review_due` to a future date.
2. **Before Phase 4 go-live:** Expand `universe.csv` from seed (~25 rows) to ~1,500 tickers. Add `core_holding=1` for personal portfolio positions. Verify K-1 ETFs and commodity futures ETFs are marked `k1_etf=1`.
3. **Set `ANTHROPIC_MODEL` in `.env`:** Use current Sonnet model ID (e.g., `claude-sonnet-4-6`).

## Self-Check: PASSED

All created files verified present. All 15 task commits found in git log. Final `uv run pytest -x` exits 0 with 17 tests passing.
