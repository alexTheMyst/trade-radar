# Phase 5: Alert Router — Pattern Map

**Mapped:** 2026-05-16
**Files analyzed:** 4 (3 new, 1 modified)
**Analogs found:** 4 / 4

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/signal_system/router/alert_router.py` | service / pure-function | request-response (read DB → transform → return) | `src/signal_system/discovery/discovery_agent.py` | role-match |
| `src/signal_system/router/__init__.py` | package init | N/A | `src/signal_system/discovery/__init__.py` | exact |
| `src/signal_system/state/repository.py` | repository (modify) | CRUD | self (existing `insert_signal`, `_ensure_column`) | exact |
| `tests/test_alert_router.py` | test | request-response | `tests/test_discovery_agent.py` | exact |

---

## Pattern Assignments

### `src/signal_system/router/alert_router.py` (service, request-response)

**Analog:** `src/signal_system/discovery/discovery_agent.py`

**Imports pattern** (`discovery_agent.py` lines 1–18):
```python
"""Alert Router — enforces daily budget caps and slot competition.

Pure logic: reads count_delivered_today() once, runs severity-first slot
competition with deterministic tiebreak, returns (signal, routing_status,
demoted_from) tuples. Does NOT write to DB.
"""
from __future__ import annotations

import logging
from typing import Literal

from signal_system.models import Signal
from signal_system.state import repository

logger = logging.getLogger(__name__)
```

**Constants pattern** (`discovery_agent.py` lines 21–26 — module-level typed constants):
```python
# Hard-coded daily budget caps (ROUT-01 / D-01)
_BUDGET_AR: int = 1
_BUDGET_INFO: int = 3

# Valid demoted_from reason codes (D-10) — use these string literals, not free-form
DemotedFrom = Literal["budget_cap_ar", "budget_cap_info", "outscored"]
```

**Tiebreak sort pattern** (`discovery_agent.py` lines 29–37 — `_rank_values` uses same key):
```python
# Descending score, ascending ticker alphabetical (D-05 / ROUT-05)
# Mirror of _rank_values() sort key in discovery_agent.py:
sorted_items = sorted(values.items(), key=lambda x: (-x[1], x[0]))
```

**Core function pattern** (`discovery_agent.py` lines 40–47 — public function with docstring, reads config/DB once at top):
```python
def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:
    """Route a batch of signals against today's delivery budget.

    Returns a list of (signal, routing_status, demoted_from) tuples,
    one per input signal. routing_status is 'DELIVERED' or 'SUPPRESSED'.
    demoted_from is None for DELIVERED signals.

    Reads count_delivered_today() once at start. Does NOT insert to DB.
    Caller (Phase 6 job) handles insert_signal() and email.
    """
    # Guard: MONITORING signals must never enter the router (D-15)
    for sig in signals:
        if sig.severity == "MONITORING":
            raise ValueError(
                f"MONITORING signals bypass the router; got ticker={sig.ticker!r}"
            )

    if not signals:
        return []

    # Read DB budget once — cross-run awareness (D-07)
    delivered = repository.count_delivered_today()
    ar_used = delivered.get("ACTION_REQUIRED", 0)
    info_used = delivered.get("INFORMATIONAL", 0)
    ar_remaining = max(0, _BUDGET_AR - ar_used)
    info_remaining = max(0, _BUDGET_INFO - info_used)

    results: list[tuple[Signal, str, str | None]] = []

    # --- Severity-first slot competition (D-04) ---
    # Step 1: AR signals — sort descending score, ascending ticker
    ar_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )
    # Step 2: INFO signals — same sort
    info_signals = sorted(
        [s for s in signals if s.severity == "INFORMATIONAL"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )

    # Allocate AR slots
    for i, sig in enumerate(ar_signals):
        if i < ar_remaining:
            results.append((sig, "DELIVERED", None))
        elif ar_remaining == 0 and ar_used >= _BUDGET_AR:
            # Budget was full before this batch (cross-run, D-06)
            results.append((sig, "SUPPRESSED", "budget_cap_ar"))
        else:
            # Outscored within this batch
            results.append((sig, "SUPPRESSED", "outscored"))

    # Allocate INFO slots
    for i, sig in enumerate(info_signals):
        if i < info_remaining:
            results.append((sig, "DELIVERED", None))
        elif info_remaining == 0 and info_used >= _BUDGET_INFO:
            results.append((sig, "SUPPRESSED", "budget_cap_info"))
        else:
            results.append((sig, "SUPPRESSED", "outscored"))

    return results
```

> **Note on `demoted_from` precision:** Within an intra-batch run where `ar_remaining > 0` but is exhausted mid-loop, signals at index `>= ar_remaining` are beaten by intra-batch peers → `"outscored"`. Signals where `ar_remaining == 0` on entry are budget-capped from a prior run → `"budget_cap_ar"`. The same logic applies for INFO / `"budget_cap_info"`.

**Error handling pattern** (`discovery_agent.py` lines 49–51 — early return on empty input):
```python
if not signals:
    return []
```

No try/except needed — `route_signals()` is pure logic with one DB read. If `count_delivered_today()` raises, let it propagate to the caller (job-level error handling).

---

### `src/signal_system/router/__init__.py` (package init)

**Analog:** `src/signal_system/discovery/__init__.py` (lines 1–3)

**Exact pattern to copy:**
```python
from .alert_router import route_signals

__all__ = ["route_signals"]
```

---

### `src/signal_system/state/repository.py` — MODIFY (repository, CRUD)

**Analog:** self — existing `insert_signal()` (lines 125–158) and `init_db()` (lines 38–122)

#### Modification 1 — `_ensure_column` call in `init_db()` (lines 83–90 pattern)

Add after the existing Phase 4 `_ensure_column` block (line 90):
```python
# Idempotent column additions to signals (Phase 5 schema extensions)
_ensure_column(cursor, "signals", "demoted_from", "TEXT")
```

Existing context to locate insert point (`repository.py` lines 88–91):
```python
        # Idempotent column additions to runs (Phase 4 schema extensions)
        _ensure_column(cursor, "runs", "tickers_scanned", "INTEGER")
        _ensure_column(cursor, "runs", "tickers_signaled", "INTEGER")

        # New tables (Phase 1)   ← insert the Phase 5 block BEFORE this line
```

#### Modification 2 — `insert_signal()` signature + body (lines 125–158 pattern)

Change the function signature from:
```python
def insert_signal(signal: Signal, routing_status: str | None = None) -> bool:
```
To (backward-compatible kwarg addition, D-18):
```python
def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,
) -> bool:
```

Add `demoted_from` to the INSERT column list and values tuple. Current INSERT (`repository.py` lines 135–154):
```python
        cursor.execute("""
            INSERT OR IGNORE INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                score, routing_status, signal_price_snapshot, model_version,
                thesis_version_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
```

New INSERT after modification:
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
            demoted_from,               # Phase 5 addition (D-11)
        ))
```

---

### `tests/test_alert_router.py` (test, request-response)

**Analog:** `tests/test_discovery_agent.py`

**Imports pattern** (`test_discovery_agent.py` lines 1–15):
```python
"""Tests for the Alert Router — route_signals() covering ROUT-01..ROUT-05.

Tests T-AR-01 through T-AR-XX per D-19 test scenarios.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from signal_system.state import repository
from signal_system.router import route_signals
from signal_system.models import Signal, compute_alert_id
```

**`db` fixture pattern** (`test_discovery_agent.py` lines 28–32 — monkeypatches `DB_PATH`, calls `init_db()`):
```python
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"
```

**Signal factory helper** (new, analogous to `_q()` and `_news()` helpers in test_discovery_agent.py lines 19–25):
```python
DATE_ISO = "2026-05-16"

def _sig(
    ticker: str = "AAPL",
    score: float = 75.0,
    severity: str = "INFORMATIONAL",
    agent: str = "test_agent",
) -> Signal:
    """Build a minimal valid Signal for routing tests."""
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc),
        alert_id=compute_alert_id(ticker, DATE_ISO, "test_rule", agent),
        title=f"{ticker}: test signal",
    )
```

**Monkeypatching `count_delivered_today` pattern** (`test_discovery_agent.py` lines 58–60 — patch at module path where it's used):
```python
# Patch the function where the router imports it
with patch("signal_system.router.alert_router.repository") as mock_repo:
    mock_repo.count_delivered_today.return_value = {"ACTION_REQUIRED": 0, "INFORMATIONAL": 0}
    results = route_signals([sig])
```

Or using `monkeypatch` attribute-style (preferred for fixtures):
```python
def test_empty_budget(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    results = route_signals([])
    assert results == []
```

**Key test structure** (D-19 scenarios, following T-01..T-18 numbering convention):
```python
# T-AR-01: 5 AR signals → 1 DELIVERED (highest score, alpha tiebreak), 4 SUPPRESSED
def test_ar_budget_one_winner(monkeypatch):
    ...

# T-AR-02: Mixed batch (2 AR + 5 INFO) → 1 AR DELIVERED + 3 INFO DELIVERED + 3 SUPPRESSED
def test_mixed_batch_allocation(monkeypatch):
    ...

# T-AR-03: DB already has 1 AR → new AR → budget_cap_ar
def test_cross_run_ar_full(db, monkeypatch):
    ...

# T-AR-04: Equal scores, different tickers → alphabetical winner deterministic
def test_tiebreak_alphabetical(monkeypatch):
    ...

# T-AR-05: Empty input → []
def test_empty_input(monkeypatch):
    ...

# T-AR-06: MONITORING signal raises ValueError
def test_monitoring_raises(monkeypatch):
    with pytest.raises(ValueError, match="MONITORING"):
        route_signals([_sig(severity="MONITORING")])

# T-AR-07: demoted_from values are correct for each suppression reason
def test_demoted_from_reason_codes(monkeypatch):
    ...
```

**Assert pattern** (`test_discovery_agent.py` lines 62–67 — unpack and assert per field):
```python
delivered = [(s, rs, dmf) for s, rs, dmf in results if rs == "DELIVERED"]
suppressed = [(s, rs, dmf) for s, rs, dmf in results if rs == "SUPPRESSED"]
assert len(delivered) == 1
assert delivered[0][0].ticker == "AAPL"
assert suppressed[0][2] == "outscored"
```

---

## Shared Patterns

### DB Fixture (monkeypatched `DB_PATH`)
**Source:** `tests/test_discovery_agent.py` lines 28–32
**Apply to:** All tests that need DB state (cross-run budget tests use real SQLite via fixture)
```python
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"
```

### `_ensure_column` Idempotent Migration
**Source:** `src/signal_system/state/repository.py` lines 26–35
**Apply to:** Every new column added in `init_db()` — never ALTER TABLE directly
```python
def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, type_def: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
```

### `_connect()` / `conn.close()` in finally
**Source:** `src/signal_system/state/repository.py` lines 19–23, 125–158
**Apply to:** Any new DB-touching function in `repository.py`
```python
conn = _connect()
try:
    cursor = conn.cursor()
    # ... SQL ...
    conn.commit()
    return result
finally:
    conn.close()
```

### Keyword-Only Backward-Compatible Kwarg Addition
**Source:** `repository.py` line 125 — `routing_status: str | None = None` was added the same way
**Apply to:** `insert_signal()` when adding `demoted_from`
```python
def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,   # ← add as trailing optional kwarg
) -> bool:
```

### `from __future__ import annotations`
**Source:** `discovery_agent.py` line 7, `models.py` line 7
**Apply to:** `alert_router.py` — needed for `list[tuple[...]]` return type annotation in Python 3.12
```python
from __future__ import annotations
```

### Logging setup
**Source:** `discovery_agent.py` line 18
**Apply to:** `alert_router.py`
```python
logger = logging.getLogger(__name__)
```

---

## No Analog Found

All files have close analogs. No gaps.

---

## Metadata

**Analog search scope:** `src/signal_system/`, `tests/`
**Files read:** `discovery_agent.py`, `discovery/__init__.py`, `test_discovery_agent.py`, `repository.py`, `models.py`, `CLAUDE.md`
**Pattern extraction date:** 2026-05-16
