---
phase: 01-foundation
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - pyproject.toml
  - src/signal_system/models.py
  - src/signal_system/config.py
  - src/signal_system/state/repository.py
  - src/signal_system/jobs/daily_close.py
  - src/signal_system/data/thesis_loader.py
  - src/signal_system/data/universe.py
  - src/signal_system/data/universe.csv
  - thesis.example.yaml
  - .env.example
  - .gitignore
  - tests/test_smoke.py
  - tests/conftest.py
autonomous: true
requirements:
  - TYPE-01
  - TYPE-02
  - TAX-01
  - TAX-02
  - TAX-03
  - TAX-04
  - UNIV-01
  - UNIV-02
  - UNIV-03
  - UNIV-04
  - SCHEMA-01
  - SCHEMA-02
  - SCHEMA-03
  - SCHEMA-04
  - SCHEMA-05
  - SCHEMA-06

must_haves:
  truths:
    - "A Signal dataclass is importable from signal_system.models and is immutable (frozen)"
    - "compute_alert_id() returns a SHA-256 hex digest deterministic across processes"
    - "Inserting the same Signal twice produces exactly one row in the signals table (INSERT OR IGNORE)"
    - "init_db() is idempotent on an upgraded DB — adds new columns/tables without dropping existing rows"
    - "Every sqlite3 connection in repository.py has PRAGMA busy_timeout = 30000 applied"
    - "load_thesis() raises ThesisStaleError (subclass of RuntimeError) when review_due is in the past"
    - "get_todays_universe() returns deterministic partition for a given ticker on a given ET day"
    - "K-1 ETFs (k1_etf=1 rows) never appear in get_todays_universe() output"
    - "Core holdings (core_holding=1) appear in get_todays_universe() output every day"
    - "config.ANTHROPIC_MODEL, config.THESIS_PATH, config.DISCOVERY_PHASE are importable"
    - "DISCOVERY_PHASE config raises if value is not 'A' or 'B'"
    - "thesis.yaml is gitignored; thesis.example.yaml is committed as a template"
  artifacts:
    - path: "src/signal_system/models.py"
      provides: "Signal frozen dataclass, Severity literal, compute_alert_id() helper"
      contains: "@dataclass(frozen=True"
    - path: "src/signal_system/data/thesis_loader.py"
      provides: "Pydantic Thesis/Pillar models, ThesisStaleError, load_thesis()"
      contains: "class ThesisStaleError"
    - path: "src/signal_system/data/universe.py"
      provides: "get_todays_universe(), _md5_bucket(), _today_bucket()"
      contains: "hashlib.md5"
    - path: "src/signal_system/data/universe.csv"
      provides: "Seed ticker universe with core_holding and k1_etf columns"
      contains: "ticker,core_holding,k1_etf"
    - path: "thesis.example.yaml"
      provides: "Operator-facing template for thesis.yaml"
      contains: "review_due"
    - path: ".env.example"
      provides: "Documented env vars including ANTHROPIC_MODEL, THESIS_PATH, DISCOVERY_PHASE"
      contains: "ANTHROPIC_MODEL"
  key_links:
    - from: "src/signal_system/jobs/daily_close.py"
      to: "src/signal_system/models.Signal"
      via: "constructs Signal then calls repository.insert_signal(signal)"
      pattern: "Signal\\("
    - from: "src/signal_system/state/repository.py"
      to: "sqlite3 connection"
      via: "_connect() helper with PRAGMA busy_timeout"
      pattern: "PRAGMA busy_timeout"
    - from: "src/signal_system/data/thesis_loader.py"
      to: "thesis.yaml on disk"
      via: "yaml.safe_load + Thesis.model_validate"
      pattern: "yaml\\.safe_load"
    - from: "src/signal_system/data/universe.py"
      to: "universe.csv on disk"
      via: "csv.DictReader iterating rows, filtering k1_etf"
      pattern: "csv\\.DictReader"
---

<objective>
Establish the foundation layer that every subsequent phase depends on: the canonical `Signal` dataclass, deterministic SHA-256 `alert_id`, SQLite schema extensions (idempotent migration), the operator-maintained `thesis.yaml` taxonomy with a `review_due` gate, and the static ticker universe with deterministic md5 partitioning and K-1 exclusion.

Purpose: Lock the shared contract and schema BEFORE any agent or router code is written, so Phases 2-6 can build against a stable interface without rework.

Output:
- New module `src/signal_system/models.py` (Signal + compute_alert_id)
- Extended `src/signal_system/config.py` (3 new env vars)
- Extended `src/signal_system/state/repository.py` (new columns, new tables, `_connect()`, `_ensure_column()`, `count_delivered_today()`, `insert_signal(Signal)`)
- New modules `src/signal_system/data/thesis_loader.py` and `src/signal_system/data/universe.py`
- Seed `src/signal_system/data/universe.csv` and root `thesis.example.yaml`
- Updated `.env.example`, `.gitignore`, `tests/conftest.py`
- New tests in `tests/test_smoke.py`
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/REQUIREMENTS.md
@.planning/research/SUMMARY.md
@.planning/research/PITFALLS.md
@.planning/phases/01-foundation/01-CONTEXT.md
@.planning/phases/01-foundation/01-RESEARCH.md
@src/signal_system/state/repository.py
@src/signal_system/config.py
@src/signal_system/jobs/daily_close.py
@tests/test_smoke.py
@tests/conftest.py
@pyproject.toml

<interfaces>
<!-- Key contracts produced by this plan that downstream phases (2-6) consume. -->
<!-- Executor: implement these signatures exactly. -->

# src/signal_system/models.py (NEW)
Severity = Literal["ACTION_REQUIRED", "INFORMATIONAL", "MONITORING"]

@dataclass(frozen=True, slots=True)
class Signal:
    ticker: str | None
    score: float | None
    severity: Severity
    agent: str
    timestamp: datetime
    alert_id: str
    title: str
    body: str | None = None
    sub_scores: dict[str, float] = field(default_factory=dict)

def compute_alert_id(ticker: str | None, date_iso: str, rule: str, agent: str) -> str: ...

# src/signal_system/state/repository.py (EXTENDED)
def _connect() -> sqlite3.Connection: ...
def _ensure_column(cursor, table: str, column: str, type_def: str) -> None: ...
def init_db() -> None: ...                         # idempotent
def insert_signal(signal: Signal) -> bool: ...     # NEW SIGNATURE — returns True if inserted
def insert_run(job: str) -> str: ...               # unchanged
def update_run(run_id: str, status: str) -> None:  # unchanged
def count_delivered_today() -> dict[str, int]: ... # NEW

# src/signal_system/data/thesis_loader.py (NEW)
class ThesisStaleError(RuntimeError): ...
class Pillar(BaseModel):
    name: str
    description: str
    keywords: list[str]
class Thesis(BaseModel):
    review_due: date
    pillars: list[Pillar]
def load_thesis(path: Path | str) -> tuple[Thesis, str]: ...   # returns (thesis, version_hash)

# src/signal_system/data/universe.py (NEW)
def get_todays_universe() -> list[str]: ...

# src/signal_system/config.py (EXTENDED)
def _optional(name: str, default: str) -> str: ...
ANTHROPIC_MODEL: str                # required, e.g. "claude-sonnet-4-6"
THESIS_PATH: str                    # optional, default "thesis.yaml"
DISCOVERY_PHASE: str                # optional, default "A", must be "A" or "B"
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>T1: Verify Pydantic v2 dependency present + add tenacity placeholder note</name>
  <files>pyproject.toml</files>
  <behavior>
    - `pydantic` already appears in `[project].dependencies` — must be present (no version change required since Pydantic releases since 2.0 satisfy a bare `pydantic` requirement; v1 is yanked from PyPI for new installs).
    - Pin to `pydantic>=2.0` explicitly to be defensive against future v1 backports.
    - `uv sync` runs without error after the edit.
  </behavior>
  <action>
    Edit `pyproject.toml` `[project].dependencies`: change the bare `"pydantic"` entry to `"pydantic>=2.0"` (per D-12, RESEARCH §D-11–D-14, pitfall #3). Leave all other dependencies untouched. Do NOT add `tenacity` in this phase — RESEARCH/SUMMARY notes it for Phase 2.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv sync &amp;&amp; uv run python -c "import pydantic; assert pydantic.VERSION.startswith('2.'), f'Expected pydantic 2.x, got {pydantic.VERSION}'"</automated>
  </verify>
  <done>`pyproject.toml` lists `pydantic>=2.0`; `uv sync` succeeds; `pydantic.VERSION` starts with `2.`.</done>
</task>

<task type="auto" tdd="true">
  <name>T2: Create signal_system/models.py — Signal frozen dataclass + compute_alert_id</name>
  <files>src/signal_system/models.py, tests/test_smoke.py</files>
  <behavior>
    - `Severity` is a `Literal["ACTION_REQUIRED", "INFORMATIONAL", "MONITORING"]` exported at module top level (per D-04).
    - `Signal` is a frozen, slotted dataclass with fields exactly: `ticker: str | None`, `score: float | None`, `severity: Severity`, `agent: str`, `timestamp: datetime`, `alert_id: str`, `title: str`, `body: str | None = None`, `sub_scores: dict[str, float] = field(default_factory=dict)` (per D-01, D-03).
    - Attempting to mutate any field (e.g. `signal.score = 0.5`) raises `FrozenInstanceError`.
    - `routing_status` is NOT a field on Signal (per D-01).
    - `compute_alert_id(ticker, date_iso, rule, agent)` returns the hex SHA-256 of `f"{ticker or '_'}:{date_iso}:{rule}:{agent}"` (per D-02).
    - Calling `compute_alert_id` twice with identical args returns identical strings; changing any arg changes the digest.
    - `compute_alert_id(None, "2026-05-15", "rule_x", "news")` does not raise — `None` ticker normalizes to `_`.
  </behavior>
  <action>
    Create `src/signal_system/models.py` per RESEARCH §D-01–D-04. Use stdlib only (`dataclasses`, `datetime`, `typing.Literal`, `hashlib`). Do NOT import Pydantic here. Use `from __future__ import annotations` so type hints don't evaluate eagerly. Implement `compute_alert_id` as `hashlib.sha256(f"{ticker or '_'}:{date_iso}:{rule}:{agent}".encode("utf-8")).hexdigest()`.

    Add TWO tests to `tests/test_smoke.py`:
    1. `test_signal_is_frozen` — construct a Signal, assert `pytest.raises(dataclasses.FrozenInstanceError)` when assigning to `.score`.
    2. `test_compute_alert_id_deterministic` — assert `compute_alert_id("AAPL","2026-05-15","r","news")` equals itself, equals a precomputed expected hex string (compute once and pin it), and differs when any argument changes; also assert `compute_alert_id(None, ...)` works.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py::test_signal_is_frozen tests/test_smoke.py::test_compute_alert_id_deterministic -x</automated>
  </verify>
  <done>`src/signal_system/models.py` exists; both new tests pass; `from signal_system.models import Signal, Severity, compute_alert_id` succeeds.</done>
</task>

<task type="auto" tdd="true">
  <name>T3: Extend config.py — _optional() + ANTHROPIC_MODEL/THESIS_PATH/DISCOVERY_PHASE + .env.example + conftest</name>
  <files>src/signal_system/config.py, .env.example, tests/conftest.py</files>
  <behavior>
    - `config._optional(name, default)` returns `os.environ[name].strip()` if set and non-empty, else `default` (per D-18).
    - `config.ANTHROPIC_MODEL` is required via `_require()` (per D-19).
    - `config.THESIS_PATH` defaults to `"thesis.yaml"` (per D-20).
    - `config.DISCOVERY_PHASE` defaults to `"A"`; importing `config` with `DISCOVERY_PHASE=invalid` raises `RuntimeError` (per D-20).
    - `.env.example` documents all three vars (per D-21).
    - `tests/conftest.py` sets `ANTHROPIC_MODEL` to a dummy value so test imports of `config` don't raise.
  </behavior>
  <action>
    1. Edit `src/signal_system/config.py` per RESEARCH §D-18–D-21: add `_optional()` helper (uses pattern `os.environ.get(name, default).strip() or default`); then add three module-level assignments after the existing `_require()` block — `ANTHROPIC_MODEL = _require("ANTHROPIC_MODEL")`, `THESIS_PATH = _optional("THESIS_PATH", "thesis.yaml")`, `DISCOVERY_PHASE = _optional("DISCOVERY_PHASE", "A")`. Follow with `if DISCOVERY_PHASE not in ("A", "B"): raise RuntimeError(...)`.
    2. Edit `.env.example` to add the three new vars with brief comments (per D-21). If `.env.example` does not exist, create it with all existing required vars plus the new ones.
    3. Edit `tests/conftest.py` to add `os.environ.setdefault("ANTHROPIC_MODEL", "claude-sonnet-4-6")` so test imports do not raise on the new required var.

    Add ONE test to `tests/test_smoke.py`: `test_config_optional_fallback_and_phase_validation` — uses `monkeypatch.setenv("DISCOVERY_PHASE", "invalid")`, then `importlib.reload(signal_system.config)` and asserts `RuntimeError` is raised. After cleanup, asserts `signal_system.config.THESIS_PATH == "thesis.yaml"` when env is unset.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py::test_config_optional_fallback_and_phase_validation -x &amp;&amp; uv run python -c "from signal_system import config; assert config.ANTHROPIC_MODEL and config.THESIS_PATH == 'thesis.yaml' and config.DISCOVERY_PHASE in ('A','B')"</automated>
  </verify>
  <done>Config exports the three new vars; invalid DISCOVERY_PHASE raises; .env.example documents them; conftest sets ANTHROPIC_MODEL; the new test passes.</done>
</task>

<task type="auto" tdd="true">
  <name>T4: Refactor repository.py — _connect, _ensure_column, schema extensions, count_delivered_today, INSERT OR IGNORE</name>
  <files>src/signal_system/state/repository.py, tests/test_smoke.py</files>
  <behavior>
    - Every `sqlite3.connect(...)` call in repository.py is replaced by `_connect()` which applies `PRAGMA busy_timeout = 30000` (per D-07, pitfall #4).
    - `_ensure_column(cursor, table, column, type_def)` reads `PRAGMA table_info(table)` and only ALTERs if column is missing (per D-05, RESEARCH pitfall #1 — `ALTER TABLE ADD COLUMN IF NOT EXISTS` is NOT valid SQLite).
    - `init_db()` is idempotent: calling it twice on an upgraded DB does NOT raise and does NOT drop existing data.
    - `init_db()` adds these columns to `signals` (nullable): `routing_status TEXT`, `signal_price_snapshot REAL`, `model_version TEXT`, `thesis_version_hash TEXT` (per D-06).
    - `init_db()` creates `wash_sale` table with columns and CHECK constraint per D-08, and `llm_calls` table per D-09.
    - `insert_signal(signal: Signal) -> bool` accepts a `Signal` object, executes `INSERT OR IGNORE` keyed on `alert_id`, returns `True` if inserted (rowcount == 1), `False` if duplicate (per D-02). Old kwargs-based signature is REPLACED, not preserved.
    - `count_delivered_today() -> dict[str, int]` returns severity-keyed counts for rows where `routing_status = 'DELIVERED'` and `timestamp` LIKEs today's ET ISO date prefix (per D-10).
    - Inserting the same Signal twice produces exactly one row.
    - Existing `insert_run()` / `update_run()` callers must continue working; their signatures don't change.
  </behavior>
  <action>
    Rewrite `src/signal_system/state/repository.py` per RESEARCH §D-05 through §D-10:

    1. Add private helpers at module top: `_connect()` (per RESEARCH "Connection helper") and `_ensure_column()` (per RESEARCH "Idempotent column add"). Both use `DB_PATH` (already module-level).
    2. Replace every `sqlite3.connect(DB_PATH)` in `init_db`, `insert_run`, `update_run` with `_connect()`. Do NOT also re-execute `PRAGMA busy_timeout` in those functions — `_connect()` does it.
    3. Extend `init_db()` to ALSO call `_ensure_column(cursor, "signals", "routing_status", "TEXT")`, `..."signal_price_snapshot", "REAL"`, `..."model_version", "TEXT"`, `..."thesis_version_hash", "TEXT"`. Then create `wash_sale` and `llm_calls` tables (full schemas per CONTEXT D-08, D-09 and RESEARCH §D-05).
    4. Replace `insert_signal()` entirely. New signature: `def insert_signal(signal: Signal) -> bool`. Remove `import uuid` if it becomes unused. Use `INSERT OR IGNORE INTO signals (alert_id, timestamp, agent, severity, ticker, title, body, score, routing_status, signal_price_snapshot, model_version, thesis_version_hash) VALUES (...)` with 12 placeholders. Bind `signal.timestamp.isoformat()` for timestamp. Routing/snapshot/model/thesis columns bind `None`. Return `cursor.rowcount == 1`. Import `Signal` from `signal_system.models` (top of file).
    5. Add `count_delivered_today() -> dict[str, int]` per RESEARCH §D-10. Use ISO-date-prefix `LIKE` match — `timestamp LIKE ? || '%'` with today's ET date.

    Add THREE tests to `tests/test_smoke.py`:
    1. `test_init_db_idempotent_and_new_schema` — call `init_db()` twice; query `PRAGMA table_info(signals)` and assert all 4 new columns present; query `sqlite_master` for `wash_sale` and `llm_calls`.
    2. `test_insert_signal_idempotent` — construct a Signal with a fixed `alert_id`, call `insert_signal(s)` twice, assert first returns `True`, second returns `False`, and `SELECT COUNT(*) FROM signals` is `1`.
    3. `test_count_delivered_today_filters_by_routing_status` — insert one signal directly via SQL with `routing_status='DELIVERED'` and today's ET timestamp; one with `routing_status=NULL`; one DELIVERED but timestamp = yesterday. Assert `count_delivered_today()` returns `{<severity>: 1}` and excludes the other two.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py::test_init_db_idempotent_and_new_schema tests/test_smoke.py::test_insert_signal_idempotent tests/test_smoke.py::test_count_delivered_today_filters_by_routing_status -x</automated>
  </verify>
  <done>All three new tests pass; `grep -n "sqlite3.connect" src/signal_system/state/repository.py | grep -v "_connect" | grep -v "^#"` returns no lines (all connects routed through `_connect()`); `grep -c "busy_timeout" src/signal_system/state/repository.py` ≥ 1.</done>
</task>

<task type="auto" tdd="true">
  <name>T5: Update daily_close.py + existing smoke tests for new insert_signal(Signal) signature</name>
  <files>src/signal_system/jobs/daily_close.py, tests/test_smoke.py</files>
  <behavior>
    - `daily_close.run()` constructs a `Signal` object and calls `repository.insert_signal(signal)`.
    - The existing `test_daily_close_smoke` test continues to pass after the change.
    - The legacy `test_insert_signal_returns_uuid` test must be REPLACED (the old function no longer returns a UUID) — new test validates the new contract: build a Signal, call insert, assert the row exists.
  </behavior>
  <action>
    1. Update `src/signal_system/jobs/daily_close.py`:
       - Import `Signal` and `compute_alert_id` from `signal_system.models`.
       - Inside the `with heartbeat.heartbeat():` block, after `spy_close = ...`, build:
         ```
         now_et = datetime.now(ZoneInfo("America/New_York"))
         alert_id = compute_alert_id("SPY", now_et.date().isoformat(), "daily_close", "DAILY_CLOSE")
         signal = Signal(ticker="SPY", score=spy_close, severity="INFORMATIONAL",
                         agent="DAILY_CLOSE", timestamp=now_et, alert_id=alert_id,
                         title=f"SPY closed at {spy_close:.2f} (S&amp;P 500 proxy)",
                         body="Daily close captured at market close.")
         repository.insert_signal(signal)
         ```
         Use the alert_id local variable for the email body (instead of the previous return value of insert_signal).
       - Add `from datetime import datetime` and `from zoneinfo import ZoneInfo` to imports.

    2. In `tests/test_smoke.py`:
       - DELETE `test_insert_signal_returns_uuid` (it asserts UUID v4 — no longer applicable per D-02).
       - Confirm `test_daily_close_smoke`, `test_daily_close_finnhub_failure`, `test_daily_close_email_failure` still pass without modification (they query by `agent='DAILY_CLOSE'`, not by alert_id format).
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py -x</automated>
  </verify>
  <done>Full `test_smoke.py` test suite passes; daily_close constructs a Signal and uses the new `insert_signal(Signal)` API; `grep -n "insert_signal" src/signal_system/jobs/daily_close.py` shows only the new call shape.</done>
</task>

<task type="auto" tdd="true">
  <name>T6: Create thesis_loader.py — Pydantic Thesis/Pillar + ThesisStaleError + load_thesis</name>
  <files>src/signal_system/data/thesis_loader.py, tests/test_smoke.py</files>
  <behavior>
    - `ThesisStaleError` extends `RuntimeError` (per D-13). It is NOT swallowed by `except Exception` in jobs — the existing `daily_close.py` pattern (catch outside heartbeat) is the contract.
    - `Pillar` and `Thesis` are Pydantic v2 `BaseModel`s with fields per D-11/D-12. `Thesis.review_due` is a `datetime.date`.
    - `load_thesis(path) -> (Thesis, version_hash)` reads bytes from path, computes `sha256` of raw bytes as `version_hash`, parses YAML via `yaml.safe_load` (NEVER `yaml.load` — pitfall #5), validates via `Thesis.model_validate()` (Pydantic v2, NOT `.parse_obj()` — pitfall #3).
    - Raises `ThesisStaleError` when `thesis.review_due < date.today()`.
    - Raises `FileNotFoundError` when path does not exist; raises `pydantic.ValidationError` on schema failure.
  </behavior>
  <action>
    Create `src/signal_system/data/thesis_loader.py` per RESEARCH §D-11–D-14. Use `from __future__ import annotations`. Import `hashlib`, `yaml`, `date` from `datetime`, `Path` from `pathlib`, `BaseModel, ValidationError` from `pydantic`. Implement exactly as shown in RESEARCH.

    Add THREE tests to `tests/test_smoke.py`:
    1. `test_load_thesis_happy_path` — write a future-dated thesis YAML to `tmp_path`, call `load_thesis`, assert `thesis.review_due > date.today()`, len(thesis.pillars) > 0, version_hash is a 64-char hex string.
    2. `test_load_thesis_stale_raises` — write a past-dated thesis YAML, assert `pytest.raises(ThesisStaleError)`. Also assert `issubclass(ThesisStaleError, RuntimeError)`.
    3. `test_load_thesis_invalid_schema_raises_validation_error` — write a YAML missing `pillars`, assert `pytest.raises(ValidationError)` (import from `pydantic`).
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py::test_load_thesis_happy_path tests/test_smoke.py::test_load_thesis_stale_raises tests/test_smoke.py::test_load_thesis_invalid_schema_raises_validation_error -x</automated>
  </verify>
  <done>All three new tests pass; `from signal_system.data.thesis_loader import load_thesis, ThesisStaleError, Thesis, Pillar` succeeds.</done>
</task>

<task type="auto" tdd="false">
  <name>T7: Create thesis.example.yaml + verify .gitignore covers thesis.yaml</name>
  <files>thesis.example.yaml, .gitignore</files>
  <behavior>
    - Repo root contains `thesis.example.yaml` with a future `review_due` (≥ 6 months out from today, 2026-05-15) and at least 2 pillars (per D-11, RESEARCH sample).
    - `.gitignore` already contains `thesis.yaml` (confirmed); leave it alone if present, add if missing.
    - `load_thesis("thesis.example.yaml")` succeeds when the example file is the path — used as a smoke import for new operators.
  </behavior>
  <action>
    1. Create `thesis.example.yaml` at the repo root per RESEARCH "Sample thesis.yaml". Set `review_due: 2026-11-01` (6 months from today, 2026-05-15) so operators don't trip the stale check immediately on copy. Include at least the two pillars from RESEARCH (`monetary_policy`, `ai_capex`).
    2. Verify `.gitignore` contains `thesis.yaml` on its own line. If not, append it. (Current state per `.gitignore` read: already present — verify only.)
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; test -f thesis.example.yaml &amp;&amp; grep -E "^thesis\.yaml$" .gitignore &amp;&amp; uv run python -c "from signal_system.data.thesis_loader import load_thesis; t,h = load_thesis('thesis.example.yaml'); assert len(t.pillars) >= 2 and len(h) == 64"</automated>
  </verify>
  <done>`thesis.example.yaml` exists at repo root with valid future review_due and ≥2 pillars; `.gitignore` ignores `thesis.yaml`; `load_thesis('thesis.example.yaml')` succeeds.</done>
</task>

<task type="auto" tdd="true">
  <name>T8: Create universe.py — md5 partitioning + K-1 exclusion + get_todays_universe</name>
  <files>src/signal_system/data/universe.py, tests/test_smoke.py</files>
  <behavior>
    - `_md5_bucket(ticker)` returns `int(hashlib.md5(ticker.encode("utf-8")).hexdigest(), 16) % 3` (per D-16, pitfall #2 — `hash()` is salted, MUST use `hashlib`).
    - `_today_bucket()` returns `datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday % 3` (per D-16).
    - `get_todays_universe()` reads `UNIVERSE_PATH` (sibling `universe.csv`), normalizes ticker to uppercase, EXCLUDES any row where `k1_etf == 1` unconditionally (per D-15), INCLUDES core holdings every day (per D-16), INCLUDES non-core tickers only when `_md5_bucket(ticker) == _today_bucket()`.
    - Returns `list[str]` of ticker symbols (uppercase). Order is the CSV input order.
    - Determinism: calling `get_todays_universe()` twice on the same ET day returns the same list.
  </behavior>
  <action>
    Create `src/signal_system/data/universe.py` per RESEARCH §D-15–D-17. Use stdlib `csv.DictReader`. `UNIVERSE_PATH = Path(__file__).parent / "universe.csv"`. Implement `_md5_bucket`, `_today_bucket`, `get_todays_universe` exactly as in RESEARCH.

    Add THREE tests to `tests/test_smoke.py`:
    1. `test_md5_bucket_deterministic` — assert `_md5_bucket("AAPL")` equals itself across two calls AND equals the bucket computed directly from `hashlib.md5("AAPL".encode()).hexdigest()`.
    2. `test_get_todays_universe_excludes_k1` — monkeypatch `UNIVERSE_PATH` to a `tmp_path` CSV containing `AAPL,1,0`, `USO,0,1`, `UNG,0,1`. Call `get_todays_universe()`; assert `"USO"` and `"UNG"` are NOT in the result; `"AAPL"` IS (it's a core holding).
    3. `test_get_todays_universe_includes_core_excludes_off_partition` — monkeypatch CSV with one core ticker (`AAPL,1,0`) and one non-core ticker known to be in a DIFFERENT partition than today. Use `_md5_bucket` to find a ticker whose bucket ≠ today's bucket (compute live in the test from a small probe set like `["FOO","BAR","BAZ","QUX"]`); assert that ticker is excluded while `AAPL` is included.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest tests/test_smoke.py::test_md5_bucket_deterministic tests/test_smoke.py::test_get_todays_universe_excludes_k1 tests/test_smoke.py::test_get_todays_universe_includes_core_excludes_off_partition -x</automated>
  </verify>
  <done>All three new tests pass; `from signal_system.data.universe import get_todays_universe` succeeds; `grep -c "hashlib.md5" src/signal_system/data/universe.py` ≥ 1; `grep -c "hash(" src/signal_system/data/universe.py | grep -v "hashlib"` returns 0 — no use of built-in `hash()`.</done>
</task>

<task type="auto" tdd="false">
  <name>T9: Seed universe.csv with core holdings + K-1 examples (~20-30 tickers)</name>
  <files>src/signal_system/data/universe.csv</files>
  <behavior>
    - `src/signal_system/data/universe.csv` exists with header `ticker,core_holding,k1_etf` (per D-15).
    - Contains at least 20 rows: a handful of core holdings (`core_holding=1`), the four mandatory K-1 ETFs (`USO`, `UNG`, `DBC`, `GSG` — all with `k1_etf=1`, per CLAUDE.md "K-1 exclusion"), and a mix of common large-caps with `core_holding=0, k1_etf=0` (e.g., AAPL, MSFT, GOOGL, NVDA, AMZN, META, TSLA, JPM, V, MA, JNJ, UNH, PG, HD, XOM, CVX, WMT, KO).
    - Values are integers `0`/`1` (NOT Python `True`/`False` — per CONTEXT specifics).
    - Operator will expand to ~1,500 tickers manually post-Phase 1; this is a working seed.
  </behavior>
  <action>
    Create `src/signal_system/data/universe.csv` with header `ticker,core_holding,k1_etf` and the seed roster described in &lt;behavior&gt;. Make ~5 of the names `core_holding=1` (e.g. SPY, QQQ, VTI, plus 2 personal-portfolio picks like AAPL, MSFT). Mark `USO`, `UNG`, `DBC`, `GSG` as `k1_etf=1`. All other tickers `0,0`. Use uppercase tickers. No trailing newline issues.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; test -f src/signal_system/data/universe.csv &amp;&amp; head -1 src/signal_system/data/universe.csv | grep -qx "ticker,core_holding,k1_etf" &amp;&amp; uv run python -c "import csv; rows = list(csv.DictReader(open('src/signal_system/data/universe.csv'))); assert len(rows) >= 20; assert {'USO','UNG','DBC','GSG'}.issubset({r['ticker'] for r in rows if r['k1_etf']=='1'}); assert any(r['core_holding']=='1' for r in rows)"</automated>
  </verify>
  <done>universe.csv exists, header is correct, ≥20 rows, K-1 ETFs marked, ≥1 core holding present.</done>
</task>

<task type="auto" tdd="false">
  <name>T10: Phase-complete integration check — full test suite + import smoke</name>
  <files>tests/test_smoke.py</files>
  <behavior>
    - Full `uv run pytest` is green (all pre-existing + all new tests).
    - All four new public surfaces import cleanly together: `Signal`, `compute_alert_id`, `load_thesis`, `ThesisStaleError`, `get_todays_universe`, `count_delivered_today`, `ANTHROPIC_MODEL`.
    - `repository.init_db()` followed by `get_todays_universe()` returns a non-empty list when run against the committed `universe.csv`.
  </behavior>
  <action>
    Add ONE integration test to `tests/test_smoke.py`: `test_phase1_integration_imports` — imports all public names listed in &lt;behavior&gt; in one go, calls `init_db()` against a `tmp_path` DB, calls `get_todays_universe()` (using the committed `universe.csv`), asserts the result is a non-empty `list[str]` and that none of `{"USO","UNG","DBC","GSG"}` appear.

    Run the full test suite and confirm green. This is the goal-backward check: Phase 1 ROADMAP success criteria 1, 3, 4, 5 are testable end-to-end here (criterion 2 — stale thesis trips /fail — is unit-covered in T6 + the heartbeat propagation contract; full job-level wiring lands in Phase 6).
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent &amp;&amp; uv run pytest -x</automated>
  </verify>
  <done>`uv run pytest` exits 0 with all tests green (≥ 5 original + ≥ 11 new ≈ 16+ tests); `test_phase1_integration_imports` passes.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| operator → thesis.yaml | Operator edits YAML by hand; classifier loads it without further sanitization. |
| filesystem → repository.py | SQLite file may be concurrently opened by Task Scheduler runs (news-morning + discovery + daily-close). |
| package registry → pyproject.toml | `pydantic` install pulled from PyPI; supply-chain risk. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-01-01 | Tampering | thesis.yaml | mitigate | `yaml.safe_load` (never `yaml.load`); Pydantic `model_validate` rejects unexpected types; file is local-only (not network-fetched). |
| T-01-02 | Denial of Service | SQLite concurrent access | mitigate | `PRAGMA busy_timeout = 30000` on every `_connect()`; WAL mode already enabled. |
| T-01-03 | Tampering | Stale thesis silently used | mitigate | `ThesisStaleError` extends `RuntimeError`, not caught by `except Exception` before heartbeat — trips `/fail`. |
| T-01-04 | Information Disclosure | thesis.yaml committed by mistake | mitigate | `.gitignore` includes `thesis.yaml`; only `thesis.example.yaml` is committed. |
| T-01-05 | Tampering | Process-salted `hash()` causes inconsistent partitioning | mitigate | Use `hashlib.md5(ticker.encode()).hexdigest()` exclusively; grep gate in T8 verify ensures `hash()` is not used. |
| T-01-06 | Spoofing | Duplicate signal alert reaches operator inbox | mitigate | `alert_id` SHA-256 deterministic + `INSERT OR IGNORE` semantics in T4. |
| T-01-07 | Elevation of Privilege | K-1 ETFs reach trading attention layer | mitigate | Exclusion at universe-builder level (T8); never passed downstream regardless of agent code. |
| T-01-SC | Tampering | Pydantic package install supply chain | accept | `pydantic>=2.0` is a mainstream, well-vetted package (Tier 1 per any reasonable audit); already in dependencies before Phase 1. No new package introduced. |
</threat_model>

<verification>
## Phase-level Verification

1. `uv run pytest -x` is green.
2. `uv run python -c "from signal_system.models import Signal, compute_alert_id; from signal_system.data.thesis_loader import load_thesis, ThesisStaleError; from signal_system.data.universe import get_todays_universe; from signal_system.state.repository import count_delivered_today, init_db; from signal_system import config; print('ok')"` prints `ok`.
3. `sqlite3 state/signals.db ".schema signals"` (after running `init_db()`) shows columns: `routing_status`, `signal_price_snapshot`, `model_version`, `thesis_version_hash`.
4. `sqlite3 state/signals.db ".schema wash_sale"` shows the `account` CHECK constraint.
5. `sqlite3 state/signals.db ".schema llm_calls"` shows all 7 columns from D-09.
6. `git check-ignore thesis.yaml` returns `thesis.yaml` (confirming gitignored).
7. `grep -E "^thesis\.yaml$" .gitignore` finds the line.
8. `grep -v '^#' src/signal_system/data/universe.py | grep -c "hashlib.md5"` ≥ 1.
9. `grep -v '^#' src/signal_system/state/repository.py | grep -c "busy_timeout"` ≥ 1.
</verification>

<success_criteria>
## Success Criteria — measurable completion

(Mirrors the ROADMAP Phase 1 success criteria, mapped to the Goal-Backward Check below.)

1. `from signal_system.models import Signal` works; constructing the same Signal twice via `repository.insert_signal()` yields exactly one row.
2. `load_thesis(...)` on a past-dated YAML raises `ThesisStaleError` (a `RuntimeError` subclass) — propagates through the heartbeat context manager unchanged.
3. `get_todays_universe()` is deterministic for a given ticker on a given ET day; core holdings appear every day; same-input determinism is unit-tested.
4. `USO`, `UNG`, `DBC`, `GSG` never appear in `get_todays_universe()` output — verified by integration test.
5. `sqlite3 state/signals.db ".schema"` shows `routing_status`, `signal_price_snapshot`, `model_version`, `wash_sale` table with `account` column, `llm_calls` table; `repository.count_delivered_today()` is importable and returns a dict.
</success_criteria>

<goal_backward_check>
## Goal-Backward Check — ROADMAP success criterion → Task(s)

| ROADMAP Criterion | Truth Statement | Satisfied By |
|-------------------|-----------------|--------------|
| 1. Signal alert_id SHA-256, idempotent insert | A Signal exists and double-insert yields one row | T2 (Signal + compute_alert_id), T4 (INSERT OR IGNORE + idempotent test), T5 (caller migrated) |
| 2. Stale thesis aborts job and trips /fail | `ThesisStaleError` propagates through heartbeat | T6 (`ThesisStaleError(RuntimeError)` + tests); contract verified at job level in Phase 6 |
| 3. Universe ~1,500 tickers + deterministic partition + core daily | `get_todays_universe()` deterministic per ET day | T8 (md5 partition + core inclusion tests), T9 (seed CSV with core holdings); operator expands to 1,500 manually |
| 4. K-1 ETFs absent from scanned subset | USO/UNG/DBC/GSG never returned by universe | T8 (K-1 exclusion test), T9 (K-1 rows present in seed with k1_etf=1) |
| 5. Schema shows new columns + new tables + count_delivered_today() | init_db idempotent + new schema | T4 (schema migration + count_delivered_today), T10 (integration smoke) |
| REQ TYPE-01, TYPE-02 | Signal + compute_alert_id contract | T2 |
| REQ TAX-01 thru TAX-04 | thesis.yaml + Pydantic + stale guard + version_hash | T6, T7 |
| REQ UNIV-01 thru UNIV-04 | universe.csv + md5 partition + core + K-1 exclusion | T8, T9 |
| REQ SCHEMA-01 thru SCHEMA-06 | routing_status, signal_price_snapshot, model_version, wash_sale, llm_calls, count_delivered_today | T4 |
</goal_backward_check>

<risks>
## Risks and Mitigations

1. **T4 changes `insert_signal` signature — breaks `daily_close.py` until T5 lands.** Mitigation: T5 is the same wave as T4 and explicitly updates the caller; the executor MUST land T4+T5 in adjacent commits and run `uv run pytest -x` BEFORE pushing. The verify command in T5 runs the full suite to catch any straggler caller.

2. **Pydantic v2 vs v1 syntax differs (pitfall #3).** Mitigation: T1 pins `pydantic>=2.0` defensively; T6 uses v2 API (`model_validate`, not `parse_obj`).

3. **`ALTER TABLE ADD COLUMN IF NOT EXISTS` is not valid SQLite (pitfall #1).** Mitigation: T4 uses `_ensure_column()` with `PRAGMA table_info()` check, NOT raw `ALTER TABLE ... IF NOT EXISTS`. This is the most likely silent failure mode if executor copies the CONTEXT.md wording literally.

4. **`hash()` vs `hashlib.md5()` (pitfall #2).** Mitigation: T8 verify includes a grep gate that fails the task if built-in `hash(` (not preceded by `hashlib`) appears in `universe.py`.

5. **Test environment must have `ANTHROPIC_MODEL` set.** Mitigation: T3 updates `tests/conftest.py` to setdefault the variable BEFORE any signal_system import.

6. **`thesis.example.yaml` is read by smoke test (T7); a future date is required.** Mitigation: T7 uses 2026-11-01 (6 months out from today, 2026-05-15). Operator must replace before that date when copying — documented via the file's own `review_due` field.

7. **`count_delivered_today` LIKE-prefix match assumes timestamps are stored in ET ISO format.** Mitigation: existing `insert_run`/`insert_signal` use `datetime.now(ZoneInfo("America/New_York")).isoformat()` — already true. No change needed but worth noting for Phase 5 router callers.
</risks>

<output>
Create `.planning/phases/01-foundation/01-01-SUMMARY.md` when all 10 tasks are complete and `uv run pytest -x` is green. The summary should record:
- Files created (models.py, thesis_loader.py, universe.py, universe.csv, thesis.example.yaml, .env.example updates)
- Files extended (repository.py, config.py, daily_close.py, .gitignore, conftest.py, test_smoke.py)
- New test count and final `pytest` exit status
- Any deviations from the plan (with rationale)
- Operator follow-up: copy `thesis.example.yaml` → `thesis.yaml` and customize before running news-morning (Phase 3); expand `universe.csv` from seed (~20 rows) to ~1,500 before Phase 4 go-live
</output>
