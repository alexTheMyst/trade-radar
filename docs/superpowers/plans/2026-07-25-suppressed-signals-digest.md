# Suppressed Signals Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the router's suppression and demotion decisions visible in the digest — a new "Suppressed" section with score/reason/HELD detail, demotion surfacing on delivered lines, and score-delta annotations — without changing which signals actually get delivered, demoted, or suppressed.

**Architecture:** The router (`route_signals()`) already makes the correct delivered/demoted/suppressed decisions; it just doesn't record *why* a suppression happened in a form the digest can use. This plan adds a `suppression_reason` column and tuple field (distinct from the existing `demoted_from` field), then extends the shared `render_digest()` in `jobs/common.py` to render that detail. Both `discovery.py` and `news_morning.py` consume the same `render_digest()`, so the change lands in both digests from one implementation.

**Tech Stack:** Python 3.12+, stdlib `sqlite3`, pytest — no new dependencies.

## Global Constraints

- All SQLite access goes through `src/signal_system/state/repository.py` — no raw SQL anywhere else (CLAUDE.md).
- SQLite schema changes use the existing idempotent `_ensure_column()` helper, not a literal `ALTER TABLE` migration script.
- **Router routing decisions do not change.** This plan only adds a `suppression_reason` label on top of whatever the router already decided. Confirmed with the user: the cross-run AR/INFO cap-exhaustion path currently suppresses directly without attempting demotion first (unlike the in-batch path), and that inconsistency is explicitly *not* being fixed here — matches the spec's own "this does not raise caps or change policy, only visibility" framing.
- Exactly two suppression reasons exist: `outscored` and `cap_exhausted`. The router's existing internal reasons `budget_cap_ar` and `budget_cap_info` both collapse into `cap_exhausted`; the existing `outscored` reason is unchanged in meaning, just relocated to its own field.
- `demoted_from` (severity a signal was demoted *from*, e.g. `"ACTION_REQUIRED"`) and `suppression_reason` (why a signal was cut, e.g. `"cap_exhausted"`) are two distinct concepts and must never share a column or tuple slot again.
- Score-delta ("prior scan" comparison) is computed as the most recent prior signal for the same `(ticker, agent)` pair, regardless of how many days back — confirmed with the user, since Discovery only rescans non-core tickers every ~3 days and a strict "yesterday" comparison would show `NEW` most of the time.
- Score-delta display is scoped to the new Suppressed section only — confirmed with the user. Delivered-alert lines are not changed to show a score/delta column. Demotion surfacing (Requirement C) and the HELD flag (Requirement D) are separate requirements from the score-delta (Requirement E) and DO apply to delivered lines regardless.
- HELD flag source is the existing `get_core_holdings()` in `src/signal_system/data/universe.py` (already filters `core_holding=true`, excludes K-1 ETFs) — no new CSV parsing needed.
- `daily_close.py` is out of scope: it emits a single fixed INFORMATIONAL signal and never calls `route_signals()`, so there is nothing to suppress.

---

## File Structure

| File | Action | Responsibility |
|------|--------|-----------------|
| `src/signal_system/state/repository.py` | Modify | Add `suppression_reason` column; extend `insert_signal()`; add `get_previous_score()` |
| `src/signal_system/router/alert_router.py` | Modify | Return 4-tuples with `suppression_reason`; make `BUDGET_AR`/`BUDGET_INFO` public constants |
| `src/signal_system/router/__init__.py` | Modify | Export `BUDGET_AR`, `BUDGET_INFO` alongside `route_signals` |
| `src/signal_system/jobs/common.py` | Modify | `RoutedSignal` type gains `suppression_reason`; `persist_routed_signals()` passthrough; new `compute_score_deltas()`; `render_digest()` rewritten (demotion surfacing, HELD flags, Suppressed section) |
| `src/signal_system/jobs/discovery.py` | Modify | Wire `routed_signals`/`held_tickers`/`score_deltas` into `render_digest()` |
| `src/signal_system/jobs/news_morning.py` | Modify | Same wiring |
| `tests/test_smoke.py` | Modify | Schema test covers `suppression_reason`; new insert-persistence test |
| `tests/test_alert_router.py` | Modify | Rewrite tuple assertions for the 4-tuple shape; consolidate `budget_cap_ar`/`budget_cap_info` assertions into `cap_exhausted` |
| `tests/test_job_orchestration.py` | Modify | Update `route_signals`/`render_digest` mocks for the new shapes/signature; add tests for score deltas, demotion surfacing, HELD flags, and the Suppressed section (unit + end-to-end) |

---

### Task 1: Add `suppression_reason` column and thread it through `insert_signal()`

**Files:**
- Modify: `src/signal_system/state/repository.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: `repository.insert_signal(signal, routing_status=None, demoted_from=None, suppression_reason=None) -> bool` (new optional 4th parameter)

- [ ] **Step 1: Write the failing schema test**

In `tests/test_smoke.py`, extend the column list inside `test_init_db_idempotent_and_new_schema` (around line 236):

```python
    for col in (
        "routing_status",
        "signal_price_snapshot",
        "model_version",
        "thesis_version_hash",
        "demoted_from",
        "suppression_reason",
    ):
        assert col in col_names, f"Column {col!r} missing from signals"
```

Then add a new test directly after `test_insert_signal_persists_demoted_from`:

```python
def test_insert_signal_persists_suppression_reason(tmp_path, monkeypatch):
    """insert_signal() persists suppression_reason distinctly from demoted_from."""
    from datetime import datetime, timezone
    from signal_system.models import Signal, compute_alert_id

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    alert_id = compute_alert_id("NVDA", "2026-05-15", "router_test", "TEST")
    signal = Signal(
        ticker="NVDA",
        score=74.0,
        severity="INFORMATIONAL",
        agent="TEST",
        timestamp=datetime.now(timezone.utc),
        alert_id=alert_id,
        title="Suppression persistence test",
    )

    inserted = repository.insert_signal(
        signal,
        routing_status="SUPPRESSED",
        suppression_reason="cap_exhausted",
    )

    assert inserted is True
    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute(
        "SELECT routing_status, demoted_from, suppression_reason FROM signals WHERE alert_id=?",
        (alert_id,),
    ).fetchone()
    conn.close()

    assert row == ("SUPPRESSED", None, "cap_exhausted")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -k "suppression_reason or new_schema" -v`
Expected: FAIL — `suppression_reason` column does not exist; `insert_signal()` raises `TypeError: unexpected keyword argument 'suppression_reason'`.

- [ ] **Step 3: Add the column and extend `insert_signal()`**

In `src/signal_system/state/repository.py`, add the column inside `init_db()` next to the other Phase 5 additions (after line 112's `_ensure_column(cursor, "signals", "demoted_from", "TEXT")`):

```python
        _ensure_column(cursor, "signals", "demoted_from", "TEXT")
        _ensure_column(cursor, "signals", "suppression_reason", "TEXT")
```

Update `insert_signal()`'s signature and body:

```python
def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,
    suppression_reason: str | None = None,
) -> bool:
    """Insert a Signal into the database using INSERT OR IGNORE semantics.

    Returns:
        True if the signal was newly inserted.
        False if a signal with the same alert_id already existed (idempotent rerun).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                score, routing_status, signal_price_snapshot, model_version,
                thesis_version_hash, demoted_from, direction, pillar, suppression_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            demoted_from,
            signal.direction,
            signal.pillar,
            suppression_reason,
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -k "suppression_reason or new_schema" -v`
Expected: PASS

- [ ] **Step 5: Run the full test file to check for regressions**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS (all tests, including the untouched `test_insert_signal_persists_demoted_from`)

- [ ] **Step 6: Commit**

```bash
git add src/signal_system/state/repository.py tests/test_smoke.py
git commit -m "feat: add suppression_reason column distinct from demoted_from"
```

---

### Task 2: Add `get_previous_score()` for score-delta lookups

**Files:**
- Modify: `src/signal_system/state/repository.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing new (reads existing `signals` table)
- Produces: `repository.get_previous_score(ticker: str, agent: str, before: datetime) -> float | None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_smoke.py`, after the suppression_reason test from Task 1:

```python
def test_get_previous_score_returns_none_when_no_prior_row(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    assert repository.get_previous_score("AAPL", "discovery_agent", datetime.now(timezone.utc)) is None


def test_get_previous_score_finds_most_recent_row_across_any_gap(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from signal_system.models import Signal, compute_alert_id

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    three_days_ago = datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 5, 18, 16, 0, tzinfo=timezone.utc)
    today = datetime(2026, 5, 19, 16, 0, tzinfo=timezone.utc)

    repository.insert_signal(
        Signal(
            ticker="AAPL", score=40.0, severity="INFORMATIONAL", agent="discovery_agent",
            timestamp=three_days_ago,
            alert_id=compute_alert_id("AAPL", "2026-05-16", "discovery", "discovery_agent"),
            title="AAPL: Discovery score 40",
        ),
        routing_status="DELIVERED",
    )
    repository.insert_signal(
        Signal(
            ticker="AAPL", score=45.0, severity="INFORMATIONAL", agent="discovery_agent",
            timestamp=yesterday,
            alert_id=compute_alert_id("AAPL", "2026-05-18", "discovery", "discovery_agent"),
            title="AAPL: Discovery score 45",
        ),
        routing_status="SUPPRESSED",
    )

    # Most recent prior row wins (45.0, not 40.0), even though the gap is 1 day
    # here and would be 3 days for a rotated non-core ticker.
    assert repository.get_previous_score("AAPL", "discovery_agent", today) == 45.0


def test_get_previous_score_ignores_monitoring_and_other_ticker_or_agent(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from signal_system.models import Signal, compute_alert_id

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    earlier = datetime(2026, 5, 18, 16, 0, tzinfo=timezone.utc)
    today = datetime(2026, 5, 19, 16, 0, tzinfo=timezone.utc)

    repository.insert_signal(
        Signal(
            ticker="AAPL", score=None, severity="MONITORING", agent="discovery_agent",
            timestamp=earlier,
            alert_id=compute_alert_id("AAPL", "2026-05-18", "monitoring", "discovery_agent"),
            title="[parse_failure] AAPL",
        ),
        routing_status="MONITORING",
    )
    repository.insert_signal(
        Signal(
            ticker="MSFT", score=99.0, severity="INFORMATIONAL", agent="discovery_agent",
            timestamp=earlier,
            alert_id=compute_alert_id("MSFT", "2026-05-18", "discovery", "discovery_agent"),
            title="MSFT: Discovery score 99",
        ),
        routing_status="DELIVERED",
    )
    repository.insert_signal(
        Signal(
            ticker="AAPL", score=88.0, severity="INFORMATIONAL", agent="news_classifier",
            timestamp=earlier,
            alert_id=compute_alert_id("AAPL", "2026-05-18", "news", "news_classifier"),
            title="AAPL: news",
        ),
        routing_status="DELIVERED",
    )

    assert repository.get_previous_score("AAPL", "discovery_agent", today) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -k get_previous_score -v`
Expected: FAIL with `AttributeError: module 'signal_system.state.repository' has no attribute 'get_previous_score'`

- [ ] **Step 3: Implement `get_previous_score()`**

Add to `src/signal_system/state/repository.py`, after `count_delivered_today()`:

```python
def get_previous_score(ticker: str, agent: str, before: datetime) -> float | None:
    """Return the most recent score for (ticker, agent) strictly before *before*.

    Looks back across any gap — Discovery's ~3-day rotation means the "prior
    scan" is often not literally yesterday. Excludes MONITORING rows (no
    comparable score, mirrors get_recent_signals()'s exclusion).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT score FROM signals
            WHERE ticker = ? AND agent = ? AND timestamp < ?
              AND routing_status IN ('DELIVERED', 'SUPPRESSED')
              AND score IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (ticker, agent, before.isoformat()),
        )
        row = cursor.fetchone()
        return float(row[0]) if row is not None else None
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -k get_previous_score -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/state/repository.py tests/test_smoke.py
git commit -m "feat: add get_previous_score() for score-delta lookups"
```

---

### Task 3: Router records `suppression_reason` as a distinct 4th tuple field

**Files:**
- Modify: `src/signal_system/router/alert_router.py`
- Modify: `src/signal_system/router/__init__.py`
- Test: `tests/test_alert_router.py` (full rewrite of tuple-shape assertions)

**Interfaces:**
- Consumes: nothing new
- Produces: `route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None, str | None]]` — `(signal, routing_status, demoted_from, suppression_reason)`. `demoted_from` is set for any signal (delivered or suppressed) that was demoted from a higher severity; `suppression_reason` is set only for SUPPRESSED signals, to `"outscored"` or `"cap_exhausted"`. **Amended post-merge (final whole-branch review):** these two fields are NOT mutually exclusive — a demoted-then-suppressed signal correctly carries both non-`None`, so the digest never loses the fact that a signal was originally ACTION_REQUIRED. The original Task 3 text below still describes the two fields as mutually exclusive; that constraint was relaxed in the final-review fix wave (see git history).
- Produces: `BUDGET_AR: int = 1`, `BUDGET_INFO: int = 3` (renamed from `_BUDGET_AR`/`_BUDGET_INFO`, now public — needed by `jobs/common.py` to render `"(AR 1/1)"` / `"(INFO 3/3)"` without duplicating the cap values)

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/test_alert_router.py`:

```python
"""Tests for the Alert Router — route_signals() covering ROUT-01..ROUT-05.

Tests T-AR-01 through T-AR-10 per D-19 test scenarios and purity guard.
"""
from datetime import datetime, timedelta, timezone

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.router import route_signals
from signal_system.router.alert_router import BUDGET_AR, BUDGET_INFO
from signal_system.state import repository

DATE_ISO = "2026-05-16"
_TS = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc)


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
        timestamp=_TS,
        alert_id=compute_alert_id(ticker, DATE_ISO, "test_rule", agent),
        title=f"{ticker}: test signal",
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_budget_constants_are_public():
    """BUDGET_AR/BUDGET_INFO are public so jobs/common.py can render cap fractions."""
    assert BUDGET_AR == 1
    assert BUDGET_INFO == 3


# ---------------------------------------------------------------------------
# T-AR-07: empty input
# ---------------------------------------------------------------------------

def test_empty_input(monkeypatch):
    """route_signals([]) returns [] without touching the DB."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    assert route_signals([]) == []


# ---------------------------------------------------------------------------
# T-AR-06: MONITORING raises ValueError
# ---------------------------------------------------------------------------

def test_monitoring_raises():
    """MONITORING signals must never enter the router."""
    sig = _sig(severity="MONITORING")
    with pytest.raises(ValueError, match="MONITORING"):
        route_signals([sig])


# ---------------------------------------------------------------------------
# T-AR-08: DELIVERED signals always have demoted_from=None and suppression_reason=None
# ---------------------------------------------------------------------------

def test_delivered_demoted_from_and_suppression_reason_are_none(monkeypatch):
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    sig = _sig(ticker="AAPL", score=80.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "DELIVERED"
    assert dmf is None
    assert reason is None


# ---------------------------------------------------------------------------
# T-AR-10: route_signals is pure and does not write to the DB
# ---------------------------------------------------------------------------

def test_route_signals_is_pure_no_db_writes(monkeypatch):
    """route_signals() must not call insert_signal or perform any DB writes."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})

    def fail_insert(*args, **kwargs):
        raise AssertionError("route_signals() must not call insert_signal()")

    monkeypatch.setattr(repository, "insert_signal", fail_insert)

    sig = _sig(ticker="AAPL", score=80.0, severity="INFORMATIONAL")
    results = route_signals([sig])

    assert results == [(sig, "DELIVERED", None, None)]


# ---------------------------------------------------------------------------
# T-AR-01: 5 AR signals → 1 DELIVERED (highest score), 4 SUPPRESSED (outscored)
# ---------------------------------------------------------------------------

def test_ar_budget_one_winner(monkeypatch):
    """AR cap=1: highest score wins AR, next 3 demoted to INFO (compete for INFO slots), lowest suppressed."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    signals = [
        _sig(ticker=f"T{i}", score=float(score), severity="ACTION_REQUIRED")
        for i, score in enumerate([50, 60, 70, 80, 90])
    ]
    results = route_signals(signals)
    assert len(results) == 5

    delivered = [t for t in results if t[1] == "DELIVERED"]
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert len(delivered) == 4  # 1 AR + 3 demoted-to-INFO
    assert delivered[0][0].score == 90.0  # AR winner
    assert delivered[0][0].severity == "ACTION_REQUIRED"
    assert delivered[0][2] is None  # demoted_from
    assert delivered[0][3] is None  # suppression_reason
    # Top 3 demoted AR signals (80, 70, 60) win INFO slots
    info_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "INFORMATIONAL"]
    assert len(info_delivered) == 3
    assert sorted(s.score for s in info_delivered) == [60.0, 70.0, 80.0]
    assert all(
        dmf == "ACTION_REQUIRED" and reason is None
        for _, _, dmf, reason in delivered
        if dmf is not None
    )
    assert len(suppressed) == 1
    assert suppressed[0][0].score == 50.0
    assert suppressed[0][2] is None  # demoted_from
    # All 4 non-winning AR signals (80/70/60/50) get demoted into the INFO pool
    # (ar_remaining=1 > 0, so the cap_exhausted branch never fires here). The
    # lowest of those 4 then loses the 3-slot INFO competition to the other 3 —
    # a genuine head-to-head loss against real, contested INFO slots, so it's
    # "outscored", not "cap_exhausted" (same principle as test_mixed_batch_allocation).
    assert suppressed[0][3] == "outscored"


# ---------------------------------------------------------------------------
# T-AR-04: equal scores → alphabetical tiebreak is deterministic
# ---------------------------------------------------------------------------

def test_tiebreak_alphabetical(monkeypatch):
    """Equal-score AR signals: alphabetically first (AAPL) wins AR, MSFT demoted to INFO."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})
    msft = _sig(ticker="MSFT", score=75.0, severity="ACTION_REQUIRED", agent="a1")
    aapl = Signal(
        ticker="AAPL", score=75.0, severity="ACTION_REQUIRED", agent="a2",
        timestamp=_TS,
        alert_id=compute_alert_id("AAPL", DATE_ISO, "test_rule", "a2"),
        title="AAPL: test signal",
    )
    results = route_signals([msft, aapl])
    delivered = [t for t in results if t[1] == "DELIVERED"]

    assert len(delivered) == 2  # AAPL AR + MSFT demoted to INFO
    # AAPL wins AR (alphabetical)
    assert delivered[0][0].ticker == "AAPL"
    assert delivered[0][0].severity == "ACTION_REQUIRED"
    assert delivered[0][2] is None
    # MSFT demoted to INFO
    assert delivered[1][0].ticker == "MSFT"
    assert delivered[1][0].severity == "INFORMATIONAL"
    assert delivered[1][2] == "ACTION_REQUIRED"
    assert delivered[1][3] is None


# ---------------------------------------------------------------------------
# T-AR-02: mixed batch — 2 AR + 5 INFO → 1 AR + 3 INFO DELIVERED, 3 SUPPRESSED
# ---------------------------------------------------------------------------

def test_mixed_batch_allocation(monkeypatch):
    """Mixed severity batch: 1 AR + 3 INFO slots; demoted AR enters INFO pool."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {})

    ar_signals = [
        _sig(ticker="AR1", score=85.0, severity="ACTION_REQUIRED", agent="ag1"),
        Signal(
            ticker="AR2", score=75.0, severity="ACTION_REQUIRED", agent="ag2",
            timestamp=_TS,
            alert_id=compute_alert_id("AR2", DATE_ISO, "test_rule", "ag2"),
            title="AR2: test",
        ),
    ]
    info_signals = [
        Signal(
            ticker=f"I{i}", score=float(s), severity="INFORMATIONAL", agent="ag3",
            timestamp=_TS,
            alert_id=compute_alert_id(f"I{i}", DATE_ISO, "test_rule", "ag3"),
            title=f"I{i}: test",
        )
        for i, s in enumerate([95, 88, 70, 60, 50])
    ]
    results = route_signals(ar_signals + info_signals)
    assert len(results) == 7

    delivered = [t for t in results if t[1] == "DELIVERED"]
    suppressed = [t for t in results if t[1] == "SUPPRESSED"]

    assert len(delivered) == 4  # 1 AR (AR1) + 3 INFO (I0:95, I1:88, AR2:75)
    assert len(suppressed) == 3  # I2:70, I3:60, I4:50

    ar_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "ACTION_REQUIRED"]
    info_delivered = [s for s, rs, dmf, reason in delivered if s.severity == "INFORMATIONAL"]
    assert len(ar_delivered) == 1
    assert ar_delivered[0].score == 85.0
    assert len(info_delivered) == 3
    assert sorted(s.score for s in info_delivered) == [75.0, 88.0, 95.0]

    # AR2 got demoted to INFO and delivered with demoted_from, no suppression_reason
    demoted_delivered = [t for t in delivered if t[2] == "ACTION_REQUIRED"]
    assert len(demoted_delivered) == 1
    assert demoted_delivered[0][0].ticker == "AR2"
    assert demoted_delivered[0][0].score == 75.0
    assert demoted_delivered[0][3] is None

    # Suppressed INFO signals lost the head-to-head competition for the 3 INFO
    # slots that DID exist this batch — that's "outscored", not "cap_exhausted".
    suppressed_info = [t for t in suppressed if t[0].severity == "INFORMATIONAL"]
    assert all(dmf is None for _, _, dmf, _reason in suppressed_info)
    assert all(reason == "outscored" for _, _, _dmf, reason in suppressed_info)
    assert sorted(s.score for s, _, _, _ in suppressed_info) == [50.0, 60.0, 70.0]


# ---------------------------------------------------------------------------
# T-AR-03: cross-run — DB already has 1 AR DELIVERED → new AR → cap_exhausted
# ---------------------------------------------------------------------------

def test_cross_run_ar_full(db, monkeypatch):
    """No eviction: if DB has 1 AR DELIVERED today, new AR gets cap_exhausted."""
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York"))
    prior_sig = Signal(
        ticker="PRIOR", score=90.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=today_et,
        alert_id=compute_alert_id("PRIOR", today_et.date().isoformat(), "r", "ag"),
        title="prior",
    )
    repository.insert_signal(prior_sig, routing_status="DELIVERED")

    new_sig = Signal(
        ticker="NEW", score=95.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=today_et,
        alert_id=compute_alert_id("NEW", today_et.date().isoformat(), "r2", "ag"),
        title="new",
    )
    results = route_signals([new_sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "SUPPRESSED"
    assert dmf is None
    assert reason == "cap_exhausted"


# ---------------------------------------------------------------------------
# T-AR-05: cross-run — INFO budget full → cap_exhausted
# ---------------------------------------------------------------------------

def test_cross_run_info_full(monkeypatch):
    """If DB has 3 INFO DELIVERED, new INFO gets cap_exhausted."""
    monkeypatch.setattr(repository, "count_delivered_today", lambda: {"INFORMATIONAL": 3})
    sig = _sig(ticker="LATE", score=99.0, severity="INFORMATIONAL")
    results = route_signals([sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "SUPPRESSED"
    assert dmf is None
    assert reason == "cap_exhausted"


# ---------------------------------------------------------------------------
# T-AR-09: ET midnight reset — yesterday's DELIVERED signals don't count today
# ---------------------------------------------------------------------------

def test_et_midnight_reset(db):
    """Signals from yesterday's ET date don't count against today's budget (ROUT-04)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    yesterday_et = datetime.now(et) - timedelta(days=1)

    yesterday_sig = Signal(
        ticker="YEST", score=90.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=yesterday_et,
        alert_id=compute_alert_id("YEST", yesterday_et.date().isoformat(), "r", "ag"),
        title="yesterday",
    )
    repository.insert_signal(yesterday_sig, routing_status="DELIVERED")

    # count_delivered_today() uses today's ET date — yesterday's signal should NOT count
    today_counts = repository.count_delivered_today()
    assert today_counts.get("ACTION_REQUIRED", 0) == 0, (
        f"Yesterday's signal leaked into today's budget: {today_counts}"
    )

    # Therefore a new AR signal today should be DELIVERED
    today_sig = Signal(
        ticker="TODAY", score=85.0, severity="ACTION_REQUIRED", agent="ag",
        timestamp=datetime.now(et),
        alert_id=compute_alert_id("TODAY", datetime.now(et).date().isoformat(), "r2", "ag"),
        title="today",
    )
    results = route_signals([today_sig])
    assert len(results) == 1
    _, rs, dmf, reason = results[0]
    assert rs == "DELIVERED", f"Expected DELIVERED but got {rs} (dmf={dmf}, reason={reason})"
    assert dmf is None
    assert reason is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_alert_router.py -v`
Expected: FAIL — `ImportError: cannot import name 'BUDGET_AR'`, and tuple-unpacking mismatches once that import is stubbed out.

- [ ] **Step 3: Update `alert_router.py`**

Replace the full contents of `src/signal_system/router/alert_router.py`:

```python
"""Alert Router — enforces daily budget caps and slot competition.

Pure logic: reads count_delivered_today() once, runs severity-first slot competition
with deterministic tiebreak, returns (signal, routing_status, demoted_from,
suppression_reason) tuples. Does NOT write to DB. Caller (Phase 6 job) handles
insert_signal() and email.
"""
from __future__ import annotations

import dataclasses
import logging

from signal_system.models import Signal
from signal_system.state import repository

logger = logging.getLogger(__name__)

# Hard-coded daily budget caps (ROUT-01 / D-01)
BUDGET_AR: int = 1
BUDGET_INFO: int = 3


def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None, str | None]]:
    """Route a batch of signals against today's delivery budget.

    Returns a list of (signal, routing_status, demoted_from, suppression_reason)
    tuples, one per input signal. routing_status is 'DELIVERED' or 'SUPPRESSED'.

    demoted_from is set only for DELIVERED signals that were demoted from a
    higher severity (currently only ACTION_REQUIRED -> INFORMATIONAL).
    suppression_reason is set only for SUPPRESSED signals, to 'outscored'
    (lost a head-to-head slot competition within this batch) or
    'cap_exhausted' (the severity's daily budget was already full — either
    from earlier today or after in-batch demotion still left no room). The
    two fields are mutually exclusive.

    Outscored ACTION_REQUIRED signals are demoted to INFORMATIONAL and
    compete for INFO slots, consistent with the core design: "others demote."
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
    ar_remaining = max(0, BUDGET_AR - ar_used)
    info_remaining = max(0, BUDGET_INFO - info_used)

    results: list[tuple[Signal, str, str | None, str | None]] = []

    # --- Severity-first slot competition (D-04) ---
    ar_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )
    info_signals = sorted(
        [s for s in signals if s.severity == "INFORMATIONAL"],
        key=lambda s: (-(s.score or 0.0), s.ticker or ""),
    )

    # Track AR signals demoted to INFO so they carry demoted_from="ACTION_REQUIRED"
    demoted_ids: set[str] = set()
    demoted_info: list[Signal] = []

    for i, sig in enumerate(ar_signals):
        if i < ar_remaining:
            results.append((sig, "DELIVERED", None, None))
        elif ar_remaining == 0 and ar_used >= BUDGET_AR:
            results.append((sig, "SUPPRESSED", None, "cap_exhausted"))
        else:
            demoted = dataclasses.replace(sig, severity="INFORMATIONAL")
            demoted_ids.add(demoted.alert_id)
            demoted_info.append(demoted)

    # Merge demoted AR signals into the INFO pool for slot competition
    if demoted_info:
        info_signals = sorted(
            info_signals + demoted_info,
            key=lambda s: (-(s.score or 0.0), s.ticker or ""),
        )

    for i, sig in enumerate(info_signals):
        is_demoted = sig.alert_id in demoted_ids
        if i < info_remaining:
            dmf = "ACTION_REQUIRED" if is_demoted else None
            results.append((sig, "DELIVERED", dmf, None))
        elif info_remaining == 0 and info_used >= BUDGET_INFO:
            results.append((sig, "SUPPRESSED", None, "cap_exhausted"))
        else:
            results.append((sig, "SUPPRESSED", None, "outscored"))

    return results
```

Update `src/signal_system/router/__init__.py`:

```python
from .alert_router import BUDGET_AR, BUDGET_INFO, route_signals

__all__ = ["route_signals", "BUDGET_AR", "BUDGET_INFO"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_alert_router.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/router/alert_router.py src/signal_system/router/__init__.py tests/test_alert_router.py
git commit -m "feat: router records suppression_reason as a distinct field from demoted_from"
```

---

### Task 4: `jobs/common.py` — thread `suppression_reason` through `RoutedSignal` and `persist_routed_signals()`

**Files:**
- Modify: `src/signal_system/jobs/common.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: `route_signals()` output from Task 3 — `tuple[Signal, str, str | None, str | None]`
- Produces: `RoutedSignal = tuple[Signal, str, str | None, str | None]`; `persist_routed_signals(routed_signals: list[RoutedSignal]) -> PersistenceSummary` (unchanged public shape, now passes `suppression_reason` to `insert_signal()`)

- [ ] **Step 1: Write the failing test**

In `tests/test_job_orchestration.py`, replace `test_routed_persistence_stores_every_tuple_with_demotions`:

```python
def test_routed_persistence_stores_every_tuple_with_demotions_and_suppression_reasons():
    from signal_system.jobs.common import persist_routed_signals

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    demoted = _sig(ticker="MSFT", severity="INFORMATIONAL", score=0.71)
    suppressed = _sig(ticker="SNOW", severity="INFORMATIONAL", score=0.60)

    with patch("signal_system.state.repository.insert_signal") as mock_insert:
        summary = persist_routed_signals(
            [
                (delivered, "DELIVERED", None, None),
                (demoted, "DELIVERED", "ACTION_REQUIRED", None),
                (suppressed, "SUPPRESSED", None, "outscored"),
            ]
        )

    assert summary.delivered_signals == [delivered, demoted]
    assert summary.status_counts == {"DELIVERED": 2, "SUPPRESSED": 1, "MONITORING": 0}
    mock_insert.assert_has_calls(
        [
            call(delivered, routing_status="PENDING", demoted_from=None, suppression_reason=None),
            call(demoted, routing_status="PENDING", demoted_from="ACTION_REQUIRED", suppression_reason=None),
            call(suppressed, routing_status="SUPPRESSED", demoted_from=None, suppression_reason="outscored"),
        ],
        any_order=True,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_job_orchestration.py -k stores_every_tuple -v`
Expected: FAIL — `ValueError: not enough values to unpack` in `persist_routed_signals()` (still expects 3-tuples), and the mock assertion expects a `suppression_reason=` kwarg that isn't passed yet.

- [ ] **Step 3: Update `persist_routed_signals()`**

In `src/signal_system/jobs/common.py`, change the type alias and function body:

```python
RoutedSignal = tuple[Signal, str, str | None, str | None]
```

```python
def persist_routed_signals(routed_signals: list[RoutedSignal]) -> PersistenceSummary:
    """Persist routed tuples and return to-be-delivered signals plus normalized counts.

    Signals that should be DELIVERED are persisted with routing_status='PENDING'
    so a failed Telegram send does not leave phantom DELIVERED rows in the DB.
    Caller must call confirm_delivered_signals() after a successful send to flip
    PENDING → DELIVERED.
    """
    status_counts = {"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0}
    pending_signals: list[Signal] = []

    for signal, routing_status, demoted_from, suppression_reason in routed_signals:
        db_status = routing_status
        if routing_status == "DELIVERED":
            db_status = "PENDING"
        inserted = repository.insert_signal(
            signal,
            routing_status=db_status,
            demoted_from=demoted_from,
            suppression_reason=suppression_reason,
        )
        status_counts[routing_status] = status_counts.get(routing_status, 0) + 1
        if routing_status == "DELIVERED" and inserted:
            pending_signals.append(signal)

    return PersistenceSummary(
        delivered_signals=pending_signals,
        status_counts=status_counts,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_job_orchestration.py -k stores_every_tuple -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/common.py tests/test_job_orchestration.py
git commit -m "feat: persist_routed_signals passes suppression_reason to insert_signal"
```

---

### Task 5: `compute_score_deltas()` — score vs. most recent prior scan

**Files:**
- Modify: `src/signal_system/jobs/common.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: `repository.get_previous_score()` from Task 2; `RoutedSignal` from Task 4
- Produces: `compute_score_deltas(routed_signals: list[RoutedSignal]) -> dict[str, str]` — maps `signal.alert_id` to `"+7"` / `"-3"` / `"NEW"`. Skips signals with `ticker is None` or `score is None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_job_orchestration.py` (needs the existing `db` fixture, `_sig` helper, and `Signal`/`compute_alert_id`/`datetime`/`ZoneInfo` imports already present at the top of the file):

```python
def test_compute_score_deltas_new_when_no_prior_signal(db):
    from signal_system.jobs.common import compute_score_deltas

    sig = _sig(ticker="AAPL", agent="discovery_agent", score=76.0)
    deltas = compute_score_deltas([(sig, "SUPPRESSED", None, "cap_exhausted")])

    assert deltas == {sig.alert_id: "NEW"}


def test_compute_score_deltas_formats_signed_difference(db):
    from signal_system.jobs.common import compute_score_deltas

    earlier = Signal(
        ticker="AAPL", score=45.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 17, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-17", "discovery", "discovery_agent"),
        title="AAPL: Discovery score 45",
    )
    repository.insert_signal(earlier, routing_status="DELIVERED")

    later = Signal(
        ticker="AAPL", score=76.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-19", "discovery", "discovery_agent"),
        title="AAPL: Discovery score 76",
    )

    deltas = compute_score_deltas([(later, "DELIVERED", None, None)])

    assert deltas == {later.alert_id: "+31"}


def test_compute_score_deltas_formats_negative_difference(db):
    from signal_system.jobs.common import compute_score_deltas

    earlier = Signal(
        ticker="RTX", score=78.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 17, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-17", "discovery", "discovery_agent"),
        title="RTX: Discovery score 78",
    )
    repository.insert_signal(earlier, routing_status="SUPPRESSED")

    later = Signal(
        ticker="RTX", score=69.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-19", "discovery", "discovery_agent"),
        title="RTX: Discovery score 69",
    )

    deltas = compute_score_deltas([(later, "SUPPRESSED", None, "outscored")])

    assert deltas == {later.alert_id: "-9"}


def test_compute_score_deltas_skips_signals_without_ticker_or_score(db):
    from signal_system.jobs.common import compute_score_deltas

    no_ticker = Signal(
        ticker=None, score=50.0, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id(None, "2026-05-19", "r1", "news_classifier"),
        title="untagged",
    )
    no_score = Signal(
        ticker="MSFT", score=None, severity="MONITORING", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("MSFT", "2026-05-19", "r2", "news_classifier"),
        title="no score",
    )

    deltas = compute_score_deltas([
        (no_ticker, "DELIVERED", None, None),
        (no_score, "MONITORING", None, None),
    ])

    assert deltas == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_orchestration.py -k compute_score_deltas -v`
Expected: FAIL with `ImportError: cannot import name 'compute_score_deltas'`

- [ ] **Step 3: Implement `compute_score_deltas()`**

Add to `src/signal_system/jobs/common.py`, after `persist_routed_signals()`:

```python
def compute_score_deltas(routed_signals: list[RoutedSignal]) -> dict[str, str]:
    """Return {alert_id: '+7' | '-3' | 'NEW'} vs each ticker+agent's prior scan.

    "Prior scan" is the most recent earlier signal for the same (ticker, agent)
    pair, regardless of gap — Discovery only rescans non-core tickers every
    ~3 days, so a strict "yesterday" comparison would show NEW most of the time.
    """
    deltas: dict[str, str] = {}
    for signal, _routing_status, _demoted_from, _reason in routed_signals:
        if signal.score is None or not signal.ticker:
            continue
        previous = repository.get_previous_score(signal.ticker, signal.agent, signal.timestamp)
        if previous is None:
            deltas[signal.alert_id] = "NEW"
        else:
            deltas[signal.alert_id] = f"{signal.score - previous:+.0f}"
    return deltas
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_orchestration.py -k compute_score_deltas -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/common.py tests/test_job_orchestration.py
git commit -m "feat: add compute_score_deltas() for prior-scan comparison"
```

---

### Task 6: `render_digest()` — surface demotions and HELD flag on delivered lines

**Files:**
- Modify: `src/signal_system/jobs/common.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: `RoutedSignal` from Task 4
- Produces: `render_digest(*, job_name, scanned_tickers, routed_signals: list[RoutedSignal], held_tickers: set[str], status_counts: dict[str, int]) -> DigestPayload` — signature change: `delivered_signals: list[Signal]` is replaced by `routed_signals: list[RoutedSignal]`; new `held_tickers` parameter.
- Produces (private helper): `_delivered_header_line(signal, demoted_from, ar_winner, held_tickers) -> str`

This task changes `render_digest()`'s signature but does **not** yet add the Suppressed section or score deltas — those are Task 7. `status_counts` keeps working exactly as before (it still separately carries the `MONITORING` count, which never appears in `routed_signals` since MONITORING bypasses the router entirely).

- [ ] **Step 1: Write the failing tests**

In `tests/test_job_orchestration.py`, replace the two existing `render_digest` tests:

```python
def test_shared_digest_details_only_delivered_and_counts_non_delivered():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        routed_signals=[(delivered, "DELIVERED", None, None)],
        held_tickers=set(),
        status_counts={"DELIVERED": 1, "SUPPRESSED": 2, "MONITORING": 1},
    )

    assert "News Morning" in payload.subject
    assert "Scanned 3 tickers, 1 alert" in payload.body
    assert "AAPL: important update" in payload.body
    assert "AAPL detailed body" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 1" in payload.body


def test_shared_digest_zero_alert_confirmation():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=4,
        routed_signals=[],
        held_tickers=set(),
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 3},
    )

    assert "Scanned 4 tickers, 0 alerts" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 3" in payload.body
```

Add three new tests directly after them:

```python
def test_render_digest_surfaces_demotion_with_outscored_winner():
    from signal_system.jobs.common import render_digest

    winner = _sig(ticker="SCHD", severity="ACTION_REQUIRED", score=83.0)
    demoted = _sig(ticker="AAPL", severity="INFORMATIONAL", score=81.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[
            (winner, "DELIVERED", None, None),
            (demoted, "DELIVERED", "ACTION_REQUIRED", None),
        ],
        held_tickers=set(),
        status_counts={"DELIVERED": 2, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert (
        "AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED, outscored by SCHD 83)"
        in payload.body
    )


def test_render_digest_demotion_without_batch_winner_omits_outscored_detail():
    from signal_system.jobs.common import render_digest

    demoted = _sig(ticker="AAPL", severity="INFORMATIONAL", score=81.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(demoted, "DELIVERED", "ACTION_REQUIRED", None)],
        held_tickers=set(),
        status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED)" in payload.body
    assert "outscored by" not in payload.body


def test_render_digest_tags_held_tickers_on_delivered_lines():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="NVDA", severity="ACTION_REQUIRED", score=88.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=10,
        routed_signals=[(delivered, "DELIVERED", None, None)],
        held_tickers={"NVDA"},
        status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "NVDA — ACTION_REQUIRED — HELD" in payload.body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_orchestration.py -k "shared_digest or render_digest_surfaces or render_digest_demotion or render_digest_tags" -v`
Expected: FAIL — `TypeError: render_digest() got an unexpected keyword argument 'routed_signals'`

- [ ] **Step 3: Rewrite `render_digest()`**

In `src/signal_system/jobs/common.py`, replace `render_digest()` and add the new helper:

```python
def render_digest(
    *,
    job_name: str,
    scanned_tickers: int,
    routed_signals: list[RoutedSignal],
    held_tickers: set[str],
    status_counts: dict[str, int],
) -> DigestPayload:
    """Render the plain-text digest required by the live jobs."""
    delivered = [
        (s, dmf) for s, rs, dmf, _reason in routed_signals if rs == "DELIVERED"
    ]
    ar_winner = next(
        (s for s, _dmf in delivered if s.severity == "ACTION_REQUIRED"), None
    )

    delivered_count = status_counts.get("DELIVERED", len(delivered))
    suppressed_count = status_counts.get("SUPPRESSED", 0)
    monitoring_count = status_counts.get("MONITORING", 0)
    title = job_name.replace("-", " ").title()
    alert_label = "alert" if delivered_count == 1 else "alerts"

    lines = [
        f"Scanned {scanned_tickers} tickers, {delivered_count} {alert_label}",
        f"Suppressed: {suppressed_count}",
        f"Monitoring: {monitoring_count}",
    ]

    if delivered:
        lines.append("")
        lines.append("Delivered Alerts")
        lines.append("----------------")
        for signal, demoted_from in delivered:
            lines.append(_delivered_header_line(signal, demoted_from, ar_winner, held_tickers))
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines.append("")
        lines.append(f"Scanned {scanned_tickers} tickers, 0 alerts")

    subject = f"{title} Digest — {delivered_count} {alert_label}"
    return DigestPayload(
        subject=subject,
        body="\n".join(lines).strip(),
        status_counts={
            "DELIVERED": delivered_count,
            "SUPPRESSED": suppressed_count,
            "MONITORING": monitoring_count,
        },
    )


def _delivered_header_line(
    signal: Signal,
    demoted_from: str | None,
    ar_winner: Signal | None,
    held_tickers: set[str],
) -> str:
    header = f"{signal.ticker or 'UNKNOWN'} — {signal.severity}"
    if demoted_from is not None:
        if ar_winner is not None and ar_winner.alert_id != signal.alert_id:
            winner_score = (
                f"{ar_winner.score:.0f}" if ar_winner.score is not None else "N/A"
            )
            header += (
                f" (demoted from {demoted_from}, outscored by "
                f"{ar_winner.ticker or 'UNKNOWN'} {winner_score})"
            )
        else:
            header += f" (demoted from {demoted_from})"
    if (signal.ticker or "") in held_tickers:
        header += " — HELD"
    return header
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_orchestration.py -k "shared_digest or render_digest_surfaces or render_digest_demotion or render_digest_tags" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/common.py tests/test_job_orchestration.py
git commit -m "feat: render_digest surfaces demotions and HELD flag on delivered lines"
```

---

### Task 7: `render_digest()` — add the Suppressed section (score, reason, cap fraction, HELD, delta)

**Files:**
- Modify: `src/signal_system/jobs/common.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: `BUDGET_AR`, `BUDGET_INFO` from `signal_system.router` (Task 3); `compute_score_deltas()` output from Task 5
- Produces: `render_digest(..., score_deltas: dict[str, str])` — adds the required `score_deltas` keyword parameter and renders a `"Suppressed (N)"` section after the delivered-alerts block, only when `N > 0`.

Suppressed line format (combines spec Sections A, B, and E): `{ticker} — {score}[ ({delta})] — {severity_would_have_been} — {reason}[ — HELD]`, where a `cap_exhausted` reason additionally shows the cap fraction, e.g. `cap_exhausted (INFO 3/3)` or `cap_exhausted (AR 1/1)`. Example: `NVDA — 74 (+12) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_job_orchestration.py`, and update the two Task-6 tests plus the three demotion/HELD tests to pass `score_deltas={}` (required keyword, no default):

Update `test_shared_digest_details_only_delivered_and_counts_non_delivered`, `test_shared_digest_zero_alert_confirmation`, `test_render_digest_surfaces_demotion_with_outscored_winner`, `test_render_digest_demotion_without_batch_winner_omits_outscored_detail`, and `test_render_digest_tags_held_tickers_on_delivered_lines` by adding `score_deltas={},` to each `render_digest(...)` call (alongside the existing `held_tickers=` line).

Then add:

```python
def test_render_digest_suppressed_section_cap_exhausted_with_held_and_delta(db):
    from signal_system.jobs.common import render_digest

    nvda = Signal(
        ticker="NVDA", score=74.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("NVDA", "2026-05-19", "discovery", "discovery_agent"),
        title="NVDA: Discovery score 74",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(nvda, "SUPPRESSED", None, "cap_exhausted")],
        held_tickers={"NVDA"},
        score_deltas={nvda.alert_id: "+12"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "Suppressed (1)" in payload.body
    assert "-" * len("Suppressed (1)") in payload.body
    assert "NVDA — 74 (+12) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD" in payload.body


def test_render_digest_suppressed_section_ar_cap_exhausted_fraction():
    from signal_system.jobs.common import render_digest

    sig = _sig(ticker="XOM", severity="ACTION_REQUIRED", score=91.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=5,
        routed_signals=[(sig, "SUPPRESSED", None, "cap_exhausted")],
        held_tickers=set(),
        score_deltas={sig.alert_id: "NEW"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "XOM — 91 (NEW) — ACTION_REQUIRED — cap_exhausted (AR 1/1)" in payload.body


def test_render_digest_suppressed_section_outscored_no_cap_suffix():
    from signal_system.jobs.common import render_digest

    rtx = Signal(
        ticker="RTX", score=69.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-19", "discovery", "discovery_agent"),
        title="RTX: Discovery score 69",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(rtx, "SUPPRESSED", None, "outscored")],
        held_tickers=set(),
        score_deltas={rtx.alert_id: "NEW"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "RTX — 69 (NEW) — INFORMATIONAL — outscored" in payload.body
    assert "cap_exhausted" not in payload.body


def test_render_digest_omits_suppressed_section_when_none():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="discovery",
        scanned_tickers=5,
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "Suppressed (" not in payload.body


def test_render_digest_suppressed_section_sorted_by_score_descending():
    from signal_system.jobs.common import render_digest

    low = _sig(ticker="CDE", severity="INFORMATIONAL", score=61.0)
    high = _sig(ticker="NVDA", severity="INFORMATIONAL", score=74.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[
            (low, "SUPPRESSED", None, "cap_exhausted"),
            (high, "SUPPRESSED", None, "cap_exhausted"),
        ],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 0},
    )

    nvda_pos = payload.body.index("NVDA")
    cde_pos = payload.body.index("CDE")
    assert nvda_pos < cde_pos
```

Note: `test_render_digest_suppressed_section_ar_cap_exhausted_fraction` and the sorting test use the shared `_sig()` helper, which defaults `agent="news_classifier"` — that's fine here since `compute_score_deltas` isn't invoked by these tests (deltas are passed in directly).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_orchestration.py -k "render_digest" -v`
Expected: FAIL — `TypeError: render_digest() missing 1 required keyword-only argument: 'score_deltas'` on the updated Task-6 tests, and `TypeError` / missing-section assertions on the new tests.

- [ ] **Step 3: Add `score_deltas` and the Suppressed section**

In `src/signal_system/jobs/common.py`, add the import at the top:

```python
from signal_system.router import BUDGET_AR, BUDGET_INFO
```

Replace `render_digest()` and add `_suppressed_line()`:

```python
def render_digest(
    *,
    job_name: str,
    scanned_tickers: int,
    routed_signals: list[RoutedSignal],
    held_tickers: set[str],
    score_deltas: dict[str, str],
    status_counts: dict[str, int],
) -> DigestPayload:
    """Render the plain-text digest required by the live jobs."""
    delivered = [
        (s, dmf) for s, rs, dmf, _reason in routed_signals if rs == "DELIVERED"
    ]
    suppressed = [
        (s, reason) for s, rs, _dmf, reason in routed_signals if rs == "SUPPRESSED"
    ]
    ar_winner = next(
        (s for s, _dmf in delivered if s.severity == "ACTION_REQUIRED"), None
    )

    delivered_count = status_counts.get("DELIVERED", len(delivered))
    suppressed_count = status_counts.get("SUPPRESSED", len(suppressed))
    monitoring_count = status_counts.get("MONITORING", 0)
    title = job_name.replace("-", " ").title()
    alert_label = "alert" if delivered_count == 1 else "alerts"

    lines = [
        f"Scanned {scanned_tickers} tickers, {delivered_count} {alert_label}",
        f"Suppressed: {suppressed_count}",
        f"Monitoring: {monitoring_count}",
    ]

    if delivered:
        lines.append("")
        lines.append("Delivered Alerts")
        lines.append("----------------")
        for signal, demoted_from in delivered:
            lines.append(_delivered_header_line(signal, demoted_from, ar_winner, held_tickers))
            lines.append(signal.title)
            if signal.body:
                lines.append(signal.body)
            lines.append("")
    else:
        lines.append("")
        lines.append(f"Scanned {scanned_tickers} tickers, 0 alerts")

    if suppressed:
        header = f"Suppressed ({len(suppressed)})"
        lines.append("")
        lines.append(header)
        lines.append("-" * len(header))
        for signal, reason in sorted(suppressed, key=lambda item: -(item[0].score or 0.0)):
            lines.append(_suppressed_line(signal, reason, held_tickers, score_deltas))

    subject = f"{title} Digest — {delivered_count} {alert_label}"
    return DigestPayload(
        subject=subject,
        body="\n".join(lines).strip(),
        status_counts={
            "DELIVERED": delivered_count,
            "SUPPRESSED": suppressed_count,
            "MONITORING": monitoring_count,
        },
    )
```

```python
def _suppressed_line(
    signal: Signal,
    reason: str | None,
    held_tickers: set[str],
    score_deltas: dict[str, str],
) -> str:
    ticker = signal.ticker or "UNKNOWN"
    score_str = f"{signal.score:.0f}" if signal.score is not None else "N/A"
    delta = score_deltas.get(signal.alert_id)
    if delta:
        score_str += f" ({delta})"

    if reason == "cap_exhausted":
        cap_label = "AR" if signal.severity == "ACTION_REQUIRED" else "INFO"
        cap_value = BUDGET_AR if signal.severity == "ACTION_REQUIRED" else BUDGET_INFO
        reason_display = f"cap_exhausted ({cap_label} {cap_value}/{cap_value})"
    else:
        reason_display = reason or "unknown"

    line = f"{ticker} — {score_str} — {signal.severity} — {reason_display}"
    if ticker in held_tickers:
        line += " — HELD"
    return line
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_orchestration.py -k "render_digest or shared_digest" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/common.py tests/test_job_orchestration.py
git commit -m "feat: render_digest adds Suppressed section with reason, cap fraction, HELD, and delta"
```

---

### Task 8: Wire `discovery.py` into the new `render_digest()` signature

**Files:**
- Modify: `src/signal_system/jobs/discovery.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: `route_signals()` (Task 3), `compute_score_deltas()` (Task 5), `render_digest()` (Task 7), `get_core_holdings()` (existing, `src/signal_system/data/universe.py`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_job_orchestration.py`, update `test_discovery_routes_persists_and_sends_digest` to mock 4-tuples, control held tickers, and assert the Suppressed section appears end-to-end:

```python
def test_discovery_routes_persists_and_sends_digest(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    delivered_signal = _sig(
        ticker="AAPL",
        severity="ACTION_REQUIRED",
        agent="discovery_agent",
        score=88.0,
    )
    suppressed_signal = _sig(
        ticker="MSFT",
        severity="INFORMATIONAL",
        agent="discovery_agent",
        score=71.0,
    )

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL", "MSFT"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=["MSFT"]), \
         patch(
             "signal_system.jobs.discovery.score_universe",
             return_value=[delivered_signal, suppressed_signal],
         ), \
         patch(
             "signal_system.jobs.discovery.route_signals",
             return_value=[
                 (delivered_signal, "DELIVERED", None, None),
                 (suppressed_signal, "SUPPRESSED", None, "cap_exhausted"),
             ],
         ) as mock_route, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        discovery.run()

    mock_route.assert_called_once_with([delivered_signal, suppressed_signal])
    assert mock_send.call_count == 1
    body = mock_send.call_args.args[0]
    assert "Scanned 2 tickers, 1 alert" in body
    assert "AAPL: important update" in body
    assert "Suppressed: 1" in body
    assert "Monitoring: 0" in body
    assert "Suppressed (1)" in body
    assert "cap_exhausted (INFO 3/3)" in body
    assert "MSFT — 71" in body.split("Suppressed (1)")[1]
    assert "— HELD" in body.split("Suppressed (1)")[1]

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, routing_status, demoted_from, suppression_reason FROM signals ORDER BY ticker"
    ).fetchall()
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert rows == [
        ("AAPL", "DELIVERED", None, None),
        ("MSFT", "SUPPRESSED", None, "cap_exhausted"),
    ]
    assert status == ("success",)
```

Update `test_discovery_zero_alert_digest_even_when_score_returns_empty` to add the `get_core_holdings` patch:

```python
def test_discovery_zero_alert_digest_even_when_score_returns_empty(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=[]), \
         patch("signal_system.jobs.discovery.route_signals", return_value=[]) as mock_route, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        discovery.run()

    mock_route.assert_called_once_with([])
    assert mock_send.call_count == 1
    assert "Scanned 1 tickers, 0 alerts" in mock_send.call_args.args[0]
    assert "Suppressed: 0" in mock_send.call_args.args[0]
    assert "Monitoring: 0" in mock_send.call_args.args[0]

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert status == ("success",)
```

Update `test_discovery_fails_on_digest_count_mismatch` to use a 4-tuple and add the `get_core_holdings` patch:

```python
def test_discovery_fails_on_digest_count_mismatch(db):
    from signal_system.jobs import discovery
    from signal_system.jobs.common import DigestPayload

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    routed_signal = _sig(ticker="AAPL", agent="discovery_agent", score=88.0)

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=[routed_signal]), \
         patch(
             "signal_system.jobs.discovery.route_signals",
             return_value=[(routed_signal, "DELIVERED", None, None)],
         ), \
         patch(
             "signal_system.jobs.discovery.render_digest",
             return_value=DigestPayload(
                 subject="bad digest",
                 body="Scanned 1 tickers, 0 alerts",
                 status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0},
             ),
         ), \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        with pytest.raises(RuntimeError, match="Digest counts"):
            discovery.run()

    mock_send.assert_not_called()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert status == ("failed",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_orchestration.py -k discovery -v`
Expected: FAIL — `ImportError`/`AttributeError` patching `signal_system.jobs.discovery.get_core_holdings` (not imported yet), and `render_digest()` call in `discovery.py` still uses the old `delivered_signals=` keyword.

- [ ] **Step 3: Update `discovery.py`**

Replace the full contents of `src/signal_system/jobs/discovery.py`:

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.data.universe import (
    get_core_holdings,
    get_todays_universe,
    require_non_empty_universe,
)
from signal_system.delivery import telegram_sender
from signal_system.discovery.discovery_agent import score_universe
from signal_system.jobs.common import (
    PersistenceSummary,
    compute_score_deltas,
    confirm_delivered_signals,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
from signal_system.monitoring import heartbeat
from signal_system.router import route_signals
from signal_system.state import repository

_ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(_ET)


def _send_digest_once(*, subject: str, body: str) -> None:
    telegram_sender.send_message(f"{subject}\n\n{body}")


def run() -> None:
    run_id = repository.insert_run("discovery")
    try:
        with heartbeat.heartbeat():
            now_et = _now_et()
            tickers = require_non_empty_universe(get_todays_universe(), job="discovery")
            discovered_signals = score_universe(tickers, run_id, now_et.date().isoformat())

            routed = route_signals(discovered_signals)
            persistence_summary: PersistenceSummary = persist_routed_signals(routed)
            held_tickers = set(get_core_holdings())
            score_deltas = compute_score_deltas(routed)
            digest = render_digest(
                job_name="discovery",
                scanned_tickers=len(tickers),
                routed_signals=routed,
                held_tickers=held_tickers,
                score_deltas=score_deltas,
                status_counts=persistence_summary.status_counts,
            )
            validate_digest_payload(
                digest,
                scanned_tickers=len(tickers),
                expected_counts=persistence_summary.status_counts,
                delivered_signals=persistence_summary.delivered_signals,
            )
            _send_digest_once(subject=digest.subject, body=digest.body)
            confirm_delivered_signals(persistence_summary.delivered_signals)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_orchestration.py -k discovery -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/discovery.py tests/test_job_orchestration.py
git commit -m "feat: wire discovery job into Suppressed-section digest rendering"
```

---

### Task 9: Wire `news_morning.py` into the new `render_digest()` signature

**Files:**
- Modify: `src/signal_system/jobs/news_morning.py`
- Test: `tests/test_job_orchestration.py`

**Interfaces:**
- Consumes: same as Task 8; `get_core_holdings` is already imported in `news_morning.py`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_job_orchestration.py`, update `test_news_morning_parse_failure_monitoring_bypasses_router_and_persists`'s `route_signals` mock to a 4-tuple:

```python
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(delivered_signal, "DELIVERED", None, None)]) as mock_route, \
```

(only that one line changes inside the existing `with` block — the rest of the test body is unchanged.)

Update `test_news_morning_digest_counts_zero_alert_and_mismatch_guard`'s first `route_signals` mock:

```python
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "outscored")]), \
```

and add an assertion for the Suppressed section right after the existing `"Monitoring: 1" in mock_send.call_args.args[0]` line:

```python
    body = mock_send.call_args.args[0]
    assert "Suppressed (1)" in body
    assert "AAPL — 1 (NEW) — INFORMATIONAL — outscored — HELD" in body
```

(`_sig()`'s default `score=0.75` renders as `"1"` under `:.0f` formatting — standard rounding, not truncation, so 0.75 rounds up. This test's `get_core_holdings` mock returns `["AAPL"]`, so the HELD suffix applies, and this is a fresh DB fixture with no prior signal for the ticker, so `compute_score_deltas()` correctly reports `"NEW"`. This is an existing quirk of News Classifier's 0.0–1.0 confidence scale colliding with the `:.0f` format built for Discovery's 0–100 scale, called out in the Global Constraints — it does not need fixing here; the assertion documents the actual current behavior.)

Add a new end-to-end test after `test_discovery_routes_persists_and_sends_digest` (Task 8) verifying the full news-morning pipeline surfaces a HELD suppressed signal:

```python
def test_news_morning_suppressed_signal_shows_held_flag_end_to_end(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL", score=0.76)

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "cap_exhausted")]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    body = mock_send.call_args.args[0]
    assert "Suppressed (1)" in body
    assert "cap_exhausted (INFO 3/3)" in body
    assert "AAPL" in body.split("Suppressed (1)")[1]
    assert "— HELD" in body.split("Suppressed (1)")[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_orchestration.py -k news_morning -v`
Expected: FAIL — `render_digest()` in `news_morning.py` still calls the old signature (`delivered_signals=`), so `TypeError` on every news-morning test that reaches the digest step; the new end-to-end test fails outright.

- [ ] **Step 3: Update `news_morning.py`**

In `src/signal_system/jobs/news_morning.py`, add `compute_score_deltas` to the `jobs.common` import block:

```python
from signal_system.jobs.common import (
    PersistenceSummary,
    compute_score_deltas,
    confirm_delivered_signals,
    persist_routed_signals,
    render_digest,
    validate_digest_payload,
)
```

Replace the routing/digest section of `run()` (currently lines 230–239):

```python
            routed = route_signals(routable)
            persistence_summary: PersistenceSummary = persist_routed_signals(routed)
            status_counts = dict(persistence_summary.status_counts)
            status_counts["MONITORING"] += classifier_monitoring_count + len(overflow_items) + len(reconciled_losers)

            held_tickers = set(get_core_holdings())
            score_deltas = compute_score_deltas(routed)
            digest = render_digest(
                job_name="news-morning",
                scanned_tickers=len(tickers),
                routed_signals=routed,
                held_tickers=held_tickers,
                score_deltas=score_deltas,
                status_counts=status_counts,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_orchestration.py -k news_morning -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/signal_system/jobs/news_morning.py tests/test_job_orchestration.py
git commit -m "feat: wire news-morning job into Suppressed-section digest rendering"
```

---

### Task 10: Full regression pass

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: All tests pass (baseline was 136 passing before this plan; expect that plus every new test added in Tasks 1–9).

- [ ] **Step 2: Manually sanity-check the rendered format against the spec's example**

Run a short one-off script to eyeball the output shape (not committed — throwaway verification):

```bash
uv run python -c "
from datetime import datetime
from zoneinfo import ZoneInfo
from signal_system.models import Signal, compute_alert_id
from signal_system.jobs.common import render_digest

ts = datetime(2026, 7, 22, 16, 0, tzinfo=ZoneInfo('America/New_York'))
def sig(ticker, score, severity):
    return Signal(
        ticker=ticker, score=score, severity=severity, agent='discovery_agent',
        timestamp=ts, alert_id=compute_alert_id(ticker, '2026-07-22', 'discovery', 'discovery_agent'),
        title=f'{ticker}: Discovery score {score:.0f}',
    )

schd = sig('SCHD', 83.0, 'ACTION_REQUIRED')
aapl = sig('AAPL', 81.0, 'INFORMATIONAL')
snow = sig('SNOW', 76.0, 'INFORMATIONAL')
rtx = sig('RTX', 69.0, 'INFORMATIONAL')
nvda = sig('NVDA', 74.0, 'INFORMATIONAL')
cde = sig('CDE', 61.0, 'INFORMATIONAL')

routed = [
    (schd, 'DELIVERED', None, None),
    (aapl, 'DELIVERED', 'ACTION_REQUIRED', None),
    (snow, 'DELIVERED', None, None),
    (rtx, 'DELIVERED', None, None),
    (nvda, 'SUPPRESSED', None, 'cap_exhausted'),
    (cde, 'SUPPRESSED', None, 'cap_exhausted'),
]
payload = render_digest(
    job_name='discovery', scanned_tickers=23, routed_signals=routed,
    held_tickers={'NVDA', 'CDE'},
    score_deltas={nvda.alert_id: '+12', cde.alert_id: '-4'},
    status_counts={'DELIVERED': 4, 'SUPPRESSED': 2, 'MONITORING': 0},
)
print(payload.body)
"
```

Confirm the output shows `AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED, outscored by SCHD 83)` and a `Suppressed (2)` section with `NVDA — 74 (+12) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD` and `CDE — 61 (-4) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD` — matching the spec's motivating example.

- [ ] **Step 3: No commit for this task** — it is verification only. If Step 1 or Step 2 surfaces a defect, fix it under the task where the bug was introduced (amend that task's commit is not allowed per project convention — make a new commit) and re-run this task.

---

## Explicitly Out of Scope (per spec's "What this deliberately does NOT do")

- Raising `BUDGET_AR`/`BUDGET_INFO` caps — spec explicitly defers this to a post-hoc review after ~10 sessions of Suppressed-section data.
- Fixing the cross-run AR/INFO demote-before-suppress inconsistency — confirmed with the user as a separate, pre-existing behavior not addressed by this plan.
- `daily_close.py` — never routes through `route_signals()`, nothing to suppress.
- The stale "Phase A monitoring-only" line and the Java-vs-Python / 3-factor-vs-4-factor documentation discrepancies noted in the spec's "Two discrepancies" section — those are documentation cleanup items, not code changes, and out of scope for this plan.
