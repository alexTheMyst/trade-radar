# Phase 5: Alert Router — Research

**Researched:** 2026-05-16
**Domain:** Pure-function signal routing, SQLite budget tracking, Python package structure
**Confidence:** HIGH — all key questions answered by direct codebase inspection

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Daily caps are hard-coded constants: `ACTION_REQUIRED`: max **1**, `INFORMATIONAL`: max **3**, `MONITORING`: unlimited (agents insert directly, router never handles them)
- **D-02:** Budget applies **both agents combined** per day. No per-agent budget.
- **D-03:** Competition is **intra-batch only** within a single `route_signals()` call.
- **D-04:** Allocation order: severity-first, score-ranked descending, alphabetical ticker tiebreak.
- **D-05:** Tiebreak is deterministic: descending score, then ascending ticker alphabetical.
- **D-06:** **No eviction.** Once DELIVERED, never retroactively changed. First-come-first-served across job runs.
- **D-07:** Router reads `repository.count_delivered_today()` at start of each call. No in-memory state.
- **D-08:** Budget resets at `America/New_York` midnight via `count_delivered_today()` ET convention.
- **D-09:** Add `demoted_from` column via `_ensure_column(cursor, "signals", "demoted_from", "TEXT")` in `init_db()`.
- **D-10:** Valid `demoted_from` reason codes: `"budget_cap_ar"`, `"budget_cap_info"`, `"outscored"`.
- **D-11:** `demoted_from` set **at insert time** only. No UPDATE path.
- **D-12:** Router at `src/signal_system/router/alert_router.py` with package `__init__.py` exporting `route_signals`.
- **D-13:** Public signature: `def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]`
- **D-14:** Router does **not** call `insert_signal()`, `email_sender`, or any DB write. Pure logic.
- **D-15:** `severity == "MONITORING"` in input → raise `ValueError`.
- **D-16:** MONITORING signals bypass router entirely. Discovery Phase A and parse-failure fallback insert directly.
- **D-17:** Phase 5 delivers `route_signals()` only. Phase 6 does insert + email + digest.
- **D-18:** `insert_signal()` must gain `demoted_from: str | None = None` keyword arg (Wave 0 prerequisite).
- **D-19:** Key test scenarios documented (see Test Strategy section).
- **D-20:** `count_delivered_today()` must be monkeypatched. Tests must not touch `state/signals.db`.

### the agent's Discretion

None specified — all implementation decisions are locked.

### Deferred Ideas (OUT OF SCOPE)

None from this discussion.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ROUT-01 | Alert Router enforces daily hard caps: 1 AR and 3 INFO per day | Budget constants + `count_delivered_today()` subtraction logic |
| ROUT-02 | Higher-scored signal wins slot; loser written with `routing_status=SUPPRESSED` and `demoted_from` reason code | `insert_signal()` extension + sort-and-allocate algorithm |
| ROUT-03 | Router reads `count_delivered_today()` from DB, not in-memory state | Direct DB read at top of `route_signals()` |
| ROUT-04 | Budget reset uses `America/New_York` midnight | `count_delivered_today()` already uses ET timezone — inherited, no re-implementation needed |
| ROUT-05 | Deterministic tiebreaking: alphabetical ticker as secondary sort — reruns produce identical decisions | `sorted(signals, key=lambda s: (-s.score, s.ticker))` pattern, matches Phase 4 `_rank_values()` |
</phase_requirements>

---

## Summary

Phase 5 is a pure-logic package addition: `src/signal_system/router/` containing `alert_router.py`. The router calls `count_delivered_today()` once, then runs a sort-and-allocate algorithm over the input batch, returning `(Signal, routing_status, demoted_from)` tuples — no DB writes. Phase 6 will call `insert_signal()` per tuple.

Two prerequisite changes must land in **Wave 0** before any router logic: (1) `init_db()` must call `_ensure_column(cursor, "signals", "demoted_from", "TEXT")`, and (2) `insert_signal()` must gain a `demoted_from: str | None = None` keyword argument with its SQL INSERT updated to include that column. Both changes are backward-compatible.

The routing algorithm is: read DB counts → subtract from caps to get remaining slots → sort AR signals (score desc, ticker asc) → allocate up to 1 AR slot → sort INFO signals (score desc, ticker asc) → allocate up to 3 INFO slots → any unallocated signal gets SUPPRESSED with a demoted_from code. The `demoted_from` reason distinguishes "budget was already full from DB" (`budget_cap_ar` / `budget_cap_info`) from "beaten by another signal in this same batch" (`outscored`).

**Primary recommendation:** Write Wave 0 (schema + insert_signal patch) first, then the router module, then tests. The test fixture pattern is already established — `monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")` followed by `repository.init_db()`.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Daily budget accounting | DB (SQLite) | Router (reads only) | `count_delivered_today()` is the single source of truth; router never caches |
| Slot competition / ranking | Router (pure Python) | — | Stateless sort-and-allocate; no external I/O |
| Routing decision record | Caller (Phase 6 job) | — | Router returns tuples; Phase 6 calls `insert_signal()` |
| Schema migration (`demoted_from`) | DB layer (`repository.py`) | — | `_ensure_column()` in `init_db()` is the established idempotent migration pattern |

---

## Standard Stack

### Core (no new dependencies — stdlib + existing project packages only)

| Component | Source | Purpose |
|-----------|--------|---------|
| `signal_system.models.Signal` | `src/signal_system/models.py` | Immutable input value object |
| `signal_system.state.repository.count_delivered_today()` | `src/signal_system/state/repository.py` | Single DB read at routing time |
| `signal_system.state.repository.insert_signal()` (extended) | `src/signal_system/state/repository.py` | Gets `demoted_from` arg in Wave 0 |
| `signal_system.state.repository.init_db()` (extended) | `src/signal_system/state/repository.py` | Gains `_ensure_column(..., "demoted_from", "TEXT")` in Wave 0 |
| `zoneinfo.ZoneInfo("America/New_York")` | stdlib | ET timezone — already used throughout codebase |

**No new packages to install.** The router is pure domain logic using only existing project modules and stdlib.

---

## Package Legitimacy Audit

> No external packages are installed in this phase. All code uses stdlib and existing project imports.

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

---

## Architecture Patterns

### System Architecture Diagram

```
Job caller (Phase 6)
    │
    ├─► route_signals(signals: list[Signal])
    │       │
    │       ├─► repository.count_delivered_today()  ──► SQLite signals table
    │       │       returns {"ACTION_REQUIRED": N, "INFORMATIONAL": N}
    │       │
    │       ├─► validate: severity == "MONITORING" → ValueError
    │       │
    │       ├─► compute remaining slots:
    │       │       ar_remaining  = max(0, 1 - counts.get("ACTION_REQUIRED", 0))
    │       │       info_remaining = max(0, 3 - counts.get("INFORMATIONAL", 0))
    │       │
    │       ├─► sort AR signals:  key=(-score, ticker)
    │       │       allocate first ar_remaining → DELIVERED, demoted_from=None
    │       │       rest → SUPPRESSED, demoted_from:
    │       │           "budget_cap_ar" if ar_remaining == 0 at entry
    │       │           "outscored"     if slot existed but taken by better signal
    │       │
    │       ├─► sort INFO signals: key=(-score, ticker)
    │       │       allocate first info_remaining → DELIVERED, demoted_from=None
    │       │       rest → SUPPRESSED, demoted_from:
    │       │           "budget_cap_info" if info_remaining == 0 at entry
    │       │           "outscored"       if slot existed but taken by better signal
    │       │
    │       └─► returns list[tuple[Signal, str, str | None]]
    │               one tuple per input signal, same order as input
    │
    └─► for each (signal, routing_status, demoted_from):
            repository.insert_signal(signal, routing_status=rs, demoted_from=dmf)
            if routing_status == "DELIVERED": email_sender.send_email(signal)
```

### Recommended Project Structure

```
src/signal_system/
├── router/                  # NEW — Phase 5
│   ├── __init__.py          # exports route_signals
│   └── alert_router.py      # route_signals() implementation
├── state/
│   └── repository.py        # MODIFIED — demoted_from column + insert_signal arg
├── discovery/
│   ├── __init__.py          # template: from .discovery_agent import score_universe
│   └── discovery_agent.py
└── classifier/
    ├── __init__.py          # template: from .news_classifier import classify_headlines
    └── news_classifier.py
tests/
└── test_alert_router.py     # NEW — Phase 5 tests
```

### Pattern 1: Package `__init__.py` (follow existing convention)

Both `discovery/` and `classifier/` use the same two-line pattern:

```python
# Source: src/signal_system/discovery/__init__.py (verified)
from .discovery_agent import score_universe

__all__ = ["score_universe"]
```

Apply identically to `router/__init__.py`:

```python
# src/signal_system/router/__init__.py
from .alert_router import route_signals

__all__ = ["route_signals"]
```

### Pattern 2: `_ensure_column()` in `init_db()` (follow existing convention)

The existing `init_db()` already calls `_ensure_column()` for four columns. Add `demoted_from` in the signals block:

```python
# Source: src/signal_system/state/repository.py (verified)
# Existing Phase 1 block:
_ensure_column(cursor, "signals", "routing_status", "TEXT")
_ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")
_ensure_column(cursor, "signals", "model_version", "TEXT")
_ensure_column(cursor, "signals", "thesis_version_hash", "TEXT")

# Add for Phase 5:
_ensure_column(cursor, "signals", "demoted_from", "TEXT")
```

### Pattern 3: `insert_signal()` extension (backward-compatible)

Current signature (verified from codebase):
```python
def insert_signal(signal: Signal, routing_status: str | None = None) -> bool:
```

Extended signature (Phase 5 Wave 0):
```python
def insert_signal(signal: Signal, routing_status: str | None = None, demoted_from: str | None = None) -> bool:
```

The SQL INSERT needs two changes: add `demoted_from` to the column list and add the value to the VALUES tuple:

```python
cursor.execute("""
    INSERT OR IGNORE INTO signals (
        alert_id, timestamp, agent, severity, ticker, title, body,
        score, routing_status, signal_price_snapshot, model_version,
        thesis_version_hash, demoted_from
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    signal.alert_id,
    signal.timestamp.isoformat(),
    signal.agent,
    signal.severity,
    signal.ticker,
    signal.title,
    signal.body,
    signal.score,
    routing_status,
    signal.signal_price_snapshot,
    signal.model_version,
    signal.thesis_version_hash,
    demoted_from,            # new — None for DELIVERED and MONITORING
))
```

### Pattern 4: DB fixture for router tests (follow Phase 4 pattern exactly)

```python
# Source: tests/test_discovery_agent.py (verified)
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"
```

For router tests that monkeypatch `count_delivered_today()` without needing a real DB:

```python
from unittest.mock import patch

def test_empty_budget(monkeypatch):
    with patch("signal_system.router.alert_router.count_delivered_today", return_value={}):
        result = route_signals([])
    assert result == []
```

For tests that need real DB state (pre-inserting DELIVERED signals to verify budget depletion):
Use the `db` fixture to get a real DB, `insert_signal()` pre-existing signals, then call `route_signals()`.

### Pattern 5: `route_signals()` core algorithm

```python
# Pseudocode — exact implementation for alert_router.py
from signal_system.models import Signal
from signal_system.state.repository import count_delivered_today

AR_CAP = 1
INFO_CAP = 3

def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:
    for s in signals:
        if s.severity == "MONITORING":
            raise ValueError(f"MONITORING signals must not be passed to route_signals(); got {s!r}")

    if not signals:
        return []

    counts = count_delivered_today()
    ar_remaining = max(0, AR_CAP - counts.get("ACTION_REQUIRED", 0))
    info_remaining = max(0, INFO_CAP - counts.get("INFORMATIONAL", 0))

    # Separate by severity
    ar_signals = [s for s in signals if s.severity == "ACTION_REQUIRED"]
    info_signals = [s for s in signals if s.severity == "INFORMATIONAL"]

    results: dict[str, tuple[str, str | None]] = {}  # alert_id → (status, demoted_from)

    # Allocate AR slots
    ar_sorted = sorted(ar_signals, key=lambda s: (-s.score, s.ticker))
    for i, sig in enumerate(ar_sorted):
        if ar_remaining == 0:
            # Budget was full from DB before this batch started
            results[sig.alert_id] = ("SUPPRESSED", "budget_cap_ar")
        elif i < ar_remaining:
            results[sig.alert_id] = ("DELIVERED", None)
        else:
            # Slot existed but was taken by a better-scored signal within this batch
            results[sig.alert_id] = ("SUPPRESSED", "outscored")

    # Allocate INFO slots
    info_sorted = sorted(info_signals, key=lambda s: (-s.score, s.ticker))
    for i, sig in enumerate(info_sorted):
        if info_remaining == 0:
            results[sig.alert_id] = ("SUPPRESSED", "budget_cap_info")
        elif i < info_remaining:
            results[sig.alert_id] = ("DELIVERED", None)
        else:
            results[sig.alert_id] = ("SUPPRESSED", "outscored")

    return [(sig, *results[sig.alert_id]) for sig in signals]
```

**Note on `s.ticker` in sort key:** `Signal.ticker` is `str | None`. If `ticker` is `None` for any signal, `(-s.score, s.ticker)` will raise `TypeError` when Python tries to compare `None` with a string. The safe approach: use `(s.ticker or "")` as the tiebreak key. For this project, all AR/INFO signals are expected to have tickers, but defensively handle `None`.

### Anti-Patterns to Avoid

- **Calling `insert_signal()` inside `route_signals()`:** D-14 is explicit — router is pure logic. Phase 6 does inserts.
- **Caching `count_delivered_today()` result across calls:** Each call must read fresh from DB (D-07).
- **Using Python `hash()` for any determinism need:** The codebase uses `hashlib` (established convention). Not relevant here since alert_ids are pre-computed.
- **Mutating `severity` on suppressed signals:** ROUT-02 explicitly forbids this.
- **Comparing `None` scores in sort without guard:** `Signal.score` is `float | None`. Sort key should handle: `(-(s.score or 0.0), s.ticker or "")`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| ET timezone | Custom offset math | `zoneinfo.ZoneInfo("America/New_York")` | Already used in `count_delivered_today()` and `insert_signal()` — consistent |
| Idempotent ALTER TABLE | `IF NOT EXISTS` guard | `_ensure_column()` | Already exists in `repository.py` — one call per column |
| Deterministic sort tiebreak | Custom comparator class | `sorted(x, key=lambda s: (-s.score or 0.0, s.ticker or ""))` | Matches `_rank_values()` convention from Phase 4 |

---

## Common Pitfalls

### Pitfall 1: `None`-safe sort key for score and ticker

**What goes wrong:** `Signal.score` is typed `float | None` and `Signal.ticker` is `str | None`. `sorted()` with key `(-s.score, s.ticker)` raises `TypeError: '<' not supported between instances of 'NoneType' and 'float'` if any signal has `score=None`.

**Why it happens:** The `Signal` dataclass allows `None` for both fields.

**How to avoid:** Always use `(-(s.score or 0.0), s.ticker or "")` as the sort key.

**Warning signs:** `TypeError` in test runs with signals constructed without explicit score values.

### Pitfall 2: `demoted_from` column not present at test time

**What goes wrong:** Tests call `route_signals()` which returns tuples that Phase 6 will pass to `insert_signal()`, but if `init_db()` hasn't been updated yet, the INSERT will fail because `demoted_from` column doesn't exist.

**Why it happens:** Wave 0 order matters — `_ensure_column()` call must land in `init_db()` before any test that exercises Phase 6 insert path.

**How to avoid:** Wave 0 is a prerequisite: schema migration + `insert_signal()` signature update must be committed before router implementation. Phase 5 tests test routing decisions only (no DB insert), so this only blocks Phase 6.

**Warning signs:** `OperationalError: table signals has no column named demoted_from`.

### Pitfall 3: `budget_cap_ar` vs `outscored` boundary condition

**What goes wrong:** When `ar_remaining == 0` at entry AND there are AR signals in the batch, all AR signals get `budget_cap_ar`. When `ar_remaining > 0` but fewer slots than signals, the overflow signals get `outscored`. These are different codes — confusing them breaks D-10.

**Why it happens:** Subtle conditional: `ar_remaining == 0` must be checked before iterating, not inside the loop.

**How to avoid:** Check `if ar_remaining == 0:` once before the loop (or as the first condition inside the loop, knowing it applies to every signal). See Pattern 5 pseudocode above.

**Warning signs:** Test "Second job run same day: DB already has 1 AR DELIVERED → new AR signal" fails if wrong code emitted.

### Pitfall 4: Score comparison edge case — equal scores with None

**What goes wrong:** Two signals with equal scores, one with `ticker=None`. Sort key `(-(s.score or 0.0), s.ticker or "")` maps `None` ticker to `""` which sorts before any letter. This is a deterministic behavior but must be documented.

**How to avoid:** Treat as acceptable (deterministic). Document: `None` ticker sorts before any lettered ticker when scores are equal.

---

## Key Codebase Facts (Verified)

### Q1: `insert_signal()` exact current signature [VERIFIED: direct codebase read]

```python
def insert_signal(signal: Signal, routing_status: str | None = None) -> bool:
```

**What Phase 5 adds:** `demoted_from: str | None = None` as a third keyword argument. The SQL INSERT adds `demoted_from` to both the column list (position 13) and the VALUES tuple. No other changes needed — `INSERT OR IGNORE` semantics unchanged.

### Q2: `count_delivered_today()` exact return shape and query [VERIFIED: direct codebase read]

```python
def count_delivered_today() -> dict[str, int]:
    today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    # ...
    cursor.execute("""
        SELECT severity, COUNT(*) FROM signals
        WHERE routing_status = 'DELIVERED'
          AND timestamp LIKE ? || '%'
        GROUP BY severity
    """, (today_iso,))
    return {row[0]: row[1] for row in cursor.fetchall()}
```

- Returns `{"ACTION_REQUIRED": N, "INFORMATIONAL": N}` — missing severities are absent (treat as 0).
- ET date prefix: `datetime.now(ZoneInfo("America/New_York")).date().isoformat()` → `"YYYY-MM-DD"`.
- `LIKE 'YYYY-MM-DD%'` matches all ET-stored ISO timestamps for that date.
- Budget reset happens automatically at ET midnight via this query — router inherits it, no re-implementation.

### Q3: `demoted_from` column existence [VERIFIED: direct codebase read]

**Does NOT exist yet.** The `init_db()` `_ensure_column()` calls currently add:
- `routing_status TEXT`
- `signal_price_snapshot REAL`
- `model_version TEXT`
- `thesis_version_hash TEXT`
- *(runs table)* `tickers_scanned INTEGER`, `tickers_signaled INTEGER`

`demoted_from` is absent. Phase 5 Wave 0 must add:
```python
_ensure_column(cursor, "signals", "demoted_from", "TEXT")
```

### Q4: Monkeypatching `repo.DB_PATH` pattern [VERIFIED: direct codebase read]

```python
# From tests/test_discovery_agent.py (verified)
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"
```

`DB_PATH` is a module-level `Path` constant. `monkeypatch.setattr(repository, "DB_PATH", ...)` patches it correctly. `repository.init_db()` then creates the temp DB with all migrations applied.

### Q5: `__init__.py` package template [VERIFIED: direct codebase read]

Both existing subpackages use the same two-line pattern:

```python
# discovery/__init__.py
from .discovery_agent import score_universe
__all__ = ["score_universe"]

# classifier/__init__.py
from signal_system.classifier.news_classifier import classify_headlines, ClassificationResult
__all__ = ["classify_headlines", "ClassificationResult"]
```

`router/__init__.py` should follow the relative-import style (`from .alert_router import route_signals`).

### Q6: `Signal` frozen dataclass — all fields [VERIFIED: direct codebase read]

```python
@dataclass(frozen=True, slots=True)
class Signal:
    ticker: str | None          # required
    score: float | None         # required
    severity: Severity          # required — Literal["ACTION_REQUIRED", "INFORMATIONAL", "MONITORING"]
    agent: str                  # required
    timestamp: datetime         # required
    alert_id: str               # required
    title: str                  # required
    body: str | None = None
    sub_scores: dict[str, float] = field(default_factory=dict)
    model_version: str | None = None
    thesis_version_hash: str | None = None
    signal_price_snapshot: float | None = None
```

`routing_status` is **NOT** a Signal field — it lives in the DB only.

### Q7: Discovery Phase A direct-insert pattern [VERIFIED: direct codebase read]

Yes, confirmed in `discovery_agent.py` line 138:
```python
if config.DISCOVERY_PHASE == "A":
    repository.insert_signal(signal, routing_status="MONITORING")
```

MONITORING signals go straight to DB. They never enter `route_signals()`. D-16 is accurate.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (discovered via `uv run pytest`) |
| Config file | `pyproject.toml` (standard uv project) |
| Quick run command | `uv run pytest tests/test_alert_router.py -x` |
| Full suite command | `uv run pytest` |

**Current state:** 87 tests collected across `test_discovery_agent.py` and `test_smoke.py`. Phase 5 gate: "87+ tests pass" (new router tests add to this count).

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ROUT-01 | AR cap=1, INFO cap=3 enforced | unit | `uv run pytest tests/test_alert_router.py::test_ar_cap -x` | ❌ Wave 0 |
| ROUT-01 | Budget full from DB → all suppressed | unit | `uv run pytest tests/test_alert_router.py::test_db_full_budget -x` | ❌ Wave 0 |
| ROUT-02 | Higher-scored wins slot, loser gets `outscored` | unit | `uv run pytest tests/test_alert_router.py::test_slot_competition -x` | ❌ Wave 0 |
| ROUT-02 | `demoted_from` reason codes correct | unit | `uv run pytest tests/test_alert_router.py::test_demoted_from_codes -x` | ❌ Wave 0 |
| ROUT-03 | Router reads DB, not in-memory | unit | `uv run pytest tests/test_alert_router.py::test_reads_db_each_call -x` | ❌ Wave 0 |
| ROUT-04 | ET midnight reset (via `count_delivered_today`) | unit | `uv run pytest tests/test_alert_router.py::test_et_budget_reset -x` | ❌ Wave 0 |
| ROUT-05 | Equal scores → alphabetical tiebreak is deterministic | unit | `uv run pytest tests/test_alert_router.py::test_alphabetical_tiebreak -x` | ❌ Wave 0 |
| D-15 | MONITORING input → ValueError | unit | `uv run pytest tests/test_alert_router.py::test_monitoring_raises -x` | ❌ Wave 0 |
| D-13 | Empty input → `[]` | unit | `uv run pytest tests/test_alert_router.py::test_empty_input -x` | ❌ Wave 0 |

### D-19 Scenario Coverage Map

| Scenario from D-19 | Test name suggestion |
|--------------------|---------------------|
| 5 AR signals → 1 DELIVERED (highest score+alpha), 4 SUPPRESSED | `test_5_ar_signals_one_slot` |
| Mixed (2 AR + 5 INFO) → 1 AR + 3 INFO DELIVERED, 3 SUPPRESSED | `test_mixed_severity_batch` |
| Second run same day: DB has 1 AR → new AR → `budget_cap_ar` | `test_cross_run_no_eviction` |
| Equal scores, different tickers → alphabetical winner | `test_alphabetical_tiebreak` |
| ET midnight reset: signals on adjacent days in different budget windows | `test_et_midnight_reset` (mock `count_delivered_today`) |
| Empty input → `[]` | `test_empty_input` |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_alert_router.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite (87+ tests) green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/test_alert_router.py` — covers all ROUT-xx requirements
- [ ] `src/signal_system/router/__init__.py` — package init
- [ ] `src/signal_system/router/alert_router.py` — main implementation

*(conftest.py already has env var setup — no changes needed)*

---

## Security Domain

> `security_enforcement` not explicitly set in config.json — treated as enabled.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | No | Router is internal pure function |
| V3 Session Management | No | Stateless function |
| V4 Access Control | No | Internal module, no user-facing surface |
| V5 Input Validation | Yes | ValueError on MONITORING severity input (D-15) |
| V6 Cryptography | No | No crypto operations |

**Threat note:** Router input comes from agents, not external sources. The main risk is a mistyped severity string slipping through — the `ValueError` guard on D-15 plus Python's `Literal` type annotation provides defense. No SQL injection risk in this module (router makes no SQL calls of its own).

---

## Environment Availability

> Step 2.6: All dependencies are project-internal (stdlib + existing project packages). No external tools or services required by the router module itself.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12+ | `from __future__ import annotations`, `zoneinfo` | ✓ | (project requirement) | — |
| `uv` | Test runner | ✓ | (project standard) | — |
| SQLite | `count_delivered_today()` DB read | ✓ | stdlib | — |

**Missing dependencies with no fallback:** None.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `Signal.ticker` is always non-None for AR/INFO severity signals in practice (though typed `str \| None`) | Pitfall 1 / Pattern 5 | Sort key `(s.ticker or "")` handles the None case defensively regardless |
| A2 | `Signal.score` is always non-None for AR/INFO severity signals in practice (though typed `float \| None`) | Pattern 5 | Sort key `(-(s.score or 0.0), ...)` handles None defensively |

All other claims are [VERIFIED: direct codebase read].

---

## Open Questions

1. **`demoted_from` code when `ar_remaining == 0` from DB vs. when slot was taken intra-batch**
   - What we know: D-10 defines three codes. D-04 specifies allocation order.
   - What's unclear: If 2 AR signals come in and ar_remaining=0 from DB, do both get `budget_cap_ar`? Yes — D-10 says `"budget_cap_ar"` covers "from DB read OR intra-batch allocation." The distinction is `outscored` is only when a slot was available but a better-scored signal took it.
   - **Resolved:** `budget_cap_ar` when `ar_remaining == 0` (regardless of source). `outscored` when `ar_remaining > 0` but the signal lost the intra-batch competition.

2. **Return order of output tuples**
   - What we know: The signature says `list[tuple[Signal, str, str | None]]` with "one per input signal."
   - What's unclear: Must output order match input order?
   - **Recommendation:** Yes — preserve input order. Caller (Phase 6) may rely on order for logging. Implementation: collect routing decisions in a dict keyed by `alert_id`, then reconstruct output by iterating over input `signals`.

---

## Sources

### Primary (HIGH confidence)
- `src/signal_system/state/repository.py` — verified `insert_signal()` signature, `count_delivered_today()` implementation, `_ensure_column()` pattern, `init_db()` migration block
- `src/signal_system/models.py` — verified `Signal` dataclass all fields, `Severity` type
- `src/signal_system/discovery/discovery_agent.py` — verified Phase A direct-insert pattern, `_rank_values()` sort convention
- `src/signal_system/discovery/__init__.py` — verified `__init__.py` package template
- `src/signal_system/classifier/__init__.py` — verified `__init__.py` package template
- `tests/test_discovery_agent.py` — verified `db` fixture pattern, monkeypatch approach
- `tests/conftest.py` — verified env var setup, no router-specific setup needed

### Secondary (MEDIUM confidence)
- `.planning/phases/05-alert-router/05-CONTEXT.md` — all implementation decisions D-01 through D-20

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new packages, all existing stdlib
- Architecture: HIGH — verified directly from codebase, patterns established by Phases 1-4
- Pitfalls: HIGH — identified from actual codebase types (`str | None` fields)
- Test patterns: HIGH — verified from existing test files

**Research date:** 2026-05-16
**Valid until:** Stable — pure stdlib phase, no external dependencies to drift

---

## RESEARCH COMPLETE

**Phase:** 5 — Alert Router
**Confidence:** HIGH

### Key Findings

1. **Wave 0 is two surgical edits to `repository.py`:** `init_db()` needs `_ensure_column(..., "demoted_from", "TEXT")`, and `insert_signal()` needs `demoted_from: str | None = None` added to signature + SQL INSERT. Both are backward-compatible.

2. **Router structure follows established package pattern exactly:** `router/__init__.py` (two lines, re-export), `router/alert_router.py` (pure function). No new deps.

3. **Sort key must guard against `None`:** `Signal.score` and `Signal.ticker` are typed `str | None` / `float | None`. Sort key: `(-(s.score or 0.0), s.ticker or "")`.

4. **`demoted_from` code logic:** `budget_cap_ar`/`budget_cap_info` when `ar_remaining == 0` entering the batch. `outscored` when a slot was available but taken by a higher-ranked signal in the same batch.

5. **87 tests currently pass.** Phase 5 adds `tests/test_alert_router.py` covering all 9 D-19 scenarios + D-15 ValueError guard + empty input. Full suite must still be 87+ (all existing tests green).

### File Created
`.planning/phases/05-alert-router/05-RESEARCH.md`

### Confidence Assessment

| Area | Level | Reason |
|------|-------|--------|
| Standard Stack | HIGH | No new packages; all verified from codebase |
| Architecture | HIGH | Package pattern verified from 2 existing examples |
| Repository changes | HIGH | Exact SQL and signatures read from source |
| Pitfalls | HIGH | Derived from actual type signatures in models.py |
| Test patterns | HIGH | Verified from test_discovery_agent.py |

### Ready for Planning
Research complete. Planner can now create PLAN.md.
