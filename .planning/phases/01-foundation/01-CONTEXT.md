# Phase 1: Foundation - Context

**Gathered:** 2026-05-15
**Status:** Ready for planning

<domain>
## Phase Boundary

Establish the shared infrastructure every subsequent phase depends on: the canonical `Signal` dataclass in `models.py`, the operator-maintained `thesis.yaml` taxonomy with its `review_due` gate, the static ticker universe with deterministic rotation partitioning, all SQLite schema extensions (new columns + new tables), and config additions for new environment variables. No agent logic, no classification, no routing — foundation only.

</domain>

<decisions>
## Implementation Decisions

### Signal Dataclass (models.py)

- **D-01:** `Signal` is a lean dataclass — fields produced by agents only: `ticker`, `score`, `severity`, `agent`, `timestamp`, `alert_id`, `title`, `body`, and an optional `sub_scores: dict[str, float]` for per-pillar or per-factor scores. `routing_status` is NOT a field on Signal — it is written to the DB by the router and never appears on the Signal object.
- **D-02:** `alert_id` is a deterministic SHA-256 content-hash of `f"{ticker}:{date}:{rule}:{agent}"` — NOT a UUID. `INSERT OR IGNORE` on the `signals` table ensures idempotent reruns. The existing `uuid.uuid4()` in `repository.insert_signal()` must be replaced.
- **D-03:** `Signal` is a frozen dataclass (or Pydantic BaseModel with `frozen=True`) — immutable after construction. Agents produce signals; nothing mutates them.
- **D-04:** Severity values are constrained to: `ACTION_REQUIRED`, `INFORMATIONAL`, `MONITORING` — represented as a `Literal` type or `StrEnum`.

### Schema Migration

- **D-05:** Use `ALTER TABLE signals ADD COLUMN IF NOT EXISTS` for new columns on the existing `signals` table — preserves existing rows. New tables (`wash_sale`, `llm_calls`) use `CREATE TABLE IF NOT EXISTS`. `init_db()` remains idempotent and safe to call on upgrade.
- **D-06:** New columns to add to `signals`: `routing_status TEXT`, `signal_price_snapshot REAL`, `model_version TEXT`, `thesis_version_hash TEXT`. These are all nullable — existing rows remain valid.
- **D-07:** Add `PRAGMA busy_timeout = 30000` on every connection open in `repository.py` (before any query). This prevents "database is locked" errors on overlapping Task Scheduler runs.
- **D-08:** `wash_sale` table columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `ticker TEXT NOT NULL`, `account TEXT NOT NULL` (values: schwab_main, schwab_secondary, roth_ira, hsa), `trade_date TEXT NOT NULL`, `quantity REAL`, `cost_basis REAL`, `notes TEXT`, `created_at TEXT NOT NULL`.
- **D-09:** `llm_calls` table columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `job TEXT NOT NULL`, `model_version TEXT NOT NULL`, `input_tokens INTEGER`, `output_tokens INTEGER`, `cache_read_input_tokens INTEGER`, `cache_creation_input_tokens INTEGER`, `timestamp TEXT NOT NULL`.
- **D-10:** `repository.count_delivered_today()` queries `signals` for rows where `routing_status = 'DELIVERED'` and `timestamp` falls within today's `America/New_York` date window (midnight-to-midnight ET). Returns a dict: `{"ACTION_REQUIRED": N, "INFORMATIONAL": N}`.

### thesis.yaml Structure

- **D-11:** Minimal YAML schema — top-level `review_due` (ISO date string, e.g., `2026-08-01`) and a `pillars` list. Each pillar: `name` (str), `description` (str), `keywords` (list of str). No per-pillar weights in the YAML — the classifier handles weighting.
- **D-12:** Pydantic schema for validation on load:
  ```python
  class Pillar(BaseModel):
      name: str
      description: str
      keywords: list[str]

  class Thesis(BaseModel):
      review_due: date
      pillars: list[Pillar]
  ```
- **D-13:** `thesis_loader.py` lives at `src/signal_system/data/thesis_loader.py`. It loads and validates thesis.yaml once, raises `ThesisStaleError` (subclass of `RuntimeError`) when `review_due` is past — this exception propagates through the heartbeat context manager to trip `/fail`. `ThesisStaleError` must NOT be caught by generic `except Exception` handlers in jobs.
- **D-14:** `THESIS_PATH` config var defaults to `"thesis.yaml"` at repo root if not set. Absolute path resolution preferred.

### Ticker Universe

- **D-15:** Universe lives at `src/signal_system/data/universe.csv` with columns: `ticker` (str), `core_holding` (0/1 int), `k1_etf` (0/1 int). Operator maintains this file directly. K-1 ETFs have `k1_etf=1` and are excluded from all scanned subsets by `universe.py` at load time — never passed to agents.
- **D-16:** Rotation partitioning: `int(hashlib.md5(ticker.encode()).hexdigest(), 16) % 3`. Tickers where result equals `datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday % 3` are in today's partition. Core holdings (`core_holding=1`) are always included regardless of partition. K-1 ETFs are excluded before partitioning.
- **D-17:** `universe.py` lives at `src/signal_system/data/universe.py` and exposes a `get_todays_universe() -> list[str]` function — returns today's tickers (core holdings ∪ today's rotation partition, K-1 excluded).

### Config Additions

- **D-18:** Add `_optional(name: str, default: str) -> str` helper to `config.py` for vars with reasonable defaults.
- **D-19:** New required config vars: `ANTHROPIC_MODEL` (e.g., `claude-sonnet-4-6`) — pinned model ID, no aliases.
- **D-20:** New optional config vars with defaults: `THESIS_PATH` (default: `"thesis.yaml"`), `DISCOVERY_PHASE` (default: `"A"`, values: `"A"` or `"B"`).
- **D-21:** Add `.env.example` entries for all new vars so the operator knows what to set.

### Claude's Discretion

- Where to locate `models.py`: `src/signal_system/models.py` at the package root (not inside a subdirectory) — imported by all subpackages without circular deps.
- Whether to use `dataclasses.dataclass(frozen=True)` or Pydantic `BaseModel` for Signal: prefer `dataclasses.dataclass(frozen=True)` to avoid adding Pydantic as a dependency in Phase 1. Pydantic is needed for thesis.yaml validation — it can be added as a single dependency in this phase, making it available for Signal too if desired.
- Connection management: the existing connection-per-operation pattern (open → use → close in try/finally) is fine for Phase 1. A connection pool is out of scope.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Requirements
- `.planning/PROJECT.md` — project context, constraints, key decisions
- `.planning/REQUIREMENTS.md` — TYPE-01/02, TAX-01–04, UNIV-01–04, SCHEMA-01–06 (16 requirements for this phase)
- `.planning/research/SUMMARY.md` — build order, pitfalls, stack recommendations

### Existing Code (integration points)
- `src/signal_system/state/repository.py` — existing schema, `insert_signal()` uses UUID (must change to SHA-256), connection pattern to follow
- `src/signal_system/config.py` — existing `_require()` pattern for env vars
- `src/signal_system/jobs/daily_close.py` — job orchestration pattern (heartbeat wraps everything)

### Research Findings
- `.planning/research/STACK.md` — dependency delta (only `tenacity` to add; `pydantic` for thesis validation)
- `.planning/research/PITFALLS.md` — Pitfall #4 (hashlib.md5 vs hash()), Pitfall #7 (thesis.yaml load once), Pitfall #11 (SQLite WAL / busy_timeout)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `config._require(name)`: pattern to follow for new required env vars; extend with `_optional(name, default)` for THESIS_PATH and DISCOVERY_PHASE
- `repository.init_db()`: idempotent DB init — extend with `ALTER TABLE ADD COLUMN IF NOT EXISTS` and new table `CREATE TABLE IF NOT EXISTS` calls
- `repository.insert_signal()`: must be updated to accept a `Signal` object (or keep kwargs and compute SHA-256 alert_id internally)

### Established Patterns
- Connection-per-operation: open sqlite3.connect → execute → commit → close in try/finally. No connection pooling. Add `PRAGMA busy_timeout = 30000` immediately after connect.
- `ZoneInfo("America/New_York")` for all timestamps — already established in repository.py
- All DB access goes through `repository.py` — no raw SQL anywhere else

### Integration Points
- `models.py` at `src/signal_system/models.py` — imported by agents (phases 3+4), router (phase 5), jobs (phase 6)
- `thesis_loader.py` at `src/signal_system/data/thesis_loader.py` — imported by News Classifier (phase 3)
- `universe.py` at `src/signal_system/data/universe.py` — imported by Discovery Agent (phase 4)
- `repository.count_delivered_today()` — imported by Alert Router (phase 5)
- `config.ANTHROPIC_MODEL`, `config.THESIS_PATH`, `config.DISCOVERY_PHASE` — imported by phases 3, 4

</code_context>

<specifics>
## Specific Ideas

- thesis.yaml lives at repo root by default (`thesis.yaml`), configurable via `THESIS_PATH` env var. It is gitignored (contains operator's investment thesis) — add to `.gitignore`.
- universe.csv uses simple 0/1 integers for boolean columns (not Python `True`/`False`) — compatible with plain CSV readers.
- `ThesisStaleError` must propagate through the heartbeat context manager — jobs must NOT catch it in a bare `except Exception` block before heartbeat sees it. The existing `daily_close.py` pattern is correct: the `except Exception` block is OUTSIDE the heartbeat `with` block.

</specifics>

<deferred>
## Deferred Ideas

- SQLite WAL checkpoint + weekly VACUUM cron (OPS-V2-01) — deferred to post-go-live hardening (v2)
- Weekly backup script — deferred to v2
- Connection pool / context manager pattern for DB connections — out of scope for Phase 1

</deferred>

---

*Phase: 1-Foundation*
*Context gathered: 2026-05-15*
