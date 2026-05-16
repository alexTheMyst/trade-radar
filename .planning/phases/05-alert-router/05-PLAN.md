---
phase: 05-alert-router
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/signal_system/state/repository.py
  - src/signal_system/router/__init__.py
  - src/signal_system/router/alert_router.py
  - tests/test_alert_router.py
autonomous: true
requirements: [ROUT-01, ROUT-02, ROUT-03, ROUT-04, ROUT-05]

must_haves:
  truths:
    - "route_signals([]) returns []"
    - "5 AR signals in one batch → exactly 1 DELIVERED (highest score, alphabetical tiebreak on tie), 4 SUPPRESSED with demoted_from in {'outscored', 'budget_cap_ar'}"
    - "Mixed batch 2 AR + 5 INFO → 1 AR DELIVERED + 3 INFO DELIVERED + 3 SUPPRESSED with correct demoted_from codes"
    - "When count_delivered_today returns {'ACTION_REQUIRED': 1} → new AR signal gets routing_status=SUPPRESSED, demoted_from='budget_cap_ar'"
    - "Equal score, two tickers → alphabetically first ticker is DELIVERED, second is SUPPRESSED with demoted_from='outscored'"
    - "route_signals([sig with severity='MONITORING']) raises ValueError"
    - "DELIVERED signals always have demoted_from=None"
    - "insert_signal(signal, routing_status='MONITORING', demoted_from=None) succeeds — backward-compatible"
  artifacts:
    - path: "src/signal_system/router/alert_router.py"
      provides: "route_signals() pure function"
      exports: ["route_signals"]
    - path: "src/signal_system/router/__init__.py"
      provides: "package entry point"
      contains: "from .alert_router import route_signals"
    - path: "tests/test_alert_router.py"
      provides: "≥9 tests covering all D-19 scenarios and ROUT-01..ROUT-05"
      contains: "test_ar_budget_one_winner"
  key_links:
    - from: "alert_router.py"
      to: "repository.count_delivered_today"
      via: "direct import"
      pattern: "from signal_system.state import repository"
    - from: "test_alert_router.py"
      to: "route_signals"
      via: "package import"
      pattern: "from signal_system.router import route_signals"
---

# Phase 5: Alert Router — Plan

## Phase Goal

**As a** solo trading operator, **I want** all signals from both agents to flow through a
budget-enforcing router before insertion, **so that** I never receive more than 1
`ACTION_REQUIRED` and 3 `INFORMATIONAL` alerts per day, with the highest-scored signal winning
each slot and all suppression decisions captured in SQLite for review.

---

## Requirements Coverage

| Requirement | Tasks That Cover It |
|-------------|---------------------|
| ROUT-01 — Hard caps: 1 AR + 3 INFO per day, combined across agents | Wave 1 Task 1 (`_BUDGET_AR`, `_BUDGET_INFO` constants + allocation loop); Wave 2 T-AR-01, T-AR-02 |
| ROUT-02 — Slot competition: severity-first, score-ranked, alpha tiebreak; demoted_from reason codes | Wave 1 Task 1 (sort key + reason code assignment); Wave 0 Task 1 (demoted_from column); Wave 2 T-AR-01, T-AR-04, T-AR-07 |
| ROUT-03 — Cross-run budget awareness: no eviction, first DELIVERED stands | Wave 1 Task 1 (reads count_delivered_today once); Wave 2 T-AR-03 |
| ROUT-04 — Budget resets at America/New_York midnight (inherited from count_delivered_today) | Wave 1 Task 1 (inherits from count_delivered_today ET logic); Wave 2 T-AR-09 (ET midnight reset) |
| ROUT-05 — Deterministic tiebreak: equal score → alphabetical ticker winner | Wave 1 Task 1 (sort key `(-(score or 0.0), ticker or "")`); Wave 2 T-AR-04 |

---

## Threat Model

| Threat | STRIDE | Mitigation | Code Location |
|--------|--------|------------|---------------|
| MONITORING signal accidentally routed (wrong budget slot consumed) | Elevation | `ValueError` raised immediately if `severity == "MONITORING"` enters `route_signals()` | `alert_router.py` guard |
| Budget bypass via float NaN score (NaN comparisons are non-deterministic) | Tampering | `score or 0.0` coerces None/NaN to 0.0; highest explicit score always wins | `alert_router.py` sort key |
| demoted_from free-form string leaks implementation details | Information Disclosure | Typed `Literal["budget_cap_ar","budget_cap_info","outscored"]` enforced at assignment; SQL INSERT parameterised | `alert_router.py` constants |
| SQL injection via routing_status / demoted_from values | Tampering | All DB writes via `repository.insert_signal()` with parameterised `?` placeholders | `repository.py:insert_signal` |
| Race condition: two jobs run simultaneously, both see 0 delivered | Denial of Service | SQLite WAL mode + INSERT OR IGNORE makes double-insert safe; no eviction means first write wins | `repository.py` WAL + INSERT OR IGNORE |

---

## Source Audit

| Source | Item | Covered By |
|--------|------|------------|
| GOAL | route_signals() importable, returns correct tuples, 87+ tests pass | Wave 1 Task 1 + Wave 2 |
| REQ ROUT-01 | Hard caps 1 AR + 3 INFO | Wave 1 Task 1 constants + loop |
| REQ ROUT-02 | Slot competition + demoted_from codes | Wave 0 Task 1 + Wave 1 Task 1 |
| REQ ROUT-03 | No eviction, cross-run reads count_delivered_today | Wave 1 Task 1 |
| REQ ROUT-04 | ET midnight reset (inherited) | Wave 1 Task 1 (no new code needed) |
| REQ ROUT-05 | Deterministic alpha tiebreak | Wave 1 Task 1 sort key |
| CONTEXT D-01–D-02 | Budget constants + no per-agent split | Wave 1 Task 1 |
| CONTEXT D-03–D-05 | Intra-batch sort, tiebreak | Wave 1 Task 1 |
| CONTEXT D-06–D-07 | No eviction, count_delivered_today read | Wave 1 Task 1 |
| CONTEXT D-08 | ET midnight inherited from count_delivered_today | Wave 1 Task 1 |
| CONTEXT D-09–D-11 | demoted_from column + typed codes + insert-only | Wave 0 Task 1 + Wave 1 Task 1 |
| CONTEXT D-12–D-14 | Module location, signature, pure function | Wave 1 Task 1 |
| CONTEXT D-15 | MONITORING raises ValueError | Wave 1 Task 1 + Wave 2 T-AR-06 |
| CONTEXT D-16 | MONITORING bypass — not in router scope | N/A (already in discovery_agent.py) |
| CONTEXT D-17–D-18 | Phase 5 scope fence + insert_signal demoted_from kwarg | Wave 0 Task 1 |
| CONTEXT D-19 | All 7 test scenarios | Wave 2 T-AR-01..T-AR-07 |
| CONTEXT D-20 | count_delivered_today monkeypatched in tests | Wave 2 fixture |

---

## Tasks

---

### Wave 0 — Prerequisites: Extend repository.py (no new files)

Wave 0 must complete before Wave 1. Single task — two surgical edits to `repository.py`.

---

<task type="auto">
  <name>Wave 0 / Task 1: Add demoted_from to repository.py</name>
  <read_first>
    - src/signal_system/state/repository.py (current insert_signal signature, init_db _ensure_column block, INSERT column list)
    - .planning/phases/05-alert-router/05-PATTERNS.md (exact SQL diff, line references for insert point)
  </read_first>
  <files>src/signal_system/state/repository.py</files>
  <behavior>
    - insert_signal(signal) with no kwargs → DB row has demoted_from=NULL (backward-compatible)
    - insert_signal(signal, routing_status="MONITORING") → works unchanged (backward-compatible)
    - insert_signal(signal, routing_status="SUPPRESSED", demoted_from="budget_cap_ar") → DB row has demoted_from="budget_cap_ar"
    - insert_signal(signal, routing_status="DELIVERED", demoted_from=None) → DB row has demoted_from=NULL
    - After init_db(), signals table has demoted_from column (TEXT, nullable)
    - Calling init_db() twice on a DB that already has demoted_from → no error (idempotent via _ensure_column)
  </behavior>
  <action>
    **Edit 1 — init_db(): add _ensure_column for demoted_from.**
    Locate the existing Phase 4 _ensure_column block (runs table, tickers_scanned/tickers_signaled).
    After the last existing `_ensure_column(cursor, "runs", ...)` call, add a new block:
      `_ensure_column(cursor, "signals", "demoted_from", "TEXT")`
    This is inside the existing try block before conn.commit().

    **Edit 2 — insert_signal(): add demoted_from kwarg and wire it into INSERT.**
    Change the function signature from:
      `def insert_signal(signal: Signal, routing_status: str | None = None) -> bool:`
    to:
      `def insert_signal(`
      `    signal: Signal,`
      `    routing_status: str | None = None,`
      `    demoted_from: str | None = None,`
      `) -> bool:`

    In the INSERT statement, add `demoted_from` to the column list (after `thesis_version_hash`)
    and add a corresponding `?` placeholder. Add `demoted_from,` as the last value in the tuple.
    Column count goes from 12 to 13. No other changes to the function body.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && uv run python -c "
import inspect
from signal_system.state import repository
sig = inspect.signature(repository.insert_signal)
assert 'demoted_from' in sig.parameters, 'demoted_from kwarg missing from insert_signal'
print('Wave 0 Task 1 checks passed')
"</automated>
  </verify>
  <done>
    insert_signal() has demoted_from: str | None = None kwarg. INSERT uses 13 columns including
    demoted_from. init_db() calls _ensure_column(cursor, "signals", "demoted_from", "TEXT").
    All existing call sites continue to work without passing demoted_from.
  </done>
</task>

---

### Wave 1 — Router Package (depends on Wave 0)

Two files; __init__.py is trivial and can be created alongside alert_router.py in one task.

---

<task type="auto">
  <name>Wave 1 / Task 1: Create router package — __init__.py and alert_router.py</name>
  <read_first>
    - src/signal_system/discovery/__init__.py (exact pattern for __init__.py)
    - src/signal_system/discovery/discovery_agent.py (module structure, logging, constants pattern)
    - src/signal_system/state/repository.py (count_delivered_today signature and return shape)
    - src/signal_system/models.py (Signal fields, Severity type, score: float | None, ticker: str | None)
    - .planning/phases/05-alert-router/05-PATTERNS.md (full pseudocode for route_signals, sort key, reason code logic)
    - .planning/phases/05-alert-router/05-CONTEXT.md (D-01..D-15 locked decisions)
  </read_first>
  <files>
    src/signal_system/router/__init__.py,
    src/signal_system/router/alert_router.py
  </files>
  <behavior>
    route_signals([]) → []
    route_signals([sig with severity="MONITORING"]) → raises ValueError containing "MONITORING"
    5 AR signals (scores 90,80,70,60,50) → 1 DELIVERED (score=90), 4 SUPPRESSED
      - score=80 → demoted_from="outscored" (slot taken by intra-batch peer)
      - score=70,60,50 → demoted_from="outscored"
    When count_delivered_today returns {"ACTION_REQUIRED": 1} and 1 new AR signal arrives →
      routing_status="SUPPRESSED", demoted_from="budget_cap_ar"
    When count_delivered_today returns {"INFORMATIONAL": 3} and 1 new INFO signal arrives →
      routing_status="SUPPRESSED", demoted_from="budget_cap_info"
    2 AR signals with equal score, tickers "MSFT" and "AAPL" → AAPL DELIVERED, MSFT SUPPRESSED with demoted_from="outscored"
    Mixed batch 2 AR + 5 INFO with full budget → 1 AR DELIVERED + 3 INFO DELIVERED + 3 SUPPRESSED
    DELIVERED signals always have demoted_from=None in the returned tuple
    from signal_system.router import route_signals → importable
  </behavior>
  <action>
    **File 1: src/signal_system/router/__init__.py** (create new directory + file)
    Content:
      `from .alert_router import route_signals`
      `__all__ = ["route_signals"]`

    **File 2: src/signal_system/router/alert_router.py** (create)

    Module docstring: "Alert Router — enforces daily budget caps and slot competition.
    Pure logic: reads count_delivered_today() once, runs severity-first slot competition
    with deterministic tiebreak, returns (signal, routing_status, demoted_from) tuples.
    Does NOT write to DB."

    Imports:
      `from __future__ import annotations`
      `import logging`
      `from signal_system.models import Signal`
      `from signal_system.state import repository`
      `logger = logging.getLogger(__name__)`

    Module-level constants (D-01, D-10):
      `_BUDGET_AR: int = 1`
      `_BUDGET_INFO: int = 3`

    Public function signature (D-13):
      `def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:`

    Implementation contract:
    1. Guard loop: if any signal.severity == "MONITORING" → raise ValueError(
       f"MONITORING signals bypass the router; got ticker={sig.ticker!r}")
    2. Early return: if not signals → return []
    3. Read budget once: `delivered = repository.count_delivered_today()`
       `ar_used = delivered.get("ACTION_REQUIRED", 0)`
       `info_used = delivered.get("INFORMATIONAL", 0)`
       `ar_remaining = max(0, _BUDGET_AR - ar_used)`
       `info_remaining = max(0, _BUDGET_INFO - info_used)`
    4. Separate and sort AR signals:
       `ar_signals = sorted([s for s in signals if s.severity == "ACTION_REQUIRED"],`
       `    key=lambda s: (-(s.score or 0.0), s.ticker or ""))`
    5. Separate and sort INFO signals same way.
    6. Initialize `results: list[tuple[Signal, str, str | None]] = []`
    7. Allocate AR slots — for i, sig in enumerate(ar_signals):
       - if i < ar_remaining → append (sig, "DELIVERED", None)
       - elif ar_remaining == 0 and ar_used >= _BUDGET_AR → append (sig, "SUPPRESSED", "budget_cap_ar")
       - else → append (sig, "SUPPRESSED", "outscored")
    8. Allocate INFO slots — for i, sig in enumerate(info_signals):
       - if i < info_remaining → append (sig, "DELIVERED", None)
       - elif info_remaining == 0 and info_used >= _BUDGET_INFO → append (sig, "SUPPRESSED", "budget_cap_info")
       - else → append (sig, "SUPPRESSED", "outscored")
    9. return results

    The reason code logic (step 7/8): `ar_remaining == 0 and ar_used >= _BUDGET_AR` detects
    cross-run full budget (DB already had 1 delivered). Otherwise, budget was available when the
    batch started but this signal was outscored by an intra-batch peer.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && uv run python -c "
from signal_system.router import route_signals
print('route_signals importable:', callable(route_signals))
result = route_signals([])
assert result == [], f'Expected [] for empty input, got {result}'
print('Wave 1 Task 1 checks passed')
"</automated>
  </verify>
  <done>
    src/signal_system/router/__init__.py exports route_signals.
    route_signals() is importable, returns [] for empty input, raises ValueError for MONITORING
    input. Budget logic follows D-01..D-15: severity-first, score-ranked, alpha tiebreak,
    correct demoted_from codes, DELIVERED tuples have demoted_from=None.
  </done>
</task>

---

### Wave 2 — Tests (depends on Wave 1)

---

<task type="auto">
  <name>Wave 2 / Task 1: Create tests/test_alert_router.py covering all D-19 scenarios</name>
  <read_first>
    - tests/test_discovery_agent.py (db fixture, monkeypatching pattern, _sig helper pattern)
    - tests/conftest.py (dummy env vars)
    - src/signal_system/router/alert_router.py (just implemented — verify behavior contracts)
    - src/signal_system/models.py (Signal fields, compute_alert_id signature)
    - .planning/phases/05-alert-router/05-CONTEXT.md (D-19 test scenarios, D-20 fixture guidance)
    - .planning/phases/05-alert-router/05-PATTERNS.md (test patterns section)
  </read_first>
  <files>tests/test_alert_router.py</files>
  <behavior>
    T-AR-01: 5 AR signals (scores 90,80,70,60,50) → 1 DELIVERED (score=90), 4 SUPPRESSED (outscored)
    T-AR-02: Mixed batch 2 AR (scores 85,75) + 5 INFO (scores 95,88,70,60,50) →
      1 AR DELIVERED (score=85) + 3 INFO DELIVERED (scores 95,88,70) + 3 SUPPRESSED (2 outscored INFO + 1 outscored AR)
    T-AR-03: count_delivered_today returns {"ACTION_REQUIRED": 1} + 1 new AR signal →
      SUPPRESSED with demoted_from="budget_cap_ar"
    T-AR-04: 2 AR signals with equal score=75, tickers "MSFT" and "AAPL" →
      AAPL DELIVERED, MSFT SUPPRESSED with demoted_from="outscored"
    T-AR-05: count_delivered_today returns {"INFORMATIONAL": 3} + 1 new INFO signal →
      SUPPRESSED with demoted_from="budget_cap_info"
    T-AR-06: route_signals([sig with severity="MONITORING"]) raises ValueError
    T-AR-07: route_signals([]) returns []
    T-AR-08: DELIVERED signals have demoted_from=None in all tuples
    uv run pytest tests/test_alert_router.py -q → all tests pass (0 failures)
    uv run pytest -q → 87+ tests pass (existing suite unbroken)
  </behavior>
  <action>
    Create tests/test_alert_router.py with:

    **Imports:**
      `from datetime import datetime, timezone`
      `from unittest.mock import patch`
      `import pytest`
      `from signal_system.state import repository`
      `from signal_system.router import route_signals`
      `from signal_system.models import Signal, compute_alert_id`

    **Constants:**
      `DATE_ISO = "2026-05-16"`
      `_TS = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc)`

    **Helper `_sig(ticker, score, severity, agent)`** — builds a minimal valid Signal:
      ticker default "AAPL", score default 75.0, severity default "INFORMATIONAL",
      agent default "test_agent". Uses compute_alert_id(ticker, DATE_ISO, "test_rule", agent)
      for alert_id. Sets timestamp=_TS, title=f"{ticker}: test".

    **`db` fixture** (needed for T-AR-03 cross-run test):
      `@pytest.fixture`
      `def db(tmp_path, monkeypatch):`
      `    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")`
      `    repository.init_db()`
      `    return tmp_path / "test.db"`

    **T-AR-07:** `test_empty_input` — monkeypatch count_delivered_today to return {}; assert route_signals([]) == []

    **T-AR-06:** `test_monitoring_raises` — call route_signals([_sig(severity="MONITORING")]); assert raises ValueError with match "MONITORING"

    **T-AR-01:** `test_ar_budget_one_winner(monkeypatch)` —
      mock count_delivered_today → {"ACTION_REQUIRED": 0, "INFORMATIONAL": 0}
      build 5 AR signals with scores 90,80,70,60,50 (tickers A1..A5)
      call route_signals; assert exactly 1 DELIVERED (score=90 ticker)
      assert all others SUPPRESSED with demoted_from="outscored"

    **T-AR-04:** `test_tiebreak_alphabetical(monkeypatch)` —
      mock count_delivered_today → {"ACTION_REQUIRED": 0}
      build 2 AR signals: ticker="MSFT" score=75, ticker="AAPL" score=75
      assert AAPL is DELIVERED (alphabetically first), MSFT is SUPPRESSED with demoted_from="outscored"

    **T-AR-08:** `test_delivered_demoted_from_is_none(monkeypatch)` —
      mock count_delivered_today → {}
      build 1 INFO signal; assert result tuple is (sig, "DELIVERED", None)

    **T-AR-02:** `test_mixed_batch_allocation(monkeypatch)` —
      mock count_delivered_today → {"ACTION_REQUIRED": 0, "INFORMATIONAL": 0}
      build 2 AR (scores 85,75) + 5 INFO (scores 95,88,70,60,50)
      assert 1 AR DELIVERED + 3 INFO DELIVERED + 3 SUPPRESSED
      assert suppressed INFO have demoted_from="outscored" (not "budget_cap_info" since slots were available)

    **T-AR-03:** `test_cross_run_ar_full(db, monkeypatch)` —
      Use db fixture (real SQLite in tmp_path). Insert a prior DELIVERED AR signal via
      repository.insert_signal(prior_sig, routing_status="DELIVERED").
      Then call route_signals([new_ar_sig]) — this reads the real count_delivered_today().
      Assert result is (new_ar_sig, "SUPPRESSED", "budget_cap_ar").

    **T-AR-05:** `test_cross_run_info_full(monkeypatch)` —
      mock count_delivered_today → {"INFORMATIONAL": 3}
      build 1 INFO signal; assert SUPPRESSED with demoted_from="budget_cap_info"

    **T-AR-09:** `test_et_midnight_reset(tmp_path, monkeypatch)` —
      Validates that count_delivered_today() uses ET date prefix matching (ROUT-04 / ROADMAP criterion 4).
      Use real DB via the db fixture. Insert 1 AR DELIVERED signal with timestamp set to
      yesterday's ET date (e.g. "2026-05-15T23:59:00-04:00"). Then call route_signals() for
      a new AR signal. Because count_delivered_today() uses LIKE '2026-05-16%' (today's ET date)
      and the existing signal's timestamp is '2026-05-15...', it should NOT count against today's
      budget → the new AR signal is DELIVERED (fresh budget window).
      Assert: result is (new_sig, "DELIVERED", None).
      This test exercises repository.count_delivered_today() ET date logic indirectly via the
      full route_signals() flow with real SQLite.

    Use `monkeypatch.setattr(repository, "count_delivered_today", lambda: {...})` for all
    tests except T-AR-03 and T-AR-09 (which use the real DB fixture).
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && uv run pytest tests/test_alert_router.py -q 2>&1 | tail -5</automated>
    <automated>cd /Users/alex/Documents/code/trading_agent && uv run pytest -q 2>&1 | tail -3</automated>
  </verify>
  <done>
    tests/test_alert_router.py contains ≥9 tests (T-AR-01..T-AR-09). All pass. Full suite
    passes (95+ tests). All D-19 scenarios covered including ET midnight reset. All
    ROUT-01..ROUT-05 requirements verified by at least one test.
  </done>
</task>

---

## Verification Criteria

After all waves complete:

```bash
# Import check
uv run python -c "from signal_system.router import route_signals; print('OK')"

# Schema check
uv run python -c "from signal_system.state import repository; repository.init_db()"
sqlite3 state/signals.db ".schema signals" | grep demoted_from

# Full test suite
uv run pytest -q
# Expected: 95+ passed (87 existing + ≥8 new)
```

Phase 5 is complete when:
1. `from signal_system.router import route_signals` imports cleanly
2. `signals` table has `demoted_from TEXT` column
3. `insert_signal()` accepts `demoted_from` kwarg (backward-compatible)
4. All ROUT-01..ROUT-05 tests pass
5. Full test suite passes (no regressions)
