# Phase 6: Job Orchestration - Pattern Map

**Mapped:** 2026-05-16  
**Files analyzed:** 12  
**Analogs found:** 11 / 12

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/signal_system/__main__.py` | route | request-response | `src/signal_system/__main__.py` | exact |
| `src/signal_system/jobs/news_morning.py` | service | batch | `src/signal_system/jobs/daily_close.py` | role-match |
| `src/signal_system/jobs/discovery.py` | service | batch | `src/signal_system/jobs/daily_close.py` | role-match |
| `src/signal_system/jobs/common.py` *(exact helper filename flexible)* | utility | transform | `src/signal_system/router/alert_router.py` | partial |
| `src/signal_system/jobs/outcome_backfill.py` | service | batch | `src/signal_system/discovery/discovery_agent.py` | partial |
| `src/signal_system/state/repository.py` | service | CRUD | `src/signal_system/state/repository.py` | exact |
| `src/signal_system/data/universe.py` | utility | file-I/O | `src/signal_system/data/universe.py` | exact |
| `tests/test_job_orchestration.py` | test | batch | `tests/test_smoke.py` | role-match |
| `tests/test_outcome_backfill.py` | test | CRUD | `tests/test_discovery_agent.py` | role-match |
| `ops/windows-task-scheduler.md` | config | file-I/O | `mvp-week1.md` | role-match |
| `ops/task-scheduler-reference.xml` | config | file-I/O | — | none |
| `ops/operator-setup-checklist.md` | config | file-I/O | `mvp-week1.md` | role-match |

## Pattern Assignments

### `src/signal_system/__main__.py` (route, request-response)

**Analog:** `src/signal_system/__main__.py`

**Dispatcher pattern** (`src/signal_system/__main__.py:1-21`):
```python
import logging
import sys

from signal_system.state import repository
from signal_system.jobs.daily_close import run as run_daily_close

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

JOBS = {
    "daily-close": run_daily_close,
}

if __name__ == "__main__":
    repository.init_db()
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print(f"Usage: python -m signal_system <job>. Available: {list(JOBS)}")
        sys.exit(2)
    JOBS[sys.argv[1]]()
```

Copy this shape when adding `news-morning` and `discovery`: import run functions, extend `JOBS`, keep `repository.init_db()` before dispatch, keep the usage guard.

---

### `src/signal_system/jobs/news_morning.py` (service, batch)

**Primary analog:** `src/signal_system/jobs/daily_close.py`  
**Secondary analogs:** `src/signal_system/classifier/news_classifier.py`, `src/signal_system/router/alert_router.py`, `src/signal_system/state/repository.py`

**Job skeleton** (`src/signal_system/jobs/daily_close.py:1-40`):
```python
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.models import Signal, compute_alert_id
from signal_system.monitoring import heartbeat
from signal_system.data import finnhub_client
from signal_system.state import repository
from signal_system.delivery import email_sender

logger = logging.getLogger(__name__)

def run() -> None:
    run_id = repository.insert_run("daily-close")
    try:
        with heartbeat.heartbeat():
            ...
            email_sender.send_email(...)
            repository.update_run(run_id, "success")  # inside heartbeat
    except Exception:
        repository.update_run(run_id, "failed")
        raise
```

**Thesis load / fail-loud pattern** (`src/signal_system/data/thesis_loader.py:43-72`):
```python
def load_thesis(path: Path | str) -> tuple[Thesis, str]:
    p = Path(path)
    raw = p.read_bytes()
    version_hash = hashlib.sha256(raw).hexdigest()

    data = yaml.safe_load(raw)
    thesis = Thesis.model_validate(data)

    today_et = date.today()
    if thesis.review_due < today_et:
        raise ThesisStaleError(...)

    return thesis, version_hash
```

**Dedup + batch classification pattern** (`src/signal_system/classifier/news_classifier.py:115-125`, `295-348`):
```python
def _normalize_headline_for_dedup(headline: str) -> str:
    s = " ".join(headline.lower().split())
    return s.rstrip(".!?;:,")

def _headline_dedup_key(ticker: str, headline: str) -> str:
    et_date = datetime.now(_ET).date().isoformat()
    norm = _normalize_headline_for_dedup(headline)
    return hashlib.sha256(f"{ticker}:{et_date}:{norm}".encode("utf-8")).hexdigest()

def classify_headlines(..., *, dedup_seen: set[str] | None = None) -> list[Signal]:
    if dedup_seen is None:
        dedup_seen = set()
    system_prompt = _build_system_prompt(thesis)
    results: list[Signal] = []
    for item in headlines:
        raw = item.get("headline", "")
        if not raw or not str(raw).strip():
            continue
        dedup_key = _headline_dedup_key(ticker, str(raw))
        if dedup_key in dedup_seen:
            continue
        dedup_seen.add(dedup_key)
        signal = classify_headline(...)
        if signal is not None:
            results.append(signal)
    return results
```

**Parse-failure MONITORING pattern** (`src/signal_system/classifier/news_classifier.py:238-257`):
```python
    try:
        parsed, usage = _call_with_retry(sanitized, system_prompt)
    except ValidationError:
        ...
        return _make_parse_failure_signal(ticker, alert_id, raw, raw_text, thesis_version_hash)
```

Persist those parse-failure signals directly with `routing_status="MONITORING"`; do not send them into the router.

**Router purity + caller-owned persistence** (`src/signal_system/router/alert_router.py:21-30`, `src/signal_system/state/repository.py:128-166`):
```python
def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:
    """... Does NOT insert to DB.
    Caller (Phase 6 job) handles insert_signal() and email.
    """
```

```python
def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,
) -> bool:
    cursor.execute("""
        INSERT OR IGNORE INTO signals (
            alert_id, timestamp, agent, severity, ticker, title, body,
            score, routing_status, signal_price_snapshot, model_version,
            thesis_version_hash, demoted_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (...))
```

**Email boundary** (`src/signal_system/delivery/email_sender.py:6-26`):
```python
def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USERNAME
    msg["To"] = config.ALERT_RECIPIENT_EMAIL
    msg.set_content(body)
    ...
```

Use one plain-text digest per run; the helper should return a single `subject/body`, then call `send_email()` once.

---

### `src/signal_system/jobs/discovery.py` (service, batch)

**Primary analog:** `src/signal_system/jobs/daily_close.py`  
**Secondary analog:** `src/signal_system/discovery/discovery_agent.py`

**Run lifecycle pattern** (`src/signal_system/jobs/daily_close.py:13-40`):
```python
def run() -> None:
    run_id = repository.insert_run("daily-close")
    try:
        with heartbeat.heartbeat():
            ...
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
```

**Phase A/B branch pattern** (`src/signal_system/discovery/discovery_agent.py:40-47`, `137-142`):
```python
def score_universe(tickers: list[str], run_id: str, date_iso: str) -> list[Signal]:
    """Phase A: inserts signals directly with routing_status='MONITORING', returns [].
    Phase B: returns list[Signal].
    Always calls repository.update_run_counts() before returning.
    """
    ...
    if config.DISCOVERY_PHASE == "A":
        repository.insert_signal(signal, routing_status="MONITORING")
    else:
        results.append(signal)

    repository.update_run_counts(run_id, tickers_scanned, len(signals_emitted))
    return results
```

**Job orchestration implication:** branch on `config.DISCOVERY_PHASE`, not on whether `score_universe()` returned `[]`.

**Digest send pattern** reuse the same `email_sender.send_email()` call shape from `src/signal_system/jobs/daily_close.py:33-37`.

---

### `src/signal_system/jobs/common.py` *(or similar shared helper)* (utility, transform)

**Analog:** `src/signal_system/router/alert_router.py`  
**Secondary analogs:** `src/signal_system/state/repository.py`, `src/signal_system/delivery/email_sender.py`

**Pure helper boundary** (`src/signal_system/router/alert_router.py:1-5`, `21-30`):
```python
"""Pure logic ... returns (signal, routing_status, demoted_from) tuples.
Does NOT write to DB. Caller (Phase 6 job) handles insert_signal() and email.
"""
```

Use this for digest rendering and tuple-to-persistence helpers: keep helpers side-effect free unless the helper is explicitly the persistence helper.

**Persistence helper pattern** (`src/signal_system/state/repository.py:128-166`): copy the loop shape that hands `signal`, `routing_status`, and `demoted_from` into `repository.insert_signal(...)`.

**Plain-text rendering target** (`src/signal_system/delivery/email_sender.py:16-26`): render to plain text only; no HTML/template system exists.

---

### `src/signal_system/jobs/outcome_backfill.py` (service, batch)

**Primary analog:** `src/signal_system/discovery/discovery_agent.py`  
**Secondary analog:** `src/signal_system/state/repository.py`

**Batch loop pattern** (`src/signal_system/discovery/discovery_agent.py:59-71`, `84-142`):
```python
for ticker in tickers:
    quote = fetch_quote(ticker)
    if quote is None:
        continue
    ...

for ticker in raw_quotes:
    ...
    if composite < SCORE_THRESHOLD_INFORM:
        continue
```

Use the same thin batch style for backfill: fetch candidate rows from repository, skip rows that do not yet qualify, update only eligible rows once.

**Repository-only DB rule** (`src/signal_system/state/repository.py:1-5`, `169-220`):
```python
"""All SQLite access in signal-system goes through this module — no raw SQL elsewhere."""
```

Do not place SQL in `outcome_backfill.py`; add select/update helpers in `repository.py` and call them from the job module.

---

### `src/signal_system/state/repository.py` (service, CRUD)

**Analog:** `src/signal_system/state/repository.py`

**Connection / concurrency pattern** (`src/signal_system/state/repository.py:1-23`):
```python
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn
```

**Idempotent migration pattern** (`src/signal_system/state/repository.py:26-36`, `82-93`):
```python
def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, type_def: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
```

**Run helpers pattern** (`src/signal_system/state/repository.py:169-220`):
```python
def insert_run(job: str) -> str:
    run_id = str(uuid.uuid4())
    started_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    ...

def update_run(run_id: str, status: str) -> None:
    ended_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    ...
```

**Read-query pattern** (`src/signal_system/state/repository.py:260-284`):
```python
def count_delivered_today() -> dict[str, int]:
    today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    cursor.execute("""
        SELECT severity, COUNT(*) FROM signals
        WHERE routing_status = 'DELIVERED'
          AND timestamp LIKE ? || '%'
        GROUP BY severity
    """, (today_iso,))
```

Use the same style for:
- “most recent successful run for job”
- outcome-backfill candidate selection
- idempotent outcome update helpers

---

### `src/signal_system/data/universe.py` (utility, file-I/O)

**Analog:** `src/signal_system/data/universe.py`

**File load pattern** (`src/signal_system/data/universe.py:11-19`, `40-69`):
```python
import csv
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UNIVERSE_PATH = Path(__file__).parent / "universe.csv"

def get_todays_universe() -> list[str]:
    todays_bucket = _today_bucket()
    tickers: list[str] = []

    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["k1_etf"]):
                continue
            ticker = row["ticker"].strip().upper()
            is_core = bool(int(row["core_holding"]))
            in_partition = _md5_bucket(ticker) == todays_bucket
            if is_core or in_partition:
                tickers.append(ticker)
```

Copy this loop shape for `get_core_holdings()`: same CSV read, same K-1 exclusion, same uppercase normalization, but include only `core_holding=1`.

---

### `tests/test_job_orchestration.py` (test, batch)

**Primary analog:** `tests/test_smoke.py`  
**Secondary analog:** `tests/test_discovery_agent.py`

**Global env fixture pattern** (`tests/conftest.py:1-17`):
```python
os.environ.setdefault("FINNHUB_API_KEY", "test_key")
os.environ.setdefault("HEALTHCHECKS_UUID", "test-uuid-1234")
os.environ.setdefault("GMAIL_USERNAME", "test@example.com")
...
```

**Temp DB + mocked I/O pattern** (`tests/test_smoke.py:90-106`, `119-154`):
```python
monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
repository.init_db()

with (
    patch("signal_system.data.finnhub_client.fetch_spy_close", return_value=591.42),
    patch("signal_system.delivery.email_sender.send_email"),
    patch("httpx.post", return_value=MagicMock(raise_for_status=MagicMock())),
):
    daily_close.run()
```

```python
with patch("signal_system.data.finnhub_client.fetch_spy_close", side_effect=ValueError("API down")), \
     patch("httpx.post", mock_post):
    with pytest.raises(ValueError, match="API down"):
        daily_close.run()
```

**Phase-branch assertion pattern** (`tests/test_discovery_agent.py:175-232`):
```python
monkeypatch.setattr(config, "DISCOVERY_PHASE", "A")
...
with patch(...), patch(...), patch("signal_system.state.repository.insert_signal") as mock_insert:
    result = score_universe(["HIGH", "LOW"], run_id, DATE_ISO)

assert result == []
assert mock_insert.call_args.kwargs["routing_status"] == "MONITORING"
```

Use this file for end-to-end job tests: heartbeat pings, zero-alert digest, cap overflow MONITORING inserts, routed tuple persistence, and `__main__.py` dispatch smoke.

---

### `tests/test_outcome_backfill.py` (test, CRUD)

**Analog:** `tests/test_discovery_agent.py`

**DB fixture pattern** (`tests/test_discovery_agent.py:32-36`):
```python
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"
```

**DB assertion pattern** (`tests/test_discovery_agent.py:388-418`):
```python
conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT tickers_scanned, tickers_signaled FROM runs WHERE run_id=?",
    (run_id,),
).fetchone()
conn.close()

assert row[0] == 2
assert row[1] == 1
```

**Deferred-toggle pattern** (`tests/test_discovery_agent.py:351-366`):
```python
import signal_system.discovery.discovery_agent as da
monkeypatch.setattr(da, "SCORE_THRESHOLD_INFORM", 0.0)
...
assert len(result) == 1
```

Use the same style to prove:
- only rows with `acted IS NOT NULL` and null outcome fields are selected
- rows younger than threshold are skipped
- re-running is idempotent
- activation stays deferred unless explicitly invoked

---

### `ops/windows-task-scheduler.md` (config, file-I/O)

**Primary analog:** `mvp-week1.md`  
**Secondary analogs:** `architecture.md`, `risks-and-open-items.md`

**Checklist format** (`mvp-week1.md:117-126`):
```markdown
### Windows Task Scheduler

- [ ] Create a basic task: "Daily Close Signal"
- [ ] Trigger: daily at 4:30 PM ET (adjust for your machine timezone)
- [ ] Action — recommended setup:
  - Program: `C:\path\to\signal-system\.venv\Scripts\python.exe`
  - Arguments: `-m signal_system daily-close`
  - Start in: `C:\path\to\signal-system`
- [ ] Settings: run whether user is logged on or not; wake the computer to run this task
```

**Scheduler context table** (`architecture.md:5-15`):
```markdown
| Job | Cadence | Time (ET) | Purpose |
|---|---|---|---|
| `news-morning` | Weekdays | 9:00 AM | News classifier — pre-open scan |
...
```

**Risk/mitigation bullets** (`risks-and-open-items.md:33-49`):
```markdown
**Mitigation:**
- Configure task to "wake the computer to run this task"
- Configure task to "run whether user is logged on or not"
- Set Windows Update active hours to span your scheduled job times
- Healthchecks.io will catch the failure mode — but you still need to fix it
```

Use the same checklist-heavy style. Add the Phase 6-specific settings from research: `StartWhenAvailable`, single-instance enforcement, and password-backed logon guidance.

---

### `ops/task-scheduler-reference.xml` (config, file-I/O)

**Analog:** none in repo

Borrow only the action fields from `mvp-week1.md:121-124`:
```markdown
- Program: `C:\path\to\signal-system\.venv\Scripts\python.exe`
- Arguments: `-m signal_system daily-close`
- Start in: `C:\path\to\signal-system`
```

Planner should use research + Microsoft schema docs for the XML itself. Keep the artifact scrubbed: no real usernames, absolute personal paths, or secrets.

---

### `ops/operator-setup-checklist.md` (config, file-I/O)

**Analog:** `mvp-week1.md`

**Operator checklist pattern** (`mvp-week1.md:25-29`, `roadmap.md:17-19`):
```markdown
- [ ] **Create Healthchecks.io account**
- [ ] **Set up Gmail app password**
- [ ] **Install uv on the Windows machine**
```

```markdown
- [ ] Register the new job in `__main__.py` JOBS dict
- [ ] Add the three Task Scheduler entries (9 AM, 12 PM, 4:15 PM)
- [ ] Add Healthchecks.io check for the morning run
```

Use the same terse checkbox style for Gmail filter setup, Healthchecks non-email alert setup, and go-live enablement steps.

## Shared Patterns

### Job lifecycle + heartbeat
**Source:** `src/signal_system/jobs/daily_close.py:13-40`, `src/signal_system/monitoring/heartbeat.py:35-57`  
**Apply to:** `news_morning.py`, `discovery.py`, any runnable backfill entrypoint
```python
run_id = repository.insert_run("daily-close")
try:
    with heartbeat.heartbeat():
        ...
        repository.update_run(run_id, "success")
except Exception:
    repository.update_run(run_id, "failed")
    raise
```

```python
@contextlib.contextmanager
def heartbeat():
    _ping("/start")
    try:
        yield
    except Exception:
        _ping("/fail")
        raise
    else:
        _ping("")
```

### Repository-only DB access
**Source:** `src/signal_system/state/repository.py:1-5`, `128-220`  
**Apply to:** all jobs/helpers
```python
"""All SQLite access in signal-system goes through this module — no raw SQL elsewhere."""
```

### ET timestamping
**Source:** `src/signal_system/jobs/daily_close.py:19-20`, `src/signal_system/data/universe.py:31-37`, `src/signal_system/state/repository.py:171-173`  
**Apply to:** all time-window, run, and digest code
```python
now_et = datetime.now(ZoneInfo("America/New_York"))
started_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
return datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday % 3
```

### Plain-text single-email delivery
**Source:** `src/signal_system/delivery/email_sender.py:6-26`  
**Apply to:** `news_morning.py`, Discovery Phase B digest
```python
def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg.set_content(body)
```

### Test harness setup
**Source:** `tests/conftest.py:1-17`, `tests/test_discovery_agent.py:32-36`  
**Apply to:** all new tests
```python
os.environ.setdefault("FINNHUB_API_KEY", "test_key")
...
monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
repository.init_db()
```

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `ops/task-scheduler-reference.xml` | config | file-I/O | Repo has prose guidance only; no existing XML artifact or scheduler export to copy. |

## Metadata

**Analog search scope:** `src/signal_system/`, `tests/`, repo root docs (`README.md`, `architecture.md`, `mvp-week1.md`, `risks-and-open-items.md`, `roadmap.md`)  
**Files scanned:** 20  
**Pattern extraction date:** 2026-05-16
