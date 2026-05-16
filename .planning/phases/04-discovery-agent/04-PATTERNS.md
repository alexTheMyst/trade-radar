# Phase 4: Discovery Agent — Pattern Map

**Mapped:** 2026-05-16
**Files analyzed:** 6 (2 new, 4 modified)
**Analogs found:** 6 / 6

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/signal_system/discovery/discovery_agent.py` | agent/service | batch + request-response | `src/signal_system/classifier/news_classifier.py` | role-match |
| `src/signal_system/discovery/__init__.py` | module init | — | `src/signal_system/classifier/__init__.py` | exact |
| `src/signal_system/models.py` | model | — | self (add field to frozen dataclass) | exact |
| `src/signal_system/state/repository.py` | repository | CRUD | self (extend with new functions) | exact |
| `src/signal_system/data/finnhub_client.py` | data client | request-response | self (`_fetch_single_quote` + `fetch_quotes`) | exact |
| `tests/test_discovery_agent.py` | test | — | `tests/test_smoke.py` | role-match |

---

## Pattern Assignments

### `src/signal_system/discovery/discovery_agent.py` (agent, batch)

**Analog:** `src/signal_system/classifier/news_classifier.py`

**Imports pattern** (`news_classifier.py` lines 7–26):
```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from signal_system import config
from signal_system.data.finnhub_client import fetch_quote, fetch_company_news
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

logger = logging.getLogger(__name__)
```
> **Note:** No `anthropic`, `pydantic`, or `tenacity` imports — discovery agent is pure Python stdlib + existing project modules.

**Module-level threshold constants pattern** (`news_classifier.py` lines 28–31):
```python
# Module-level constants for operator-tunable thresholds
_ACTION_REQUIRED_THRESHOLD: float = 0.85
_INFORMATIONAL_THRESHOLD: float = 0.60
_ET = ZoneInfo("America/New_York")
```
Copy this pattern directly for discovery thresholds:
```python
# Score thresholds — initial guesses; tune after Phase A observation (DISC-03/DISC-09)
_ACTION_REQUIRED_SCORE: float = 80.0
_INFORMATIONAL_SCORE: float = 60.0
_WEIGHTS = {"price_momentum": 35, "volume_rank": 30, "range_position": 25, "news_activity": 10}
_ET = ZoneInfo("America/New_York")
```

**Severity mapping helper** (`news_classifier.py` lines 128–138):
```python
def _severity_from_confidence(conf: float) -> str:
    """Map confidence score to severity band.

    Thresholds are initial guesses — operator can tune during quarterly review.
    """
    # TODO(operator): confirm thresholds during quarterly review
    if conf >= _ACTION_REQUIRED_THRESHOLD:
        return "ACTION_REQUIRED"
    if conf >= _INFORMATIONAL_THRESHOLD:
        return "INFORMATIONAL"
    return "MONITORING"
```
Mirror for discovery:
```python
def _severity_from_score(score: float) -> str:
    """Map composite score to severity band. Thresholds: DISC-08/DISC-09."""
    if score >= _ACTION_REQUIRED_SCORE:
        return "ACTION_REQUIRED"
    if score >= _INFORMATIONAL_SCORE:
        return "INFORMATIONAL"
    return "MONITORING"
```

**Signal construction pattern** (`news_classifier.py` lines 281–291):
```python
return Signal(
    ticker=ticker,
    score=parsed.confidence,
    severity=_severity_from_confidence(parsed.confidence),
    agent="news_classifier",
    timestamp=datetime.now(_ET),
    alert_id=alert_id,
    title=f"{parsed.pillar_name}: {raw[:120]}",
    body=parsed.rationale,
    model_version=config.ANTHROPIC_MODEL,
    thesis_version_hash=thesis_version_hash,
)
```
Discovery variant (note `signal_price_snapshot`, `sub_scores`, no `model_version`/`thesis_version_hash`):
```python
Signal(
    ticker=ticker,
    score=composite,
    severity=_severity_from_score(composite),
    agent="discovery_agent",
    timestamp=datetime.now(_ET),
    alert_id=compute_alert_id(ticker, date_iso, "discovery", "discovery_agent"),
    title=f"{ticker}: Discovery score {composite:.0f}",
    body=f"weights=35/30/25/10 momentum={sub['price_momentum']:.2f} "
         f"volume={sub['volume_rank']:.2f} range={sub['range_position']:.2f} "
         f"news={sub['news_activity']:.2f}",
    sub_scores=sub,
    signal_price_snapshot=quote["c"],
    model_version=None,
    thesis_version_hash=None,
)
```

**Phase A / Phase B branching pattern** — read `config.DISCOVERY_PHASE` at call time, not module level (CONTEXT.md D-12):
```python
def score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]:
    ...
    phase = config.DISCOVERY_PHASE  # read at call time — no module-level caching
    signals: list[Signal] = []
    for ticker, sub in scored.items():
        signal = _build_signal(ticker, sub, date_iso)
        if phase == "A":
            repository.insert_signal(signal, routing_status="MONITORING")
        else:
            signals.append(signal)
    repository.update_run_counts(run_id, tickers_scanned, len(signals if phase == "B" else scored))
    return signals
```

**Cross-sectional ranking helper** (`04-RESEARCH.md` Q3 — stdlib only):
```python
def _cross_sectional_rank(values: dict[str, float]) -> dict[str, float]:
    """Rank tickers 0.0 (lowest) to 1.0 (highest). Alphabetical tiebreak."""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {next(iter(values)): 0.5}
    # Sort descending by value, then ascending by ticker for stable tiebreak
    ordered = sorted(values.items(), key=lambda x: (-x[1], x[0]))
    return {ticker: 1.0 - (i / (n - 1)) for i, (ticker, _) in enumerate(ordered)}
```

**Three-pass structure** (RESEARCH.md §Implementation Approach):
```python
def score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]:
    # Pass 1 — Fetch
    raw: dict[str, dict] = {}
    for ticker in tickers:
        quote = fetch_quote(ticker)
        if quote is None:
            continue  # skip — quote is required (DISC-05)
        news = fetch_company_news(ticker, today - timedelta(days=7), today)
        raw[ticker] = {"quote": quote, "news_count": len(news)}
    tickers_scanned = len(raw)

    # Pass 2 — Rank (cross-sectional per factor)
    momentum_ranks = _cross_sectional_rank({t: d["quote"]["dp"] for t, d in raw.items()})
    volume_ranks   = _cross_sectional_rank({t: d["quote"]["v"]  for t, d in raw.items()})
    range_ranks    = _cross_sectional_rank({t: _range_position(d["quote"]) for t, d in raw.items()})
    news_ranks     = _cross_sectional_rank({t: d["news_count"] for t, d in raw.items()})

    # Pass 3 — Score + emit
    signals = []
    for ticker in raw:
        sub = {
            "price_momentum": momentum_ranks[ticker],
            "volume_rank":    volume_ranks[ticker],
            "range_position": range_ranks[ticker],
            "news_activity":  news_ranks[ticker],
        }
        composite = (35*sub["price_momentum"] + 30*sub["volume_rank"]
                    + 25*sub["range_position"] + 10*sub["news_activity"])
        if composite < _INFORMATIONAL_SCORE:
            continue
        signal = _build_signal(ticker, composite, sub, raw[ticker]["quote"]["c"], date_iso)
        ...
```

**Range position helper** (handles h==l edge case — CONTEXT.md D-07):
```python
def _range_position(quote: dict) -> float:
    """(c - l) / (h - l). Returns 0.0 if h == l (flat/illiquid day)."""
    h, l, c = quote.get("h", 0), quote.get("l", 0), quote.get("c", 0)
    if h == l:
        return 0.0
    return (c - l) / (h - l)
```

---

### `src/signal_system/discovery/__init__.py` (module init)

**Analog:** `src/signal_system/classifier/__init__.py` (lines 1–3) — exact pattern:
```python
from signal_system.classifier.news_classifier import classify_headlines, ClassificationResult

__all__ = ["classify_headlines", "ClassificationResult"]
```
Mirror:
```python
from signal_system.discovery.discovery_agent import score_universe

__all__ = ["score_universe"]
```

---

### `src/signal_system/models.py` — add `signal_price_snapshot` field

**Analog:** Self — `src/signal_system/models.py` lines 17–35

**Existing frozen dataclass** (lines 17–35):
```python
@dataclass(frozen=True, slots=True)
class Signal:
    """Immutable value object produced by agents and passed to the router.

    Fields are write-once at construction time (frozen=True).
    routing_status is NOT a field — it lives in the DB and is set by the router.
    """

    ticker: str | None
    score: float | None
    severity: Severity
    agent: str
    timestamp: datetime
    alert_id: str
    title: str
    body: str | None = None
    sub_scores: dict[str, float] = field(default_factory=dict)
    model_version: str | None = None
    thesis_version_hash: str | None = None
```

**Change:** Add `signal_price_snapshot: float | None = None` after `thesis_version_hash` (last optional field, consistent pattern — all optional fields are `= None`):
```python
    model_version: str | None = None
    thesis_version_hash: str | None = None
    signal_price_snapshot: float | None = None  # ADD THIS
```
> **Gotcha:** `frozen=True, slots=True` — field order matters for `__init__`. New optional field must go AFTER all required fields and AFTER existing optional fields to avoid breaking existing Signal() call sites that pass positional args.

---

### `src/signal_system/state/repository.py` — three additions

**Analog:** Self — `src/signal_system/state/repository.py`

#### Addition 1: `routing_status` kwarg on `insert_signal()`

**Existing signature** (line 121):
```python
def insert_signal(signal: Signal) -> bool:
```
**Existing hardcoded None** (lines 146–147):
```python
            None,   # routing_status — set by the router, not the agent
            None,   # signal_price_snapshot — set by discovery agent at generation time
```

**Change pattern** — match `insert_llm_call()` style of keyword-only args (lines 194–201):
```python
def insert_signal(signal: Signal, *, routing_status: str | None = None) -> bool:
    """Insert a Signal into the database using INSERT OR IGNORE semantics.
    ...
    Args:
        routing_status: Optional override. Pass "MONITORING" from Phase A discovery
            to force MONITORING routing regardless of signal severity.
    """
    ...
    cursor.execute("""INSERT OR IGNORE INTO signals ...""", (
        ...
        routing_status,              # now caller-supplied (Phase A passes "MONITORING")
        signal.signal_price_snapshot,  # now from Signal field
        ...
    ))
```

#### Addition 2: `update_run_counts()`

**Analog pattern:** `update_run()` (lines 177–191):
```python
def update_run(run_id: str, status: str) -> None:
    """Update a run record with ended_at timestamp and status."""
    ended_at = datetime.now(ZoneInfo("America/New_York")).isoformat()

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE runs
            SET status = ?, ended_at = ?
            WHERE run_id = ?
        """, (status, ended_at, run_id))
        conn.commit()
    finally:
        conn.close()
```
Mirror for `update_run_counts()`:
```python
def update_run_counts(run_id: str, tickers_scanned: int, tickers_signaled: int) -> None:
    """Write scan audit counts to an existing run row (DISC-05)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE runs
            SET tickers_scanned = ?, tickers_signaled = ?
            WHERE run_id = ?
        """, (tickers_scanned, tickers_signaled, run_id))
        conn.commit()
    finally:
        conn.close()
```

#### Addition 3: `_ensure_column()` calls in `init_db()`

**Existing pattern** (lines 83–86):
```python
        # Idempotent column additions to signals (Phase 1 schema extensions)
        _ensure_column(cursor, "signals", "routing_status", "TEXT")
        _ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")
        _ensure_column(cursor, "signals", "model_version", "TEXT")
        _ensure_column(cursor, "signals", "thesis_version_hash", "TEXT")
```
Add after, labelled as Phase 4:
```python
        # Idempotent column additions to runs (Phase 4 scan audit trail — DISC-05)
        _ensure_column(cursor, "runs", "tickers_scanned", "INTEGER")
        _ensure_column(cursor, "runs", "tickers_signaled", "INTEGER")
```
> **Placement:** After the Phase 1 `_ensure_column` block, before `conn.commit()`.

---

### `src/signal_system/data/finnhub_client.py` — add `fetch_quote()`

**Analog:** `_fetch_single_quote()` (lines 64–82) and `fetch_company_news()` (lines 125–145)

**Private function pattern** (`_fetch_single_quote`, lines 64–82):
```python
@_RETRY_DECORATOR
def _fetch_single_quote(ticker: str) -> dict | None:
    _acquire_slot()
    try:
        response = _get_client().quote(ticker)
    except FinnhubAPIException as exc:
        if exc.status_code in PAID_TIER_STATUS_CODES:
            logger.warning(
                "Quote unavailable for %r (HTTP %s) — paid tier or unknown, skipping",
                ticker,
                exc.status_code,
            )
            return None
        raise  # 429 and other errors re-raise → tenacity sees them
    close = response.get("c")
    if close is None or close <= 0:
        logger.debug("No price data for %r (c=%r) — skipping", ticker, close)
        return None
    return response
```
> **Key difference from `_fetch_single_quote`:** The existing function validates only `c > 0`. The new `fetch_quote()` must also validate `dp is not None`, `h >= l > 0`, `v is not None` per score-floor guard (DISC-02/CONTEXT D-07). Validation belongs in the public wrapper, not the private function.

**Public wrapper pattern** (mirror `fetch_company_news` which wraps `_fetch_company_news_raw`, lines 125–145):
```python
def fetch_quote(ticker: str) -> dict | None:
    """Fetch a full quote dict for one ticker. Returns None if quote is invalid or unavailable.

    Validates all fields required by Discovery Agent score-floor guard (DISC-02):
    - c > 0, dp is not None, h >= l > 0, v is not None.
    Returns None (not raises) on validation failure — caller skips the ticker.
    """
    try:
        response = _fetch_single_quote(ticker)
    except Exception as exc:
        logger.error("Giving up on quote for %r after exhausted retries: %s", ticker, exc)
        return None
    if response is None:
        return None
    # Score-floor guard: all discovery factors require these fields
    dp = response.get("dp")
    v  = response.get("v")
    h  = response.get("h", 0)
    l  = response.get("l", 0)
    if dp is None or v is None or not (h >= l > 0):
        logger.debug("Incomplete quote for %r (dp=%r v=%r h=%r l=%r) — skipping", ticker, dp, v, h, l)
        return None
    return response
```
> **Placement:** Add after `fetch_quotes()` (line 94), before `fetch_spy_close()` (line 97). Follow the existing ordering: private helpers first, public wrappers after.

---

### `tests/test_discovery_agent.py` (test)

**Analog:** `tests/test_smoke.py`

**File header and imports pattern** (`test_smoke.py` lines 1–16):
```python
"""
Smoke tests for signal-system.

All external I/O (Finnhub, SMTP, healthchecks.io) is mocked.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from signal_system.state import repository
```
Mirror:
```python
"""
Tests for Discovery Agent (Phase 4).

All Finnhub I/O is mocked. DB is isolated via monkeypatch on repository.DB_PATH.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from signal_system.state import repository
from signal_system.discovery import score_universe
```

**DB isolation pattern** (`test_smoke.py` lines 32–33, 92–93, 215–216):
```python
def test_something(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
```
> **Note:** Always call `repository.init_db()` after monkeypatching `DB_PATH`. This is the universal DB isolation pattern in this codebase.

**Finnhub mock pattern** (`test_smoke.py` lines 457–460):
```python
monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
mock_client = MagicMock()
mock_client.quote.return_value = fake_quote
monkeypatch.setattr(fc, "_get_client", lambda: mock_client)
```
For discovery tests, patch at the public function boundary (not the private client):
```python
with patch("signal_system.discovery.discovery_agent.fetch_quote") as mock_fq, \
     patch("signal_system.discovery.discovery_agent.fetch_company_news") as mock_fcn:
    mock_fq.side_effect = lambda ticker: FAKE_QUOTES.get(ticker)
    mock_fcn.return_value = []
    result = score_universe(["AAPL", "MSFT"], run_id="test-run-id", date_iso="2026-05-16")
```

**DISCOVERY_PHASE monkeypatch pattern** (derived from `test_smoke.py` line 171):
```python
monkeypatch.setattr(config, "DISCOVERY_PHASE", "A")
# or:
monkeypatch.setattr(config, "DISCOVERY_PHASE", "B")
```
> **Not** `monkeypatch.setenv` + `importlib.reload` — patch the already-loaded `config` attribute directly to avoid side effects on other tests.

**Minimal valid quote fixture** (required fields per score-floor guard DISC-07):
```python
def _make_quote(c=100.0, dp=1.5, v=1_000_000, h=101.0, l=99.0) -> dict:
    """Minimal valid quote dict for score-floor guard validation."""
    return {"c": c, "dp": dp, "v": v, "h": h, "l": l, "o": 99.5, "pc": 98.5}
```

**DB assertion pattern** (`test_smoke.py` lines 108–116):
```python
conn = sqlite3.connect(tmp_path / "test.db")
row = conn.execute(
    "SELECT ticker, score FROM signals WHERE agent='DAILY_CLOSE'"
).fetchone()
conn.close()
assert row is not None
```
Mirror for discovery:
```python
conn = sqlite3.connect(tmp_path / "test.db")
rows = conn.execute(
    "SELECT ticker, score, routing_status, signal_price_snapshot FROM signals WHERE agent='discovery_agent'"
).fetchall()
conn.close()
assert len(rows) == expected_count
```

**Test cases to implement** (from RESEARCH.md Q10):

| Test name | What to mock | What to assert |
|---|---|---|
| `test_score_computation` | fetch_quote (2 tickers), fetch_company_news=[] | composite = weighted rank sum; Signal fields correct |
| `test_score_floor_invalid_quote` | fetch_quote returns None (c=0) | no Signal emitted |
| `test_score_floor_missing_dp` | fetch_quote returns dict with dp=None | no Signal (dp is required) |
| `test_range_position_flat_day` | h==l quote | Signal still emitted, range_position rank contributed 0.0 |
| `test_phase_a_monitoring_insert` | DISCOVERY_PHASE=A | DB row has routing_status="MONITORING", severity preserved |
| `test_phase_b_returns_signals` | DISCOVERY_PHASE=B | returns list[Signal], no DB insert |
| `test_cross_sectional_ranking_ties` | two tickers same dp | alphabetical tiebreak: A ranks higher than B |
| `test_news_activity_zero` | fetch_company_news returns [] | news_activity rank = 0.0 in sub_scores |
| `test_empty_universe` | tickers=[] | returns [] immediately |
| `test_single_ticker_ranks` | one valid ticker | all sub-score ranks = 0.5 |
| `test_below_threshold_suppressed` | score < 60 (e.g., all zero quotes) | no Signal in Phase A or B |
| `test_action_required_severity` | score >= 80 | Signal.severity="ACTION_REQUIRED" |
| `test_informational_severity` | 60 <= score < 80 | Signal.severity="INFORMATIONAL" |
| `test_update_run_counts` | real DB + monkeypatched path | runs row has correct tickers_scanned/tickers_signaled |
| `test_signal_price_snapshot` | fetch_quote returns c=123.45 | Signal.signal_price_snapshot == 123.45 |

---

## Shared Patterns

### DB Connection (all repository functions)
**Source:** `src/signal_system/state/repository.py` lines 19–23
```python
def _connect() -> sqlite3.Connection:
    """Open a SQLite connection with busy_timeout to handle concurrent writes."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")  # 30-second wait if DB is locked
    return conn
```
**Pattern:** Always `conn = _connect()` → `try:` → work → `conn.commit()` → `finally: conn.close()`. **Never** use `with conn:` context manager — explicit `close()` in `finally` is the established convention.

### Timezone (all timestamp-bearing code)
**Source:** Throughout `repository.py`, `news_classifier.py`
```python
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")
datetime.now(_ET)
```
Always `ZoneInfo("America/New_York")` — never `timezone.utc` for business timestamps, never `datetime.now()` (naive). Module-level `_ET` constant avoids repeated construction.

### Rate-limit + retry (all Finnhub calls)
**Source:** `src/signal_system/data/finnhub_client.py` lines 46–61
```python
def _is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, FinnhubAPIException):
        return exc.status_code == 429
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return False

_RETRY_DECORATOR = retry(
    retry=retry_if_exception(_is_transient_error),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
```
`fetch_quote()` calls `_fetch_single_quote()` which already has `@_RETRY_DECORATOR` and `_acquire_slot()` — no extra rate-limit/retry code needed in the discovery agent itself.

### `PAID_TIER_STATUS_CODES` check (all Finnhub callers)
**Source:** `src/signal_system/data/finnhub_client.py` line 26, used in lines 70–71, 114–115
```python
PAID_TIER_STATUS_CODES: frozenset[int] = frozenset({403, 404})
...
if exc.status_code in PAID_TIER_STATUS_CODES:
    logger.warning("... paid tier or unknown, skipping", ticker, exc.status_code)
    return None
```
Discovery agent does **not** call Finnhub directly — it calls `fetch_quote()` and `fetch_company_news()` which already handle `PAID_TIER_STATUS_CODES` internally and return `None`/`[]`. No need to reference `PAID_TIER_STATUS_CODES` in `discovery_agent.py`.

### Logging convention
**Source:** All modules (e.g., `news_classifier.py` line 26, `finnhub_client.py` line 16)
```python
logger = logging.getLogger(__name__)
```
Use `%r` for ticker symbols (adds quotes, shows None clearly). Use `%s` for error objects.
```python
logger.warning("Quote unavailable for %r (HTTP %s) — paid tier or unknown, skipping", ticker, exc.status_code)
logger.debug("No price data for %r (c=%r) — skipping", ticker, close)
logger.error("Giving up on %r after exhausted retries: %s", ticker, exc)
```

### `from __future__ import annotations`
**Source:** `news_classifier.py` line 7, `universe.py` line 11
All new modules start with `from __future__ import annotations` — enables `X | Y` union syntax on Python 3.12 without runtime overhead.

### INSERT OR IGNORE idempotency
**Source:** `repository.py` lines 131–152
```python
cursor.execute("INSERT OR IGNORE INTO signals (...) VALUES (...)", (...))
conn.commit()
return cursor.rowcount == 1  # True = new insert, False = duplicate suppressed
```
`alert_id` is the PRIMARY KEY — reruns on the same day produce the same `alert_id` (deterministic via `compute_alert_id`) and are silently ignored.

---

## No Analog Found

All files have close analogs in the existing codebase. No files require RESEARCH.md patterns as primary reference.

---

## Gotchas & Integration Notes

1. **`signal_price_snapshot` field must be added to `Signal` BEFORE `insert_signal()` is updated.** If `insert_signal()` references `signal.signal_price_snapshot` before the field exists, all tests will break. Implementation order: `models.py` → `repository.py` → `finnhub_client.py` → `discovery_agent.py` → `__init__.py` → tests.

2. **`frozen=True, slots=True` dataclass field ordering.** New `signal_price_snapshot: float | None = None` must be appended as the last field. Fields with defaults cannot precede fields without defaults in Python dataclasses — the existing field order (`sub_scores` has a `field(default_factory=...)`, followed by `model_version=None`, `thesis_version_hash=None`) must be preserved with the new field appended last.

3. **`insert_signal()` currently hardcodes `None` for both `routing_status` and `signal_price_snapshot` (lines 146–147).** Both change: `routing_status` becomes a kwarg default `None`, `signal_price_snapshot` becomes `signal.signal_price_snapshot`. Existing callers (news classifier, daily_close) pass no `routing_status` kwarg — they continue to get `None` by default (correct behavior, router sets this later).

4. **`fetch_quote()` is a NEW public function, not a rename of `_fetch_single_quote()`.** The existing `fetch_quotes()` (plural) remains unchanged — it's used elsewhere and only extracts `c`. The new `fetch_quote()` (singular) adds score-floor validation and returns the full dict.

5. **Phase A counting:** `tickers_signaled` in `update_run_counts()` should count signals that *would have been* emitted (score ≥ 60), not only those actually returned (Phase B returns them; Phase A inserts them). Use `len(scored)` where `scored` is the dict of tickers that passed the threshold.

6. **Test config isolation:** Use `monkeypatch.setattr(config, "DISCOVERY_PHASE", "A")` directly — not `importlib.reload()`. The `test_smoke.py` config reload tests are the exception, not the rule; they exist specifically to test config validation logic, not to set up test state.

7. **`_RETRY_DECORATOR` is at module level in `finnhub_client.py`** — `fetch_quote()` calls `_fetch_single_quote()` which is already decorated. Do not add another `@_RETRY_DECORATOR` to `fetch_quote()` itself (double-wrapping).

---

## Metadata

**Analog search scope:** `src/signal_system/` (all subdirs), `tests/`
**Files scanned:** 10 (news_classifier.py, classifier/__init__.py, finnhub_client.py, repository.py, models.py, config.py, universe.py, conftest.py, test_smoke.py, 04-CONTEXT.md, 04-RESEARCH.md)
**Pattern extraction date:** 2026-05-16
