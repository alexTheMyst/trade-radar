# Phase 4: Discovery Agent — Research

**Phase:** 04 - Discovery Agent
**Confidence:** HIGH
**Researched against:** live codebase

## Summary

The Discovery Agent implementation is straightforward with two Wave 0 prerequisites: `signal_price_snapshot` is absent from the Signal dataclass (DB schema and INSERT statement have the column but it's hardcoded to None), and `insert_signal()` needs a `routing_status` kwarg for Phase A's direct MONITORING insert. All scoring factors can be computed from the existing `_fetch_single_quote()` return value — no new Finnhub endpoints needed.

## Decisions Confirmed by Codebase

| Decision | Status | Evidence |
|---|---|---|
| /quote returns dp, v, h, l, c | [VERIFIED] | `finnhub_client.py:65-82` — `_fetch_single_quote()` returns full response dict |
| insert_run() returns str | [VERIFIED] | `repository.py:158-159` — `str(uuid.uuid4())` |
| routing_status is NOT on Signal | [VERIFIED] | `models.py:22` — docstring explicitly states this |
| sub_scores: dict[str, float] on Signal | [VERIFIED] | `models.py:33` |
| DB column signal_price_snapshot exists | [VERIFIED] | `repository.py:84` — `_ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")` |
| DISCOVERY_PHASE config var exists | [VERIFIED] | `config.py` |
| Test patterns use monkeypatch for DB_PATH | [VERIFIED] | `conftest.py` + existing tests |

## Technical Findings

### Q1: Finnhub /quote fields [VERIFIED]

`_fetch_single_quote(ticker)` at `finnhub_client.py:65` already returns the full Finnhub quote response dict. Fields available:
- `c` — current close price
- `dp` — daily % change
- `v` — volume
- `h` — high
- `l` — low
- `o` — open
- `pc` — previous close

**Current validation:** only checks `c > 0`. Score-floor guard needs to also check `dp is not None`, `h >= l > 0`, `v is not None`.

**Action:** add a public `fetch_quote(ticker: str) -> dict | None` wrapper that calls `_fetch_single_quote()` and validates all required fields for Discovery. OR expose `_fetch_single_quote` directly. Prefer a new `fetch_quote()` that applies score-floor validation internally.

### Q2: insert_signal() signature [VERIFIED]

Current: `def insert_signal(signal: Signal) -> bool:`

`routing_status` and `signal_price_snapshot` are hardcoded to `None` in lines 146-147. To support Phase A:

**Required changes:**
1. Add `signal_price_snapshot: float | None = None` to Signal dataclass (legitimate signal data, consistent with model_version / thesis_version_hash pattern)
2. Add `routing_status: str | None = None` kwarg to `insert_signal()` — caller passes `"MONITORING"` in Phase A

### Q3: Cross-sectional ranking [ASSUMED — stdlib]

Two-pass approach: fetch all tickers first, then rank. Implementation:

```python
# Sort by (-raw_value, ticker) → descending value, alphabetical tiebreak
sorted_tickers = sorted(
    [(ticker, val) for ticker, val in raw_values.items()],
    key=lambda x: (-x[1], x[0])
)
n = len(sorted_tickers)
if n == 0:
    return {}
if n == 1:
    return {sorted_tickers[0][0]: 0.5}
ranks = {ticker: 1.0 - (i / (n - 1)) for i, (ticker, _) in enumerate(sorted_tickers)}
```

Edge cases:
- 0 tickers → return `[]` from score_universe immediately
- 1 ticker → rank = 0.5
- Equal values → alphabetical ticker order (from sort key)

### Q4: _ensure_column() pattern [VERIFIED]

```python
# repository.py:26-35
def _ensure_column(cursor, table: str, column: str, col_type: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cursor.fetchall()}
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
```

Add inside `init_db()`:
```python
_ensure_column(cursor, "runs", "tickers_scanned", "INTEGER")
_ensure_column(cursor, "runs", "tickers_signaled", "INTEGER")
```

### Q5: /company-news date range [VERIFIED]

`fetch_company_news(ticker, from_date, to_date)` at `finnhub_client.py:125-145`. Takes `date` objects.

```python
from datetime import date, timedelta
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo("America/New_York")).date()
news = fetch_company_news(ticker, today - timedelta(days=7), today)
news_count = len(news) if news is not None else 0
```

### Q6: Test patterns [VERIFIED]

- `conftest.py` sets dummy env vars at module level via `os.environ.setdefault()`
- DB isolation: `monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")` then call `repository.init_db()`
- Finnhub mocks: `unittest.mock.patch("signal_system.discovery.discovery_agent.fetch_quote")`
- Frozen dataclass: no mutable state to mock on Signal itself

### Q7: signal_price_snapshot [VERIFIED/GAP]

DB column exists (`repository.py:84`), INSERT statement has slot (line 134), currently hardcoded `None` (line 147). Signal dataclass does NOT have this field yet.

**Resolution:** Add `signal_price_snapshot: float | None = None` to Signal. Update INSERT to use `signal.signal_price_snapshot`. Discovery agent sets `signal_price_snapshot=quote['c']` at Signal construction time.

### Q8: run_id type [VERIFIED]

`insert_run()` returns `str` (UUID v4). `score_universe(tickers, run_id, date_iso)` — `run_id: str` is correct.

### Q9: __init__.py pattern [VERIFIED]

`src/signal_system/classifier/__init__.py` exports `classify_headlines`. Mirror:
```python
# src/signal_system/discovery/__init__.py
from .discovery_agent import score_universe
__all__ = ["score_universe"]
```

### Q10: Validation Architecture

**Framework:** pytest + unittest.mock

| Test | What to verify |
|---|---|
| `test_score_computation` | Weighted sum: dp_rank×0.35 + vol_rank×0.30 + range_rank×0.25 + news_rank×0.10 |
| `test_score_floor_invalid_quote` | Ticker with c=0 or dp=None → excluded, no Signal |
| `test_score_floor_missing_field` | Ticker with h==l → range_position=0.0 (NOT skip — only quote required) |
| `test_phase_a_monitoring_insert` | DISCOVERY_PHASE=A → insert_signal called with routing_status="MONITORING" |
| `test_phase_b_returns_signals` | DISCOVERY_PHASE=B → returns list[Signal], no insert |
| `test_cross_sectional_ranking_ties` | Two tickers same dp → alphabetical tiebreak (A before B → B gets lower rank) |
| `test_news_activity_zero` | fetch_company_news returns [] → news_rank factor = 0.0 rank |
| `test_empty_universe` | score_universe([]) → [] |
| `test_single_ticker` | 1 valid ticker → all ranks = 0.5 |
| `test_below_threshold_suppressed` | Score < 60 → no Signal emitted, not even in Phase A |
| `test_action_required_severity` | Score >= 80 → severity="ACTION_REQUIRED" |
| `test_informational_severity` | 60 <= score < 80 → severity="INFORMATIONAL" |
| `test_update_run_counts` | repository.update_run_counts() updates correct run row |

## Implementation Approach

`score_universe(tickers, run_id, date_iso)` runs in 3 passes:

**Pass 1 — Fetch:** For each ticker, call `fetch_quote()` and `fetch_company_news()`. Collect into `raw: dict[str, dict]` (skipped tickers absent). Track `tickers_scanned` count.

**Pass 2 — Rank:** For each factor, build a dict of `{ticker: raw_value}` from `raw`. Apply cross-sectional ranking. Build `ranked: dict[str, dict[str, float]]` with per-factor ranks.

**Pass 3 — Score and emit:** For each ticker in `ranked`, compute composite score. Apply threshold (≥60). Construct `Signal`. In Phase A: `insert_signal(signal, routing_status="MONITORING")`. In Phase B: append to return list.

After all passes, call `update_run_counts(run_id, tickers_scanned, tickers_signaled)`.

## Risks / Unknowns

| Item | Risk | Mitigation |
|---|---|---|
| volume cross-sectional rank has no historical baseline | LOW | Accepted in CONTEXT.md D-02 |
| /company-news rate limit at 500 tickers | LOW | Token bucket handles; ~18 min wall time accepted (D-21) |
| `h == l` (halted/illiquid stock) | LOW | range_position = 0.0, ticker still scored (D-06) |

