# Architecture Patterns

**Domain:** Rules-based investment signal / alert system (alert-only, multi-agent)
**Researched:** 2026-05-14
**Confidence:** HIGH — derived from existing codebase, PROJECT.md constraints, and established pipeline design principles

---

## Recommended Architecture

The system follows a **Produce → Route → Deliver** pipeline. Two agents produce scored signals independently. The router is the single gatekeeper before any email is sent. The existing delivery layer (`email_sender.py`) is unchanged — the router calls it, not the agents.

```
┌─────────────────────────────────────────────────────────────────┐
│  Job Layer (jobs/)                                              │
│  news_morning.py          discovery.py         daily_close.py  │
│       │                        │                      │         │
│  heartbeat()             heartbeat()           heartbeat()      │
└───────┼────────────────────────┼──────────────────────┼────────┘
        │                        │                      │
        ▼                        ▼                      │
┌───────────────┐      ┌──────────────────┐            │
│ NewsClassifier│      │ DiscoveryAgent   │            │
│ agents/       │      │ agents/          │            │
│               │      │                  │            │
│ Reads:        │      │ Reads:           │            │
│  thesis.yaml  │      │  universe.py     │            │
│  finnhub news │      │  finnhub quotes  │            │
│  Claude API   │      │  Claude API      │            │
│               │      │                  │            │
│ Returns:      │      │ Returns:         │            │
│  [Signal]     │      │  [Signal]        │            │
└───────┬───────┘      └────────┬─────────┘            │
        │                       │                      │
        └──────────┬────────────┘                      │
                   ▼                                   │
        ┌──────────────────────┐                       │
        │   Alert Router       │                       │
        │   routing/           │                       │
        │   alert_router.py    │                       │
        │                      │                       │
        │  - Query today's     │                       │
        │    delivered signals │                       │
        │  - Enforce budget:   │                       │
        │    1 ACTION_REQUIRED │                       │
        │    3 INFORMATIONAL   │                       │
        │  - Slot competition  │                       │
        │    (score tiebreak)  │                       │
        │  - Write MONITORING  │                       │
        │    for suppressed    │                       │
        │                      │                       │
        │ Returns: [Signal]    │                       │
        │ (approved only)      │                       │
        └──────────┬───────────┘                       │
                   │                                   │
                   ▼                                   ▼
        ┌──────────────────────────────────────────────────────┐
        │   Delivery Layer  (delivery/email_sender.py)         │
        │   Unchanged from MVP — receives rendered text only   │
        └──────────────────────────────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │  SQLite              │
        │  state/repository.py │
        │  signals + runs +    │
        │  wash_sale tables    │
        └──────────────────────┘
```

---

## Component Boundaries

| Component | File | Responsibility | What It Does NOT Do |
|-----------|------|----------------|---------------------|
| `jobs/news_morning.py` | jobs layer | Orchestrate the news pipeline: insert run, wrap in heartbeat, call classifier, pass output to router, call delivery | Does not classify, score, or format email body |
| `jobs/discovery.py` | jobs layer | Orchestrate the discovery pipeline: insert run, wrap in heartbeat, call agent, in Phase A write to SQLite only (no router call) | Does not score tickers or decide delivery |
| `agents/news_classifier.py` | agent layer | Accept raw headlines from Finnhub, call Claude API with thesis.yaml taxonomy, return list of `Signal` dataclasses with scores | Does not check daily budget, does not send email |
| `agents/discovery_agent.py` | agent layer | Accept universe slice for today, fetch Finnhub data, score with 35/30/25/10 weights, return list of `Signal` dataclasses | Does not check daily budget, does not send email |
| `routing/alert_router.py` | router layer | Accept signals from one or both agents, query today's delivered count from repository, enforce budget, return approved subset, write suppressed signals as MONITORING | Does not call Claude, does not know about email |
| `data/universe.py` | data layer | Maintain ~1,500 ticker list with `core_holding` flag, K-1 exclusions, return today's scan slice (deterministic 1/3 rotation + all core holdings) | Does not fetch prices |
| `data/finnhub_client.py` | data layer | All Finnhub API calls — extend with news fetch, company news, rate-limit enforcement | Does not interpret data |
| `state/repository.py` | state layer | All SQLite reads and writes — extend with budget query, wash_sale table, MONITORING tag support | No business logic |
| `delivery/email_sender.py` | delivery layer | SMTP send — unchanged | No routing decisions |
| `monitoring/heartbeat.py` | monitoring layer | healthchecks.io pings — unchanged | No job logic |
| `config.py` | config layer | Load and validate all env vars — extend with `THESIS_PATH` if thesis.yaml location is env-configurable | No side effects |

---

## Data Flow

### News Morning Pipeline

```
1. news_morning.py  →  repository.insert_run("news-morning")
2. news_morning.py  →  heartbeat()/start ping
3. news_morning.py  →  finnhub_client.fetch_company_news(tickers)
4. news_morning.py  →  news_classifier.classify(headlines, thesis)
                          ├── thesis_loader.load()  →  thesis.yaml (validate review_due)
                          └── claude_api.call(prompt_with_delimited_headlines)
                               └── returns: List[Signal]
5. news_morning.py  →  alert_router.route(signals, agent="NEWS_CLASSIFIER")
                          ├── repository.count_delivered_today(severity)
                          ├── enforce budget (1 ACTION_REQUIRED, 3 INFORMATIONAL)
                          └── returns: (approved: List[Signal], suppressed: List[Signal])
6. news_morning.py  →  repository.insert_signal(each approved, status="DELIVERED")
7. news_morning.py  →  repository.insert_signal(each suppressed, status="MONITORING")
8. news_morning.py  →  email_sender.send_email(digest of approved, or zero-alert notice)
9. news_morning.py  →  repository.update_run(run_id, "success")   # inside heartbeat
10. heartbeat  →  /success ping
```

### Discovery Pipeline (Phase A — logs only)

```
1. discovery.py  →  repository.insert_run("discovery")
2. discovery.py  →  heartbeat()/start ping
3. discovery.py  →  universe.today_slice()          # 1/3 rotation + all core holdings
4. discovery.py  →  finnhub_client.fetch_quotes(tickers)   # rate-limited
5. discovery.py  →  discovery_agent.score(tickers, quotes)
                       └── returns: List[Signal] (scored, not yet routed)
6. Phase A:  →  repository.insert_signal(each, status="MONITORING")  # no router, no email
   Phase B:  →  alert_router.route(signals, agent="DISCOVERY")       # after 2-week calibration
7. discovery.py  →  repository.update_run(run_id, "success")
8. heartbeat  →  /success ping
```

---

## Canonical `Signal` Dataclass

All agents return a list of this type. The router consumes it. Repository serializes it. Nothing else.

```python
# src/signal_system/models.py  (new file)
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Signal:
    agent: str                    # "NEWS_CLASSIFIER" | "DISCOVERY" | "DAILY_CLOSE"
    severity: str                 # "ACTION_REQUIRED" | "INFORMATIONAL" | "MONITORING"
    title: str
    ticker: Optional[str] = None
    body: Optional[str] = None
    score: Optional[float] = None
    suggested_action: Optional[str] = None
    # Router sets this; agents do not touch it
    routing_status: str = "PENDING"   # "DELIVERED" | "MONITORING" | "SUPPRESSED"
```

This type is the contract between agents and the router. If it needs a field, add it here — not scattered across agent implementations.

---

## Alert Router Design

### Budget Enforcement Logic

```python
# routing/alert_router.py
def route(signals: list[Signal], date: date) -> tuple[list[Signal], list[Signal]]:
    """
    Returns (approved, suppressed).
    Approved signals are safe to deliver and insert as DELIVERED.
    Suppressed signals must be inserted as MONITORING.
    """
    delivered_action = repository.count_delivered_today("ACTION_REQUIRED", date)
    delivered_info   = repository.count_delivered_today("INFORMATIONAL", date)

    action_budget = max(0, 1 - delivered_action)
    info_budget   = max(0, 3 - delivered_info)

    # Sort descending by score within each severity tier
    action_signals = sorted(
        [s for s in signals if s.severity == "ACTION_REQUIRED"],
        key=lambda s: s.score or 0.0, reverse=True
    )
    info_signals = sorted(
        [s for s in signals if s.severity == "INFORMATIONAL"],
        key=lambda s: s.score or 0.0, reverse=True
    )

    approved = action_signals[:action_budget] + info_signals[:info_budget]
    suppressed = action_signals[action_budget:] + info_signals[info_budget:]

    # Demoted ACTION_REQUIRED that couldn't fit → tag as MONITORING, not INFORMATIONAL
    # (preserves original severity for measurement; only routing_status changes)
    for s in suppressed:
        s.routing_status = "MONITORING"
    for s in approved:
        s.routing_status = "DELIVERED"

    return approved, suppressed
```

Key design choice: the router never mutates `severity`. It only sets `routing_status`. This preserves measurement integrity — a suppressed `ACTION_REQUIRED` signal must be recorded as such for the quarterly review to be meaningful.

The router queries the DB for today's delivered count rather than maintaining in-memory state, so it is safe across multiple job invocations in the same day (news-morning and discovery both calling it).

---

## thesis.yaml Structure and Validation

### File Structure

```yaml
# thesis.yaml
version: 1
review_due: "2026-07-01"        # ISO date — classifier refuses to run if past

pillars:
  - id: fed_policy
    label: "Federal Reserve Policy"
    weight: 1.0
    keywords:
      - "interest rate"
      - "federal funds rate"
      - "FOMC"
      - "rate cut"
      - "rate hike"
    sentiment_direction: negative_rates_negative   # rising rates hurt equity positions
    alert_threshold: 0.6

  - id: ai_capex
    label: "AI Capital Expenditure Cycle"
    weight: 1.2
    keywords:
      - "data center"
      - "GPU"
      - "inference"
      - "hyperscaler"
    sentiment_direction: positive
    alert_threshold: 0.5

  - id: energy_transition
    label: "Energy Transition"
    weight: 0.9
    keywords:
      - "renewable"
      - "EV"
      - "grid"
      - "IRA"
    sentiment_direction: positive
    alert_threshold: 0.55

holdings:
  core:
    - SPY
    - QQQ
    - NVDA
    - MSFT
  extended:
    - AAPL
    - AMZN
```

### Runtime Validation

```python
# data/thesis_loader.py  (new file)
import yaml
from datetime import date
from pathlib import Path

class ThesisStaleError(RuntimeError):
    """Raised when thesis.yaml review_due date has passed."""

def load(path: Path) -> dict:
    with open(path) as f:
        thesis = yaml.safe_load(f)

    # Gate: refuse to run if review is overdue
    review_due = date.fromisoformat(thesis["review_due"])
    if date.today() > review_due:
        raise ThesisStaleError(
            f"thesis.yaml review_due was {review_due}. "
            "Update the thesis and advance the review_due date before running."
        )

    # Structural validation (fail fast, not silently)
    assert "pillars" in thesis, "thesis.yaml missing 'pillars' key"
    assert len(thesis["pillars"]) > 0, "thesis.yaml has no pillars defined"
    for p in thesis["pillars"]:
        assert "id" in p and "label" in p and "keywords" in p, \
            f"Pillar missing required keys: {p}"

    return thesis
```

The `ThesisStaleError` propagates up through the job's heartbeat context manager, which trips the `/fail` ping. This is intentional — a stale thesis is a silent failure mode the operator needs to see on healthchecks.io.

The path to `thesis.yaml` should be configurable via `THESIS_PATH` env var with a default of `./thesis.yaml` relative to the project root. Add to `config.py`.

---

## Ticker Universe Rotation

### Design

```python
# data/universe.py
import hashlib
from datetime import date
from pathlib import Path
from typing import NamedTuple

class Ticker(NamedTuple):
    symbol: str
    core_holding: bool

# K-1 exclusions — filter at universe level, never at alert time
K1_EXCLUSIONS = frozenset({"USO", "UNG", "DBC", "GSG"})

# Full universe — loaded from a flat list or embedded constant
# ~1,500 tickers; for now a curated list, later could load from CSV
_UNIVERSE: list[Ticker] = [
    # populated from data/tickers.csv or inline list
    # core holdings flagged explicitly
]

def today_slice(reference_date: date | None = None) -> list[str]:
    """
    Return tickers to scan today.

    Rule:
    - Core holdings: always included
    - Remaining universe: deterministic 1/3 rotation based on date
      (each ticker scanned every 3 calendar days, not trading days)

    K-1 exclusions are filtered out before returning.
    """
    d = reference_date or date.today()
    day_index = d.toordinal()  # stable integer, advances daily

    non_core = [t for t in _UNIVERSE if not t.core_holding]
    core = [t.symbol for t in _UNIVERSE if t.core_holding]

    # Deterministic partition: ticker assigned to bucket by its symbol hash
    bucket = day_index % 3
    rotation_slice = [
        t.symbol for t in non_core
        if int(hashlib.md5(t.symbol.encode()).hexdigest(), 16) % 3 == bucket
    ]

    all_tickers = core + rotation_slice
    return [sym for sym in all_tickers if sym not in K1_EXCLUSIONS]
```

### Rotation Design Rationale

Using `hash(symbol) % 3` rather than a position-based index means the assignment is stable across universe list reorderings. Adding a new ticker doesn't shift every other ticker to a different bucket the next day. The `md5` hash is deterministic across Python processes and platforms (unlike Python's built-in `hash()` which randomizes per process).

The `reference_date` parameter makes this fully testable without mocking the system clock.

### Rate-Limit Gate in Finnhub Client

```python
# data/finnhub_client.py — extend existing module
import time

_CALLS_PER_MINUTE = 55  # leave 5 calls/min headroom
_MIN_INTERVAL = 60.0 / _CALLS_PER_MINUTE  # ~1.09 seconds between calls

def fetch_quote(symbol: str) -> dict:
    """Rate-limited single quote fetch."""
    time.sleep(_MIN_INTERVAL)
    return _client.quote(symbol)

def fetch_quotes_bulk(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch quotes for all symbols with rate limiting.
    Returns dict keyed by symbol.
    """
    results = {}
    for symbol in symbols:
        try:
            results[symbol] = fetch_quote(symbol)
        except Exception as exc:
            logger.warning("Quote fetch failed for %s: %s", symbol, exc)
            results[symbol] = {}
    return results
```

The sleep-based rate limiter is simple and sufficient for a single-process Windows Task Scheduler runner. If the scan window becomes too large (>1,500 tickers × 1.09s = ~27 min), the universe slice should be reduced, not the sleep interval.

---

## File Layout for New Components

```
src/signal_system/
├── __main__.py                     # extend: add "news-morning", "discovery" to JOBS dict
├── config.py                       # extend: add THESIS_PATH setting
├── models.py                       # NEW: Signal dataclass (shared contract)
│
├── agents/
│   ├── __init__.py
│   ├── news_classifier.py          # NEW: thesis-driven news classification via Claude
│   └── discovery_agent.py         # NEW: ticker scoring with 35/30/25/10 weights
│
├── routing/
│   ├── __init__.py
│   └── alert_router.py             # NEW: budget enforcement, slot competition
│
├── jobs/
│   ├── daily_close.py              # existing — unchanged
│   ├── news_morning.py             # NEW: orchestrates news pipeline
│   └── discovery.py                # NEW: orchestrates discovery pipeline (Phase A: log-only)
│
├── data/
│   ├── finnhub_client.py           # extend: bulk quote fetch, news fetch, rate limiter
│   ├── universe.py                 # NEW: ticker list, core holdings, K-1 exclusions, rotation
│   └── thesis_loader.py            # NEW: load + validate thesis.yaml, review_due gate
│
├── state/
│   └── repository.py               # extend: count_delivered_today(), wash_sale table,
│                                   #   routing_status column on signals
│
└── delivery/
    └── email_sender.py             # existing — unchanged (receives rendered text)
```

---

## Build Order (Dependency Graph)

Build bottom-up: shared types before agents, agents before router, router before jobs.

```
Phase 1 — Shared types and schema
  1. models.py                      (no dependencies)
  2. thesis.yaml + thesis_loader.py (no dependencies)

Phase 2 — Data layer extensions
  3. universe.py                    (no dependencies)
  4. finnhub_client.py extensions   (depends on: config.py — already done)
  5. repository.py extensions       (add routing_status col, count_delivered_today, wash_sale)

Phase 3 — Agents (both are independent)
  6a. agents/news_classifier.py     (depends on: models.py, thesis_loader.py, finnhub_client.py)
  6b. agents/discovery_agent.py     (depends on: models.py, universe.py, finnhub_client.py)

Phase 4 — Router
  7. routing/alert_router.py        (depends on: models.py, repository.py)

Phase 5 — Jobs
  8a. jobs/news_morning.py          (depends on: news_classifier, alert_router, email_sender, repository)
  8b. jobs/discovery.py             (depends on: discovery_agent, repository; alert_router in Phase B)
  9.  __main__.py extension         (add new job names to JOBS dict)
```

Steps 6a and 6b can be built in parallel. Steps 8a and 8b can be built in parallel.

---

## Patterns to Follow

### Pattern: Job = thin orchestrator

Jobs contain only: `insert_run`, `heartbeat()` context, calls to agents/router/delivery, `update_run`. No scoring, no routing logic, no prompt construction. Keeps all business logic testable without mocking the job layer.

### Pattern: Agents return, never send

Agents return `List[Signal]`. They never call `email_sender`, never call `repository.insert_signal`. The job layer owns the persistence step after routing. This makes agents unit-testable with no DB or SMTP mocking.

### Pattern: Repository queries replace in-memory budget state

The router queries `repository.count_delivered_today()` rather than tracking counts in memory. This is safe when two jobs in the same day both invoke the router (news-morning at 8am, discovery at 10am). In-memory state would be wrong on the second invocation.

### Pattern: Phase A / Phase B flag in discovery job

The discovery job checks a config flag (`DISCOVERY_PHASE = "A"` in `.env`) to decide whether to call the router or go straight to MONITORING insert. This makes Phase B promotion a config change, not a code change.

---

## Anti-Patterns to Avoid

### Anti-Pattern: Agents checking the daily budget

If `news_classifier.py` queries the DB to check whether the budget allows emission, agents become coupled to the delivery policy. Budget policy belongs in the router exclusively. Agents should emit every signal they find; the router decides what gets through.

### Anti-Pattern: Router re-classifying signals

The router must not call Claude or re-interpret signal content. Its only inputs are the signals from agents plus the DB count of today's delivered signals. Adding classification logic to the router collapses the agent/router boundary.

### Anti-Pattern: Raw SQL outside repository.py

The `count_delivered_today()` budget query is a new SQL statement — it belongs in `repository.py`, not inline in `alert_router.py`. This is already a stated constraint in CLAUDE.md; the router should call a repository function.

### Anti-Pattern: thesis.yaml path hardcoded in agents

The classifier should receive a loaded thesis dict (or the path via config), not hard-code `./thesis.yaml`. This enables testing with a fixture thesis and running from arbitrary CWDs (Windows Task Scheduler runs from the system CWD).

### Anti-Pattern: Hash-based rotation with Python's built-in hash()

`hash("AAPL")` varies between Python processes due to hash randomization. Use `hashlib.md5` for the bucket assignment to guarantee determinism across days and restarts.

---

## Repository Extensions Required

Two additions to `repository.py` are required before the router can work:

1. `routing_status` column on `signals` table — add to `CREATE TABLE` and `insert_signal` signature. Values: `"DELIVERED"`, `"MONITORING"`, `"SUPPRESSED"`.

2. `count_delivered_today(severity: str, date: date) -> int` — query `signals` table for rows where `timestamp` matches today (ET), `severity = ?`, and `routing_status = "DELIVERED"`.

3. `wash_sale` table — add in same `init_db()` migration:
   ```sql
   CREATE TABLE IF NOT EXISTS wash_sale (
       id TEXT PRIMARY KEY,
       account TEXT NOT NULL,     -- "schwab_main" | "schwab_secondary" | "roth_ira" | "hsa"
       ticker TEXT NOT NULL,
       sale_date TEXT NOT NULL,
       wash_window_end TEXT NOT NULL,   -- sale_date + 30 days
       sale_price REAL,
       created_at TEXT NOT NULL
   )
   ```

The `account` column must be present from day one as stated in CLAUDE.md — retrofitting across 4 accounts is a painful schema migration.

---

## Scalability Considerations

This system is explicitly single-machine, single-operator. No scalability concerns apply. The relevant operational bounds are:

| Concern | Bound | Current Approach |
|---------|-------|-----------------|
| Finnhub rate limit | 60 calls/min | `time.sleep()` between calls in bulk fetch |
| Universe scan time | ~500 tickers/day × 1.09s = ~9 min | Acceptable within Task Scheduler window |
| SQLite concurrency | Single writer at a time | WAL mode handles two jobs if Task Scheduler overlaps |
| Claude API tokens | ~500 chars/headline × N headlines | Headline cap + delimiter guards against runaway cost |
| Email delivery | 1 digest/job/day | No rate concern at current volume |

---

## Sources

- Existing codebase: `src/signal_system/` (reviewed directly)
- Project constraints: `.planning/PROJECT.md` and `CLAUDE.md`
- Design decisions drawn from existing patterns in `jobs/daily_close.py`, `monitoring/heartbeat.py`, and `state/repository.py`
- Architecture confidence: HIGH — all recommendations are grounded in the existing implementation patterns and explicitly stated project constraints
