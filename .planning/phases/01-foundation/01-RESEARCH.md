# Phase 1: Foundation — Research

**Status:** Ready for planning
**Confidence:** HIGH (all decisions locked in CONTEXT.md; this expands implementation details)

---

## Executive Summary

Phase 1 is pure foundation — no agents, no classification, no routing. The work splits into 5 deliverables that can be built in dependency order:

1. **`models.py`** — `Signal` frozen dataclass + `Severity` literal/enum + `alert_id` helper
2. **`config.py` extensions** — `_optional()` helper + 3 new env vars
3. **`repository.py` extensions** — schema migration (3 new columns on `signals`, 2 new tables), `count_delivered_today()`, `INSERT OR IGNORE` semantics, `PRAGMA busy_timeout`
4. **`thesis_loader.py` + `thesis.yaml`** — Pydantic schema, `ThesisStaleError`, load-once pattern
5. **`universe.py` + `universe.csv`** — CSV loader, deterministic md5 partitioning, K-1 exclusions

**Dependencies:**
- `models.py` blocks everything else (alert_id helper used by repository)
- `config.py` extensions block thesis_loader (THESIS_PATH) and consumer phases (ANTHROPIC_MODEL, DISCOVERY_PHASE)
- All others are independent and can be built in parallel

**New dependency to add:** `pydantic>=2.0` (for thesis validation; bonus: usable for Signal if preferred)

---

## Decision-by-Decision Research

### D-01 through D-04: Signal Dataclass

**Pattern (frozen dataclass):**

```python
# src/signal_system/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
import hashlib

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


def compute_alert_id(ticker: str | None, date_iso: str, rule: str, agent: str) -> str:
    """SHA-256 content-hash for idempotent reruns. date_iso = YYYY-MM-DD."""
    key = f"{ticker or '_'}:{date_iso}:{rule}:{agent}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
```

**Notes:**
- `frozen=True` enforces immutability — agents produce signals, nothing mutates them
- `slots=True` reduces memory and prevents typos via attribute assignment
- `Literal` type for Severity catches typos at static-analysis time without runtime enum overhead
- `sub_scores` is mutable internally but the Signal itself is frozen (the dict can't be replaced post-construction)
- Pydantic is NOT used here — frozen dataclass is simpler; if Pydantic is wanted later, swap is trivial

**Trade-off vs Pydantic BaseModel:**
- Frozen dataclass: stdlib, no extra import surface for callers, faster instantiation
- Pydantic BaseModel: runtime validation, schema generation for tool-use API
- Recommendation: dataclass for Signal (no untrusted input — agents are internal). Pydantic for thesis.yaml (operator-edited YAML needs validation).

---

### D-02: SHA-256 alert_id + INSERT OR IGNORE

**Semantics confirmed:**
- `hashlib.sha256(bytes).hexdigest()` is deterministic across processes, Python versions, and OSes
- 64-character hex output fits comfortably in SQLite TEXT column
- `INSERT OR IGNORE INTO signals (alert_id, ...)` silently no-ops when alert_id already exists (because `alert_id TEXT PRIMARY KEY`)

**Working pattern:**

```python
# In repository.py
def insert_signal(signal: Signal) -> bool:
    """Returns True if inserted, False if duplicate (already existed)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                score, routing_status, signal_price_snapshot, model_version,
                thesis_version_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.alert_id, signal.timestamp.isoformat(), signal.agent,
            signal.severity, signal.ticker, signal.title, signal.body,
            signal.score, None, None, None, None,
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
```

The new `insert_signal()` takes a `Signal` object. The legacy `insert_signal()` with positional kwargs (used by `daily_close.py`) needs a compatibility wrapper OR `daily_close.py` needs updating to construct a `Signal` first. **Recommendation:** Update `daily_close.py` (1 caller, atomic change).

---

### D-05, D-06: Schema Migration (CRITICAL)

**SQLite gotcha:** `ALTER TABLE ADD COLUMN IF NOT EXISTS` is NOT valid SQLite syntax in any version. The CONTEXT.md decision used loose language. The correct pattern uses `PRAGMA table_info()`:

```python
def _ensure_column(cursor, table: str, column: str, type_def: str) -> None:
    """Idempotent ALTER TABLE — checks existence via PRAGMA."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
```

**Migration sequence in `init_db()`:**

```python
def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")

        # Existing tables — CREATE IF NOT EXISTS preserves on upgrade
        cursor.execute("""CREATE TABLE IF NOT EXISTS signals (
            alert_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
            agent TEXT NOT NULL, severity TEXT NOT NULL, ticker TEXT,
            title TEXT NOT NULL, body TEXT, suggested_action TEXT,
            score REAL, acted INTEGER, acted_at TEXT, user_note TEXT,
            outcome_price_30d REAL, outcome_price_90d REAL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, job TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL
        )""")

        # Idempotent column additions
        _ensure_column(cursor, "signals", "routing_status", "TEXT")
        _ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")
        _ensure_column(cursor, "signals", "model_version", "TEXT")
        _ensure_column(cursor, "signals", "thesis_version_hash", "TEXT")

        # New tables
        cursor.execute("""CREATE TABLE IF NOT EXISTS wash_sale (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            account TEXT NOT NULL CHECK (account IN
                ('schwab_main','schwab_secondary','roth_ira','hsa')),
            trade_date TEXT NOT NULL,
            quantity REAL,
            cost_basis REAL,
            notes TEXT,
            created_at TEXT NOT NULL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job TEXT NOT NULL,
            model_version TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER,
            timestamp TEXT NOT NULL
        )""")

        conn.commit()
    finally:
        conn.close()
```

**Test:** Calling `init_db()` twice on an existing DB must not raise — verify with unit test.

---

### D-07: PRAGMA busy_timeout

**Canonical connection helper:**

```python
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")  # 30s wait if locked
    return conn
```

**Usage:** Replace every `sqlite3.connect(DB_PATH)` in `repository.py` with `_connect()`. The busy_timeout is per-connection — must be set on every new connection.

---

### D-10: count_delivered_today()

**ET midnight-to-midnight window:**

```python
from zoneinfo import ZoneInfo
from datetime import datetime, time

def count_delivered_today() -> dict[str, int]:
    """Today's DELIVERED signal counts by severity (ET timezone)."""
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    today_start = datetime.combine(now_et.date(), time.min, tzinfo=et)
    tomorrow_start = datetime.combine(
        now_et.date().replace(day=now_et.day + 1) if now_et.day < 28 else
        now_et.date(), time.min, tzinfo=et
    )
    # Simpler: use ISO date prefix matching on timestamp column
    today_iso = now_et.date().isoformat()

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT severity, COUNT(*) FROM signals
            WHERE routing_status = 'DELIVERED'
              AND timestamp LIKE ? || '%'
            GROUP BY severity
        """, (today_iso,))
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()
```

**Note:** ISO date-prefix matching works because all timestamps are stored as ET ISO strings — already confirmed in existing `repository.py`. Avoids datetime arithmetic complexity.

Result is a dict like `{"ACTION_REQUIRED": 0, "INFORMATIONAL": 2}`. Router callers should treat missing keys as 0.

---

### D-11 through D-14: thesis_loader.py + thesis.yaml

**Pydantic v2 schema:**

```python
# src/signal_system/data/thesis_loader.py
from __future__ import annotations
import hashlib
from datetime import date
from pathlib import Path
import yaml
from pydantic import BaseModel, ValidationError


class ThesisStaleError(RuntimeError):
    """Raised when thesis.yaml review_due date is past — propagates to /fail."""


class Pillar(BaseModel):
    name: str
    description: str
    keywords: list[str]


class Thesis(BaseModel):
    review_due: date
    pillars: list[Pillar]


def load_thesis(path: Path | str) -> tuple[Thesis, str]:
    """Load and validate thesis.yaml. Returns (thesis, version_hash).

    Raises:
        ThesisStaleError: if review_due is in the past.
        FileNotFoundError: if path doesn't exist.
        ValidationError: if schema is invalid.
    """
    p = Path(path)
    raw = p.read_bytes()
    version_hash = hashlib.sha256(raw).hexdigest()
    data = yaml.safe_load(raw)
    thesis = Thesis.model_validate(data)

    today_et = date.today()  # date.today uses system tz; caller responsible for ET context
    if thesis.review_due < today_et:
        raise ThesisStaleError(
            f"thesis.yaml review_due is {thesis.review_due.isoformat()} "
            f"(today: {today_et.isoformat()}). Update thesis.yaml before running."
        )

    return thesis, version_hash
```

**Sample thesis.yaml:**

```yaml
# thesis.yaml — operator-maintained investment thesis (gitignored)
review_due: 2026-08-01

pillars:
  - name: monetary_policy
    description: Federal Reserve policy and macro liquidity conditions
    keywords:
      - rate cut
      - rate hike
      - FOMC
      - quantitative easing
      - liquidity

  - name: ai_capex
    description: Capital expenditure cycle in AI infrastructure
    keywords:
      - GPU
      - data center
      - capex
      - hyperscaler
```

**ThesisStaleError propagation:** Because `ThesisStaleError` extends `RuntimeError`, it bubbles up through the `with heartbeat()` context manager — heartbeat's `__exit__` sees the exception and pings `/fail`. Jobs must NOT catch it before heartbeat sees it. The existing `daily_close.py` pattern is correct: catch-all is OUTSIDE the `with` block.

**`.gitignore` addition:** Add `thesis.yaml` to `.gitignore` and provide a `thesis.example.yaml` instead.

---

### D-15 through D-17: Universe (CSV + md5 partition)

**`universe.csv` format:**

```csv
ticker,core_holding,k1_etf
AAPL,1,0
MSFT,1,0
SPY,0,0
USO,0,1
UNG,0,1
```

**`universe.py`:**

```python
# src/signal_system/data/universe.py
from __future__ import annotations
import csv
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UNIVERSE_PATH = Path(__file__).parent / "universe.csv"


def _md5_bucket(ticker: str) -> int:
    """Deterministic 0/1/2 partition. Stable across processes, days, restarts."""
    return int(hashlib.md5(ticker.encode("utf-8")).hexdigest(), 16) % 3


def _today_bucket() -> int:
    """ET day-of-year mod 3 — rotates the partition daily."""
    return datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday % 3


def get_todays_universe() -> list[str]:
    """Returns tickers to scan today: core_holding ∪ today's rotation partition.
    K-1 ETFs (k1_etf=1) are excluded unconditionally.
    """
    todays_bucket = _today_bucket()
    tickers: list[str] = []
    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["k1_etf"]):
                continue  # exclude K-1 ETFs at load time
            ticker = row["ticker"].strip().upper()
            is_core = bool(int(row["core_holding"]))
            in_partition = _md5_bucket(ticker) == todays_bucket
            if is_core or in_partition:
                tickers.append(ticker)
    return tickers
```

**Determinism verification:**
- `hashlib.md5` is stable across Python versions, processes, OSes
- `tm_yday` from `ZoneInfo("America/New_York")` is stable across DST (DST doesn't change date, only clock)
- Year boundary: day 365 → 365 % 3 == 2; day 1 → 1 % 3 == 1; partition rotation continues smoothly

**Edge cases:**
- Leap years (day 366): `366 % 3 == 0` — still in range, fine
- Ticker case: normalized to uppercase before hashing (universe.csv may have mixed case)
- Empty universe.csv: returns `[]` — agent must handle gracefully (Phase 4 concern)

---

### D-18 through D-21: Config Extensions

```python
# src/signal_system/config.py — additions
def _optional(name: str, default: str) -> str:
    """Optional env var with a default."""
    return os.environ.get(name, default).strip() or default

# Required
ANTHROPIC_MODEL = _require("ANTHROPIC_MODEL")  # e.g., "claude-sonnet-4-6"

# Optional with defaults
THESIS_PATH = _optional("THESIS_PATH", "thesis.yaml")
DISCOVERY_PHASE = _optional("DISCOVERY_PHASE", "A")
if DISCOVERY_PHASE not in ("A", "B"):
    raise RuntimeError(
        f"DISCOVERY_PHASE must be 'A' or 'B', got {DISCOVERY_PHASE!r}"
    )
```

**`.env.example` additions:**

```bash
# Required: pinned Claude model ID (no floating aliases)
ANTHROPIC_MODEL=claude-sonnet-4-6

# Optional: thesis.yaml path (default: thesis.yaml at repo root)
# THESIS_PATH=thesis.yaml

# Optional: Discovery Agent phase (A=logs-only, B=live routing; default: A)
# DISCOVERY_PHASE=A
```

---

## Code Patterns Reference

### Connection helper (used everywhere in repository.py)

```python
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn
```

### Idempotent column add

```python
def _ensure_column(cursor, table: str, column: str, type_def: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
```

### Construction of a Signal from agent code (example)

```python
from signal_system.models import Signal, compute_alert_id
from datetime import datetime
from zoneinfo import ZoneInfo

now_et = datetime.now(ZoneInfo("America/New_York"))
alert_id = compute_alert_id(
    ticker="AAPL",
    date_iso=now_et.date().isoformat(),
    rule="news_classifier_v1",
    agent="news_classifier",
)
signal = Signal(
    ticker="AAPL",
    score=0.85,
    severity="INFORMATIONAL",
    agent="news_classifier",
    timestamp=now_et,
    alert_id=alert_id,
    title="AAPL: GPU shipment guidance raised",
)
```

---

## Pitfalls Specific to This Phase

1. **`ALTER TABLE ADD COLUMN IF NOT EXISTS` is NOT valid SQLite.** Use `PRAGMA table_info()` check + conditional ALTER. The CONTEXT.md uses loose language — the implementation must use the pattern shown above.

2. **`hash()` vs `hashlib.md5()`.** Python's built-in `hash()` is salted per process. Use `hashlib.md5(ticker.encode()).hexdigest()` for stable cross-process partitioning. (Pitfall #4 from research/PITFALLS.md.)

3. **Pydantic v1 vs v2 syntax differs significantly.** Use Pydantic v2 (`model_validate()`, not `parse_obj()`; `ValidationError` import path differs). Pin `pydantic>=2.0` in pyproject.toml.

4. **`PRAGMA busy_timeout` is per-connection.** Setting it once in `init_db()` does nothing for subsequent connections. Every `sqlite3.connect()` call needs a follow-up `PRAGMA busy_timeout = 30000`. Use the `_connect()` helper.

5. **`yaml.safe_load`, not `yaml.load`.** `yaml.load` allows arbitrary Python object construction (security risk). Always use `yaml.safe_load`.

6. **`thesis.yaml` must be gitignored.** Contains operator's investment thesis — sensitive. Provide `thesis.example.yaml` as a template that IS committed.

7. **`date.today()` uses system timezone.** For ET-aware "today" comparison, use `datetime.now(ZoneInfo("America/New_York")).date()`. Less critical for Phase 1 (review_due granularity is day-level) but document the convention.

8. **`daily_close.py` calls the legacy `insert_signal()` kwargs API.** Updating `insert_signal()` to take a `Signal` object means updating `daily_close.py` too. Both changes go in Phase 1 — atomic.

9. **`CHECK (account IN ...)` on `wash_sale`** — this constraint prevents typos but requires schema migration if a new account type is ever added. Acceptable for Phase 1; flag in CONTEXT.md decision history if it becomes an issue.

---

## Open Questions (Flagged for Later Phases — NOT BLOCKING)

- **Q1 (Phase 3):** Does `messages.parse()` support `temperature=0.0` with `output_format`? — News Classifier concern.
- **Q2 (Phase 3):** Anthropic prompt caching minimum token threshold — empirical validation needed.
- **Q3 (Phase 4):** Which Finnhub free-tier endpoints actually work for the 35/30/25/10 Discovery scoring formula.
- **Q4 (Phase 2):** Best pattern for the Finnhub rate-limit token bucket (preemptive sleep vs tenacity reactive).

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|-----------|-------|
| Signal dataclass design | HIGH | Frozen dataclass is the canonical Python pattern |
| SHA-256 alert_id | HIGH | hashlib determinism is well-established |
| SQLite schema migration | HIGH | PRAGMA table_info pattern is standard |
| busy_timeout placement | HIGH | Per-connection requirement is documented in stdlib |
| md5 universe rotation | HIGH | Determinism verified; pitfall #4 already known |
| Pydantic v2 syntax | HIGH | Mainstream library, v2 API is stable |
| thesis.yaml gitignore | HIGH | Standard practice for operator-edited config |
| Universe CSV format | HIGH | stdlib csv.DictReader is sufficient at 1,500 rows |
| Config extensions | HIGH | Follows existing `_require()` pattern |

**Overall: HIGH confidence — all decisions have concrete implementation patterns ready for the planner.**
