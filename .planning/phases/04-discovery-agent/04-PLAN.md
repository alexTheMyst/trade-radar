---
phase: 04-discovery-agent
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/signal_system/models.py
  - src/signal_system/state/repository.py
  - src/signal_system/data/finnhub_client.py
  - src/signal_system/discovery/__init__.py
  - src/signal_system/discovery/discovery_agent.py
  - tests/test_discovery_agent.py
autonomous: true
requirements: [DISC-01, DISC-02, DISC-03, DISC-04, DISC-05]

must_haves:
  truths:
    - "score_universe(['AAPL', 'MSFT'], run_id, date_iso) returns list[Signal] in Phase B with per-factor sub_scores"
    - "A ticker where fetch_quote() returns None (invalid dp, h<l, l<=0) is excluded — no Signal, not scored"
    - "h==l produces range_position=0.0, ticker is still scored (not excluded)"
    - "DISCOVERY_PHASE=A causes direct DB insert with routing_status='MONITORING', returns []"
    - "DISCOVERY_PHASE=B returns list[Signal], calls no insert"
    - "runs table has tickers_scanned and tickers_signaled populated after each call"
  artifacts:
    - path: "src/signal_system/discovery/discovery_agent.py"
      provides: "score_universe() public function"
      exports: ["score_universe"]
    - path: "src/signal_system/discovery/__init__.py"
      provides: "package entry point"
      contains: "from .discovery_agent import score_universe"
    - path: "tests/test_discovery_agent.py"
      provides: "18 tests covering all DISC requirements"
      contains: "test_score_computation"
  key_links:
    - from: "discovery_agent.py"
      to: "finnhub_client.fetch_quote"
      via: "direct import"
      pattern: "from signal_system.data.finnhub_client import fetch_quote"
    - from: "discovery_agent.py"
      to: "repository.insert_signal"
      via: "Phase A direct insert"
      pattern: "insert_signal(signal, routing_status=\"MONITORING\")"
    - from: "discovery_agent.py"
      to: "repository.update_run_counts"
      via: "called before every return"
      pattern: "update_run_counts(run_id, tickers_scanned, len(signals_emitted))"
---

# Phase 4: Discovery Agent — Plan

## Phase Goal

**As a** solo trading operator, **I want to** run `score_universe()` against today's rotation
partition and receive ranked `Signal` objects graded by momentum, volume, range, and news
activity, **so that** I can identify the highest-scoring tickers from the free-tier Finnhub
data without code changes to switch from calibration mode (Phase A) to live routing (Phase B).

---

## Requirements Coverage

| Requirement | Tasks That Cover It |
|-------------|---------------------|
| DISC-01 — 4 scoring factors (35/30/25/10) using free-tier endpoints | Wave 1: Task 1 (discovery_agent.py — `_rank_values`, `score_universe` scoring loop) |
| DISC-02 — score-floor guard: tickers missing required /quote data receive no score | Wave 0: Task 1 (fetch_quote validation); Wave 2: T-02, T-03, T-04 |
| DISC-03 — Phase A/B behavior via DISCOVERY_PHASE config only | Wave 1: Task 1 (phase branch in `score_universe`); Wave 2: T-07, T-08 |
| DISC-04 — Signal objects with per-factor sub_scores, never sends email | Wave 1: Task 1 (Signal construction with sub_scores dict); Wave 2: T-18 |
| DISC-05 — tickers_scanned + tickers_signaled written to runs table | Wave 0: Task 2 (repository changes); Wave 1: Task 1 (`update_run_counts` call); Wave 2: T-15 |

---

## Threat Model

| Threat | STRIDE | Mitigation | Code Location |
|--------|--------|------------|---------------|
| Ticker string injected from universe file used in Finnhub API call | Tampering | finnhub SDK escapes ticker in URL; no string interpolation into SQL | `fetch_quote()`, `_fetch_single_quote()` |
| Composite score NaN due to division in range_position | Tampering | h==l guard: set range_position=0.0 before ranking (D-07) | `discovery_agent.py` scoring loop |
| Phase A insert with attacker-controlled routing_status | Elevation | routing_status is hardcoded literal `"MONITORING"` in agent, not passed from input | `discovery_agent.py:score_universe` |
| SQL injection via ticker in UPDATE runs SET | Tampering | Parameterised query `?` placeholder in `update_run_counts` | `repository.py:update_run_counts` |
| Division by zero in `_rank_values` when n==1 | Denial of Service | Explicit `n==1` early return → all ranks=0.5 | `discovery_agent.py:_rank_values` |

---

## Source Audit

| Source | Item | Covered By |
|--------|------|-----------|
| GOAL | score_universe() importable, tested, returns Signal with sub-scores | Wave 1 Task 1 + Wave 2 |
| REQ DISC-01 | 4 factors at 35/30/25/10 via /quote + /company-news | Wave 1 Task 1 |
| REQ DISC-02 | Score-floor guard | Wave 0 Task 1 (fetch_quote) + Wave 1 Task 1 |
| REQ DISC-03 | Phase A/B config switch | Wave 1 Task 1 |
| REQ DISC-04 | Signal.sub_scores; no email | Wave 1 Task 1 |
| REQ DISC-05 | tickers_scanned/tickers_signaled in runs | Wave 0 Task 2 + Wave 1 Task 1 |
| CONTEXT D-01..D-04 | Factor definitions and weights | Wave 1 Task 1 |
| CONTEXT D-05..D-07 | Score-floor guard rules | Wave 0 Task 1 + Wave 1 Task 1 |
| CONTEXT D-08..D-09 | Severity thresholds as module constants | Wave 1 Task 1 |
| CONTEXT D-10..D-12 | Phase A/B wiring | Wave 1 Task 1 |
| CONTEXT D-13 | runs table columns + update_run_counts | Wave 0 Task 2 |
| CONTEXT D-14..D-18 | Module structure, alert_id, Signal fields | Wave 0 Task 2 (signal_price_snapshot) + Wave 1 Task 1 |
| CONTEXT D-19..D-22 | Data fetching via fetch_quote only (no candle) | Wave 0 Task 1 |
| DEFERRED | weight_version column | NOT in this plan — deferred to Phase 6/V2 per CONTEXT.md |

---

## Tasks

---

### Wave 0 — Prerequisites: Extend Existing Files

Wave 0 must complete before Wave 1. Two tasks are independent of each other and can run in parallel.

---

<task type="auto" tdd="true">
  <name>Wave 0 / Task 1: Add fetch_quote() to finnhub_client.py</name>
  <files>src/signal_system/data/finnhub_client.py</files>
  <behavior>
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":2.5,"v":300,"h":60.0,"l":40.0} → returns the dict unchanged
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":None,"v":300,"h":60.0,"l":40.0} → returns None (dp required)
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":2.5,"v":None,"h":60.0,"l":40.0} → returns None (v required)
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":2.5,"v":300,"h":40.0,"l":60.0} → returns None (h < l)
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":2.5,"v":300,"h":60.0,"l":0.0} → returns None (l <= 0)
    - fetch_quote("AAPL") where _fetch_single_quote returns {"c":50.0,"dp":2.5,"v":300,"h":50.0,"l":50.0} → returns dict (h==l is valid; range_position=0.0 is handled by caller, not here)
    - fetch_quote("AAPL") where _fetch_single_quote returns None (403/404) → returns None
  </behavior>
  <action>
    Add the following public function to `src/signal_system/data/finnhub_client.py` after the existing
    `fetch_quotes()` function (after line 94). Do NOT modify `_fetch_single_quote()` or `fetch_quotes()`.

    Function signature: `def fetch_quote(ticker: str) -> dict | None:`

    Implementation contract:
    1. Call `_fetch_single_quote(ticker)`. If it returns None, return None immediately.
    2. Extract: `dp = quote.get("dp")`, `v = quote.get("v")`, `h = quote.get("h", 0)`, `l = quote.get("l", 0)`.
    3. Score-floor guard (per D-05, D-07): if `dp is None or v is None or h < l or l <= 0`, call
       `logger.debug("Incomplete quote for %r — skipping", ticker)` and return None.
    4. Note: `h == l` is VALID — range_position=0.0 is computed by the caller, not rejected here.
       The guard only rejects `l <= 0` (zero/negative low price is invalid data).
    5. Return the full quote dict on success.

    Docstring: "Fetch and validate a single quote for Discovery Agent scoring. Returns the full
    quote dict (c, dp, v, h, l) if all required score-floor fields are present and valid, else None."
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && python -c "from signal_system.data.finnhub_client import fetch_quote; print('fetch_quote importable')"</automated>
  </verify>
  <done>
    fetch_quote is importable from signal_system.data.finnhub_client. Returns None for any of:
    dp=None, v=None, h&lt;l, l&lt;=0. Returns dict when h==l (flat day). Returns None when
    _fetch_single_quote returns None.
  </done>
</task>

---

<task type="auto" tdd="true">
  <name>Wave 0 / Task 2: Extend Signal dataclass + repository for Discovery</name>
  <files>
    src/signal_system/models.py,
    src/signal_system/state/repository.py
  </files>
  <behavior>
    Signal dataclass:
    - Signal(..., signal_price_snapshot=49.5) → signal.signal_price_snapshot == 49.5
    - Signal(...) with no signal_price_snapshot → signal.signal_price_snapshot is None (default)
    - Signal is still frozen (FrozenInstanceError on assignment)

    insert_signal:
    - insert_signal(signal) with no routing_status kwarg → DB row has routing_status=NULL (existing behavior preserved)
    - insert_signal(signal, routing_status="MONITORING") → DB row has routing_status="MONITORING"
    - signal.signal_price_snapshot=49.5 → DB row has signal_price_snapshot=49.5

    update_run_counts:
    - update_run_counts(run_id, 42, 7) → runs row for run_id has tickers_scanned=42, tickers_signaled=7
    - After init_db(), runs table has tickers_scanned and tickers_signaled columns

    init_db (runs columns):
    - Calling init_db() on an existing DB that lacks tickers_scanned → idempotently adds the column
  </behavior>
  <action>
    **1. src/signal_system/models.py — add signal_price_snapshot field to Signal**

    After `thesis_version_hash: str | None = None` (currently the last field, line 35), add:
    ```
    signal_price_snapshot: float | None = None
    ```
    This field has a default value so all existing Signal construction sites continue to compile.
    No other changes to models.py.

    **2. src/signal_system/state/repository.py — three changes:**

    **2a. init_db(): add tickers_scanned/tickers_signaled columns to runs table.**
    After the existing block of four `_ensure_column(cursor, "signals", ...)` calls (after line 86),
    add inside the same try block before `conn.commit()`:
    ```
    _ensure_column(cursor, "runs", "tickers_scanned", "INTEGER")
    _ensure_column(cursor, "runs", "tickers_signaled", "INTEGER")
    ```

    **2b. insert_signal(): add routing_status kwarg and wire signal_price_snapshot.**
    Change the function signature at line 121 from:
    ```
    def insert_signal(signal: Signal) -> bool:
    ```
    to:
    ```
    def insert_signal(signal: Signal, routing_status: str | None = None) -> bool:
    ```
    Update the INSERT VALUES tuple: replace the two `None,` hardcoded lines (currently lines 146–147)
    with:
    ```
    routing_status,                 # routing_status — caller passes "MONITORING" in Phase A
    signal.signal_price_snapshot,   # set by discovery agent at generation time
    ```
    No other changes to the INSERT statement — column order already matches.

    **2c. Add update_run_counts() function after the existing update_run() function.**
    Function signature: `def update_run_counts(run_id: str, tickers_scanned: int, tickers_signaled: int) -> None:`

    Implementation: open connection via `_connect()`, execute:
    ```sql
    UPDATE runs SET tickers_scanned = ?, tickers_signaled = ? WHERE run_id = ?
    ```
    with params `(tickers_scanned, tickers_signaled, run_id)`, commit, close in try/finally.
    Pattern identical to `update_run()` above it.
    Docstring: "Write tickers_scanned and tickers_signaled counts to the runs row. Call once per
    score_universe() invocation, before returning."
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && python -c "
from signal_system.models import Signal
import inspect
fields = {f.name for f in Signal.__dataclass_fields__.values()}
assert 'signal_price_snapshot' in fields, 'signal_price_snapshot missing from Signal'
from signal_system.state import repository
assert hasattr(repository, 'update_run_counts'), 'update_run_counts missing'
import inspect
sig = inspect.signature(repository.insert_signal)
assert 'routing_status' in sig.parameters, 'routing_status kwarg missing'
print('All Wave 0/Task 2 checks passed')
"
    </automated>
  </verify>
  <done>
    Signal.signal_price_snapshot field exists with default None. insert_signal() accepts
    routing_status kwarg (default None), uses it in INSERT, and uses signal.signal_price_snapshot
    for that column. update_run_counts() exists and updates the runs row. init_db() adds
    tickers_scanned and tickers_signaled columns idempotently.
  </done>
</task>

---

### Wave 1 — Discovery Package (depends on Wave 0)

---

<task type="auto" tdd="true">
  <name>Wave 1 / Task 1: Create discovery package — __init__.py and discovery_agent.py</name>
  <files>
    src/signal_system/discovery/__init__.py,
    src/signal_system/discovery/discovery_agent.py
  </files>
  <behavior>
    score_universe([]) → []
    score_universe(["A","B","C"], ...) with all valid quotes and Phase B → list[Signal] (no DB insert)
    score_universe(["A","B","C"], ...) with all valid quotes and Phase A → [], inserts each ≥60 signal with routing_status="MONITORING"
    A ticker where fetch_quote returns None is excluded from all ranking passes
    Composite score = 35*momentum_rank + 30*volume_rank + 25*range_rank + 10*news_rank
    score &lt; 60 → no Signal emitted (silent drop)
    score ≥ 80 → severity="ACTION_REQUIRED"
    60 ≤ score &lt; 80 → severity="INFORMATIONAL"
    Signal.agent == "discovery_agent"
    Signal.model_version is None
    Signal.thesis_version_hash is None
    Signal.signal_price_snapshot == quote["c"] for that ticker
    Signal.sub_scores keys == {"price_momentum", "volume_rank", "range_position", "news_activity"}
    Signal.alert_id == compute_alert_id(ticker, date_iso, "discovery", "discovery_agent")
    Signal.title == f"{ticker}: Discovery score {composite:.0f}"
    Signal.body starts with "weights=35/30/25/10"
    update_run_counts called exactly once per score_universe() call
    tickers_scanned == len(input tickers list), regardless of how many pass score-floor
    tickers_signaled == count of Signals with score ≥ 60
  </behavior>
  <action>
    **File 1: src/signal_system/discovery/__init__.py**
    Create with:
    ```
    from .discovery_agent import score_universe
    __all__ = ["score_universe"]
    ```

    **File 2: src/signal_system/discovery/discovery_agent.py**

    Module docstring: "Discovery Agent — scores rotation universe tickers via cross-sectional
    factor ranking. Uses /quote and /company-news (free-tier confirmed). Never sends email.
    Phase A: inserts directly to DB with routing_status='MONITORING'. Phase B: returns
    list[Signal] to caller."

    **Imports:**
    ```python
    from __future__ import annotations
    import logging
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from signal_system import config
    from signal_system.data.finnhub_client import fetch_quote, fetch_company_news
    from signal_system.models import Signal, compute_alert_id
    from signal_system.state import repository
    ```

    **Module-level constants** (per D-08, D-09 — must be at module level, not buried in logic):
    ```python
    logger = logging.getLogger(__name__)
    _ET = ZoneInfo("America/New_York")
    _W_MOMENTUM: float = 35.0
    _W_VOLUME:   float = 30.0
    _W_RANGE:    float = 25.0
    _W_NEWS:     float = 10.0
    SCORE_THRESHOLD_ACTION: float = 80.0
    SCORE_THRESHOLD_INFORM: float = 60.0
    ```

    **Private helper `_rank_values(values: dict[str, float]) -> dict[str, float]`:**
    - If values is empty → return {}
    - Sort items by `(-value, ticker)` — descending value, alphabetical tiebreak (per D-02)
    - n = len(sorted_items)
    - If n == 1 → return {ticker: 0.5} (per D-02 edge case)
    - Else → `{ticker: 1.0 - (i / (n - 1)) for i, (ticker, _) in enumerate(sorted_items)}`
    - Docstring: "Rank tickers 1.0 (top) to 0.0 (bottom), alphabetical tiebreak for equal values."

    **Public function `score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]`:**

    Docstring: "Score a list of tickers using cross-sectional factor ranking. Reads
    config.DISCOVERY_PHASE at call time (no module-level caching). Phase A: inserts signals
    directly with routing_status='MONITORING', returns []. Phase B: returns list[Signal].
    Always calls repository.update_run_counts() before returning."

    **Implementation (3-pass):**

    Pass 1 — Fetch:
    ```
    tickers_scanned = len(tickers)
    if not tickers:
        repository.update_run_counts(run_id, 0, 0)
        return []

    today = datetime.fromisoformat(date_iso).date()
    news_from = today - timedelta(days=7)

    raw_quotes: dict[str, dict] = {}
    raw_news_counts: dict[str, int] = {}

    for ticker in tickers:
        quote = fetch_quote(ticker)
        if quote is None:
            logger.debug("Skipping %r — quote invalid or unavailable", ticker)
            continue
        raw_quotes[ticker] = quote

        # news is optional per D-06; failure → 0 (conservative, no artificial boost)
        news = fetch_company_news(ticker, news_from, today)
        raw_news_counts[ticker] = len(news) if news else 0

    if not raw_quotes:
        repository.update_run_counts(run_id, tickers_scanned, 0)
        return []
    ```

    Pass 2 — Cross-sectional ranking (per D-02):
    ```
    # range_position: (c - l) / (h - l); h==l → 0.0 (per D-07, NOT skip)
    range_raw: dict[str, float] = {}
    for t, q in raw_quotes.items():
        h, l, c = q["h"], q["l"], q["c"]
        range_raw[t] = (c - l) / (h - l) if h != l else 0.0

    momentum_ranks = _rank_values({t: q["dp"] for t, q in raw_quotes.items()})
    volume_ranks   = _rank_values({t: float(q["v"]) for t, q in raw_quotes.items()})
    range_ranks    = _rank_values(range_raw)
    news_ranks     = _rank_values({t: float(raw_news_counts.get(t, 0)) for t in raw_quotes})
    ```

    Pass 3 — Score and emit:
    ```
    results: list[Signal] = []
    signals_emitted: list[Signal] = []
    timestamp = datetime.now(_ET)

    for ticker in raw_quotes:
        m_rank = momentum_ranks[ticker]
        v_rank = volume_ranks[ticker]
        r_rank = range_ranks[ticker]
        n_rank = news_ranks[ticker]

        composite = (
            _W_MOMENTUM * m_rank
            + _W_VOLUME  * v_rank
            + _W_RANGE   * r_rank
            + _W_NEWS    * n_rank
        )

        if composite < SCORE_THRESHOLD_INFORM:
            continue  # silent drop per D-08

        severity = (
            "ACTION_REQUIRED" if composite >= SCORE_THRESHOLD_ACTION
            else "INFORMATIONAL"
        )

        alert_id = compute_alert_id(ticker, date_iso, "discovery", "discovery_agent")

        signal = Signal(
            ticker=ticker,
            score=composite,
            severity=severity,
            agent="discovery_agent",
            timestamp=timestamp,
            alert_id=alert_id,
            title=f"{ticker}: Discovery score {composite:.0f}",
            body=(
                f"weights=35/30/25/10 "
                f"momentum={m_rank:.2f} volume={v_rank:.2f} "
                f"range={r_rank:.2f} news={n_rank:.2f}"
            ),
            sub_scores={
                "price_momentum": m_rank,
                "volume_rank":    v_rank,
                "range_position": r_rank,
                "news_activity":  n_rank,
            },
            model_version=None,
            thesis_version_hash=None,
            signal_price_snapshot=raw_quotes[ticker]["c"],
        )

        signals_emitted.append(signal)

        if config.DISCOVERY_PHASE == "A":
            repository.insert_signal(signal, routing_status="MONITORING")
        else:
            results.append(signal)

    repository.update_run_counts(run_id, tickers_scanned, len(signals_emitted))
    return results  # [] in Phase A, list[Signal] in Phase B
    ```

    **Important implementation notes:**
    - `config.DISCOVERY_PHASE` is read inside the loop/function body, NOT at module load time (per D-12)
    - `tickers_scanned` = len(input list), not len(raw_quotes); counts all fetch attempts
    - `tickers_signaled` = len(signals_emitted), counting both Phase A inserts and Phase B returns
    - Do NOT import or call anything related to email, SMTP, or the router
    - fetch_company_news signature: `fetch_company_news(ticker, from_date: date, to_date: date)`
      — pass `date` objects, not strings (see finnhub_client.py line 125)
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && python -c "
from signal_system.discovery import score_universe
import inspect
sig = inspect.signature(score_universe)
params = list(sig.parameters.keys())
assert params == ['tickers', 'run_id', 'date_iso'], f'Wrong params: {params}'
print('score_universe importable with correct signature')
"
    </automated>
  </verify>
  <done>
    score_universe is importable from signal_system.discovery. Module has _rank_values helper,
    SCORE_THRESHOLD_ACTION=80.0, SCORE_THRESHOLD_INFORM=60.0 constants, and the 3-pass
    implementation. discovery/__init__.py exports score_universe in __all__.
  </done>
</task>

---

### Wave 2 — Test Suite (depends on Wave 1)

---

<task type="auto">
  <name>Wave 2 / Task 1: Create tests/test_discovery_agent.py — all 18 tests</name>
  <files>tests/test_discovery_agent.py</files>
  <action>
    Create `tests/test_discovery_agent.py`. All 18 required tests must pass. Use `unittest.mock.patch`
    at `"signal_system.discovery.discovery_agent.fetch_quote"` and
    `"signal_system.discovery.discovery_agent.fetch_company_news"`. Mock repository via
    `monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")` + `repository.init_db()`
    for DB-touching tests.

    **Shared fixtures and helpers (place at top of file):**

    ```python
    import sqlite3
    from datetime import datetime, timezone
    from unittest.mock import patch, MagicMock, call
    import pytest
    from signal_system import config
    from signal_system.state import repository
    from signal_system.discovery import score_universe

    DATE_ISO = "2026-05-16"

    def _q(dp=5.0, v=300, c=50.0, h=60.0, l=40.0):
        """Build a valid quote dict."""
        return {"c": c, "dp": dp, "v": v, "h": h, "l": l, "o": 48.0, "pc": 49.0}

    def _news(n=2):
        return [{"headline": f"News {i}"} for i in range(n)]

    @pytest.fixture
    def db(tmp_path, monkeypatch):
        monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
        repository.init_db()
        return tmp_path / "test.db"
    ```

    **Test implementations — exact specifications:**

    **T-01: test_score_computation**
    Use 3 tickers A, B, C with the following quotes and news:
    - A: dp=10.0, v=300, c=50.0, h=60.0, l=40.0 (range=(50-40)/(60-40)=0.5), news=3
    - B: dp=5.0, v=200, c=48.0, h=50.0, l=40.0 (range=(48-40)/(50-40)=0.8), news=2
    - C: dp=1.0, v=100, c=41.0, h=50.0, l=40.0 (range=(41-40)/(50-40)=0.1), news=1

    With n=3 tickers, ranks are 1.0/0.5/0.0 (linear). Expected ranks:
    - momentum: A=1.0, B=0.5, C=0.0
    - volume: A=1.0, B=0.5, C=0.0
    - range: B=1.0(0.8), A=0.5(0.5), C=0.0(0.1)
    - news: A=1.0(3), B=0.5(2), C=0.0(1)

    Expected composites:
    - A: 35*1.0 + 30*1.0 + 25*0.5 + 10*1.0 = 87.5
    - B: 35*0.5 + 30*0.5 + 25*1.0 + 10*0.5 = 62.5
    - C: 0.0 (all bottom ranks → 0, no signal)

    Mock fetch_quote to return the quote dicts above (side_effect keyed by ticker).
    Mock fetch_company_news to return _news(3), _news(2), _news(1) respectively.
    monkeypatch config.DISCOVERY_PHASE = "B".
    Use db fixture (needs run_id via repository.insert_run).

    Assertions: len(signals) == 2. Signal for A: score == pytest.approx(87.5). Signal for B:
    score == pytest.approx(62.5). C not in [s.ticker for s in signals].

    **T-02: test_score_floor_invalid_quote (dp=None)**
    Tests fetch_quote() directly. Mock `_fetch_single_quote` (at
    `"signal_system.data.finnhub_client._fetch_single_quote"`) to return
    `{"c": 50.0, "dp": None, "v": 300, "h": 60.0, "l": 40.0}`.
    Call `fetch_quote("AAPL")` — assert result is None.

    **T-03: test_score_floor_null_quote**
    Mock `"signal_system.discovery.discovery_agent.fetch_quote"` to return None.
    monkeypatch config.DISCOVERY_PHASE = "B". Use db fixture.
    Call score_universe(["AAPL"], run_id, DATE_ISO) → assert result == [].

    **T-04: test_range_position_flat_day**
    One ticker with h==l (h=50.0, l=50.0, c=50.0, dp=5.0, v=200).
    Mock fetch_quote to return this dict. Mock fetch_company_news to return _news(1).
    monkeypatch config.DISCOVERY_PHASE = "B". Use db fixture.
    score_universe(["AAPL"], run_id, DATE_ISO) → single ticker, all ranks=0.5 →
    composite = 50.0 → below 60 → no signal. Assert result == [].
    Also verify: no exception raised (division-by-zero guard worked).
    To test range_position=0.0 is set correctly: add a second ticker with normal values so
    the flat-day ticker has a deterministic range rank. With 2 tickers:
    - AAPL: h==l=50.0, c=50.0, dp=5.0, v=200
    - MSFT: h=60.0, l=40.0, c=55.0, dp=3.0, v=150 (news=1 for both)
    Mock fetch_quote side_effect: AAPL→flat dict, MSFT→normal dict.
    MSFT should get range_rank=1.0, AAPL range_rank=0.0.
    Assert no exception raised. If MSFT scores ≥60, verify its sub_scores["range_position"] != AAPL's.

    **T-05: test_news_activity_missing (fetch_company_news returns None)**
    Mock fetch_quote to return valid quote for one ticker.
    Mock fetch_company_news to return None.
    monkeypatch config.DISCOVERY_PHASE = "B". Use db fixture.
    With 1 ticker: all ranks=0.5, composite=50.0 < 60 → no signal. Assert result == [].
    Also verify: no exception raised (len(None) guard worked — code uses `len(news) if news else 0`).

    **T-06: test_news_activity_empty (fetch_company_news returns [])**
    Same as T-05 but fetch_company_news returns []. Assert no exception. Result == [].

    **T-07: test_phase_a_inserts_monitoring**
    Need ≥1 ticker with score ≥60. Use 2 tickers HIGH and LOW:
    - HIGH: dp=10.0, v=200, c=55.0, h=60.0, l=40.0, news=2
    - LOW: dp=1.0, v=100, c=41.0, h=50.0, l=40.0, news=0
    With 2 tickers: ranks are 1.0 and 0.0. HIGH gets all top ranks → composite=100.0.
    monkeypatch config.DISCOVERY_PHASE = "A". Use db fixture.
    run_id = repository.insert_run("discovery")  # required — update_run_counts needs a real row
    Patch `"signal_system.state.repository.insert_signal"` as mock_insert.
    score_universe(["HIGH","LOW"], run_id, DATE_ISO) → returns [].
    Assert mock_insert.called == True.
    Assert mock_insert.call_args.kwargs["routing_status"] == "MONITORING"
    (or check call_args[1]["routing_status"] == "MONITORING").
    Assert return value == [].

    **T-08: test_phase_b_returns_signals**
    Same 2-ticker setup as T-07.
    monkeypatch config.DISCOVERY_PHASE = "B". Use db fixture.
    Patch `"signal_system.state.repository.insert_signal"` as mock_insert.
    result = score_universe(["HIGH","LOW"], run_id, DATE_ISO)
    Assert len(result) == 1 (only HIGH, score=100.0 ≥ 60).
    Assert result[0].ticker == "HIGH".
    Assert mock_insert.assert_not_called()  # NOTE: must use assert_not_called(), NOT not_called()

    **T-09: test_threshold_below_60_suppressed**
    1 ticker: all ranks=0.5 → composite=50.0 < 60.
    monkeypatch config.DISCOVERY_PHASE = "B". Use db fixture.
    Assert score_universe(["AAPL"], run_id, DATE_ISO) == [].

    **T-10: test_action_required_severity**
    2 tickers HIGH and LOW (same setup as T-07). Phase B.
    result = score_universe(["HIGH","LOW"], run_id, DATE_ISO)
    Signal for HIGH has score=100.0 → assert signal.severity == "ACTION_REQUIRED".

    **T-11: test_informational_severity**
    Use 3 tickers A (high), B (medium), C (low) from T-01 setup. Phase B.
    B has composite=62.5 → assert B signal.severity == "INFORMATIONAL".

    **T-12: test_cross_sectional_ranking_ties**
    2 tickers AAPL and BIDU, identical dp=5.0, but AAPL should rank above BIDU alphabetically
    (sort key: (-dp, ticker) → (-5.0, "AAPL") < (-5.0, "BIDU") → AAPL at index 0, rank=1.0).
    Use distinct values for other factors to ensure AAPL scores ≥60.
    Set: AAPL dp=5.0 v=200 h=60 l=40 c=50 news=2; BIDU dp=5.0 v=100 h=55 l=40 c=45 news=1.
    momentum tie → AAPL rank=1.0, BIDU rank=0.0.
    Phase B. Assert AAPL signal sub_scores["price_momentum"] == pytest.approx(1.0).
    Assert BIDU (if it appears) sub_scores["price_momentum"] == pytest.approx(0.0).

    **T-13: test_single_ticker_universe**
    1 ticker, valid quote. All ranks must equal 0.5. Composite = 50.0. No signal.
    Assert score_universe(["AAPL"], run_id, DATE_ISO) == [].
    To verify ranks directly: mock and check sub_scores if a signal were emitted.
    Alternative: use Phase A with mock insert, lower SCORE_THRESHOLD_INFORM temporarily
    by monkeypatching `signal_system.discovery.discovery_agent.SCORE_THRESHOLD_INFORM = 0.0`.
    Then assert the inserted signal's sub_scores values all == pytest.approx(0.5).

    **T-14: test_empty_universe**
    score_universe([], run_id, DATE_ISO) == [] without calling fetch_quote at all.
    Use db fixture. Mock fetch_quote as mock_fq. Assert mock_fq.assert_not_called().

    **T-15: test_update_run_counts**
    2 tickers HIGH and LOW from T-07 setup. Phase B. Use db fixture.
    run_id = repository.insert_run("discovery").
    score_universe(["HIGH", "LOW"], run_id, DATE_ISO).
    Query runs table directly: `conn = sqlite3.connect(db); row = conn.execute("SELECT tickers_scanned, tickers_signaled FROM runs WHERE run_id=?", (run_id,)).fetchone()`.
    Assert row[0] == 2 (both tickers attempted) and row[1] == 1 (only HIGH scored ≥60).

    **T-16: test_alert_id_determinism**
    Use Phase B with 2 tickers (HIGH/LOW from T-07 setup). Use db fixture.
    run_id1 = repository.insert_run("discovery")
    signals_run1 = score_universe(["HIGH", "LOW"], run_id1, DATE_ISO)
    run_id2 = repository.insert_run("discovery")
    signals_run2 = score_universe(["HIGH", "LOW"], run_id2, DATE_ISO)
    Assert signals_run1[0].alert_id == signals_run2[0].alert_id (same ticker + date → same SHA-256).
    Note: Phase B is used so no DB uniqueness constraint interferes with the second call.

    **T-17: test_signal_price_snapshot**
    Valid quote with c=53.75. Phase B. 2 tickers (to get ≥60 composite for HIGH ticker).
    Assert HIGH signal.signal_price_snapshot == pytest.approx(53.75).

    **T-18: test_sub_scores_dict**
    Valid 2-ticker run, Phase B. For the returned signal:
    Assert set(signal.sub_scores.keys()) == {"price_momentum", "volume_rank", "range_position", "news_activity"}.
    For each value v in signal.sub_scores.values(): assert 0.0 <= v <= 1.0.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && python -m pytest tests/test_discovery_agent.py -v --tb=short 2>&amp;1 | tail -30</automated>
  </verify>
  <done>
    All 18 tests pass. pytest exits 0. Each of T-01..T-18 is present in the output.
    No tests are skipped or marked xfail.
  </done>
</task>

---

### Wave 3 — Integration Smoke (depends on Wave 2)

---

<task type="auto">
  <name>Wave 3 / Task 1: Full pytest run + import smoke</name>
  <files></files>
  <action>
    Run the full test suite to confirm no regressions in existing tests. Then perform an import
    smoke check to confirm the discovery package is correctly installed in the project.

    **Step 1 — Full pytest:**
    ```
    cd /Users/alex/Documents/code/trading_agent
    python -m pytest tests/ -v --tb=short
    ```
    All pre-existing tests (test_smoke.py) must continue to pass. Failure in existing tests
    means a Wave 0 change broke something — fix before declaring Wave 3 done.

    **Step 2 — Import smoke:**
    ```python
    from signal_system.discovery import score_universe
    from signal_system.discovery.discovery_agent import (
        SCORE_THRESHOLD_ACTION,
        SCORE_THRESHOLD_INFORM,
        _rank_values,
    )
    assert SCORE_THRESHOLD_ACTION == 80.0
    assert SCORE_THRESHOLD_INFORM == 60.0
    # Verify _rank_values edge cases inline
    assert _rank_values({}) == {}
    assert _rank_values({"AAPL": 5.0}) == {"AAPL": 0.5}
    assert _rank_values({"AAPL": 5.0, "MSFT": 3.0}) == {"AAPL": 1.0, "MSFT": 0.0}
    # Tiebreak: alphabetical (AAPL before BIDU)
    result = _rank_values({"BIDU": 5.0, "AAPL": 5.0})
    assert result["AAPL"] == 1.0 and result["BIDU"] == 0.0
    print("Wave 3 smoke: PASSED")
    ```

    **Step 3 — DB column smoke:**
    ```python
    import tempfile, sqlite3
    from pathlib import Path
    from signal_system.state import repository
    with tempfile.TemporaryDirectory() as td:
        # Temporarily override DB_PATH (in-process only, don't patch module)
        original = repository.DB_PATH
        repository.DB_PATH = Path(td) / "smoke.db"
        repository.init_db()
        conn = sqlite3.connect(repository.DB_PATH)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        conn.close()
        repository.DB_PATH = original
    assert "tickers_scanned" in cols
    assert "tickers_signaled" in cols
    print("DB column smoke: PASSED")
    ```

    If any step fails, diagnose and fix before marking Wave 3 done.
  </action>
  <verify>
    <automated>cd /Users/alex/Documents/code/trading_agent && python -m pytest tests/ -v 2>&amp;1 | grep -E "passed|failed|error" | tail -5</automated>
  </verify>
  <done>
    Full pytest suite passes with 0 failures, 0 errors. Import smoke and DB column smoke
    both print "PASSED". score_universe is importable from signal_system.discovery.
  </done>
</task>

---

## Interface Context for Executor

The executor needs these interfaces — extracted from codebase, no exploration needed:

```python
# src/signal_system/models.py — after Wave 0/Task 2 (signal_price_snapshot added)
@dataclass(frozen=True, slots=True)
class Signal:
    ticker: str | None
    score: float | None
    severity: Severity                       # "ACTION_REQUIRED"|"INFORMATIONAL"|"MONITORING"
    agent: str
    timestamp: datetime
    alert_id: str
    title: str
    body: str | None = None
    sub_scores: dict[str, float] = field(default_factory=dict)
    model_version: str | None = None
    thesis_version_hash: str | None = None
    signal_price_snapshot: float | None = None   # ← added in Wave 0/Task 2

def compute_alert_id(ticker: str | None, date_iso: str, rule: str, agent: str) -> str: ...

# src/signal_system/state/repository.py — after Wave 0/Task 2
def insert_signal(signal: Signal, routing_status: str | None = None) -> bool: ...
def insert_run(job: str) -> str: ...          # returns run_id (UUID v4 str)
def update_run(run_id: str, status: str) -> None: ...
def update_run_counts(run_id: str, tickers_scanned: int, tickers_signaled: int) -> None: ...

# src/signal_system/data/finnhub_client.py — after Wave 0/Task 1
def fetch_quote(ticker: str) -> dict | None:
    """Returns {"c", "dp", "v", "h", "l", "o", "pc"} or None if score-floor fails."""

def fetch_company_news(ticker: str, from_date: date, to_date: date) -> list[dict]:
    """Returns list of news item dicts. Returns [] on any failure (never raises)."""

# src/signal_system/config.py — already wired
DISCOVERY_PHASE: str  # "A" or "B", default "A"
```

---

## UAT Checklist

Items to verify during `/gsd-verify-work 4`:

- [ ] `from signal_system.discovery import score_universe` imports without error
- [ ] `score_universe.__module__` == `"signal_system.discovery.discovery_agent"`
- [ ] Signal dataclass has `signal_price_snapshot` field (inspect via `Signal.__dataclass_fields__`)
- [ ] `insert_signal` accepts `routing_status` kwarg without TypeError
- [ ] `repository.update_run_counts` exists and is callable
- [ ] `fetch_quote` is importable from `signal_system.data.finnhub_client`
- [ ] `runs` table has `tickers_scanned` and `tickers_signaled` columns after `init_db()`
- [ ] `SCORE_THRESHOLD_ACTION == 80.0` and `SCORE_THRESHOLD_INFORM == 60.0` as module constants
- [ ] `_rank_values({"AAPL": 5.0})` returns `{"AAPL": 0.5}` (single-ticker edge case)
- [ ] `_rank_values({"BIDU": 5.0, "AAPL": 5.0})` returns AAPL=1.0 (alphabetical tiebreak)
- [ ] Phase A: `score_universe()` with valid tickers returns `[]` and inserts to DB
- [ ] Phase B: `score_universe()` with valid tickers returns `list[Signal]` with no DB insert
- [ ] Signal.body starts with `"weights=35/30/25/10"`
- [ ] Signal.sub_scores has exactly 4 keys: price_momentum, volume_rank, range_position, news_activity
- [ ] All 18 tests in test_discovery_agent.py pass
- [ ] All pre-existing tests in test_smoke.py still pass

---

## Success Criteria

1. `python -m pytest tests/ -v` exits 0, all tests pass (pre-existing + 18 new)
2. `from signal_system.discovery import score_universe` succeeds in a fresh Python process
3. Calling `score_universe(["AAPL", "MSFT"], run_id, "2026-05-16")` with mocked fetch_quote
   returning valid quotes and DISCOVERY_PHASE=B returns list[Signal] where each signal has
   `sub_scores` with 4 keys and `signal_price_snapshot` set to quote["c"]
4. Calling the same with DISCOVERY_PHASE=A returns [] and calls insert_signal with routing_status="MONITORING"
5. A ticker where fetch_quote returns None is absent from all results — no partial score, no exception
6. `repository.update_run_counts` updates the correct runs row with tickers_scanned and tickers_signaled

---

<execution_context>
@~/.copilot/get-shit-done/workflows/execute-plan.md
@~/.copilot/get-shit-done/templates/summary.md
</execution_context>

<output>
Create `.planning/phases/04-discovery-agent/04-01-SUMMARY.md` when done.
</output>
