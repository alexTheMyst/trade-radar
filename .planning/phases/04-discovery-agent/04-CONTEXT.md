# Phase 4: Discovery Agent — Context

**Gathered:** 2026-05-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the Discovery Agent that scores today's rotation universe tickers across 4 weighted factors using Finnhub free-tier data. The agent enforces a score-floor guard for missing candle data, operates in Phase A (logs-only, direct DB insert) or Phase B (Signals returned to caller for router), and never sends email. Phase 4 ends when `score_universe()` is importable and returns `Signal` objects with per-factor sub-scores — job wiring happens in Phase 6.

</domain>

<decisions>
## Implementation Decisions

### Scoring Factors (DISC-01)

- **D-01:** Four scoring factors and weights (configurable, defaulting to):
  - `price_momentum` (35%): 20-day return from `/stock/candle` (OHLCV, daily resolution, 20 bars)
  - `volume_surge` (30%): today's volume ÷ 20-day average volume from the same 20-bar candle response
  - `range_position` (25%): `(current - 52w_low) / (52w_high - 52w_low)` from `/stock/metric` basicFinancials (`52WeekHigh`, `52WeekLow`); current price taken from the last candle's close
  - `news_activity` (10%): count of news items in the last 7 days from `/company-news` (already available via `fetch_company_news()`)

- **D-02:** Sub-score mapping is **cross-sectional rank normalization** — after fetching raw values for all valid tickers, rank each ticker's raw value per factor against all other valid tickers today. Top = 1.0, bottom = 0.0, linear between. Tickers with equal raw values are ranked alphabetically by ticker symbol (consistent tiebreak).

- **D-03:** Composite score formula: `(35 * momentum_rank + 30 * volume_rank + 25 * range_rank + 10 * news_rank)`. Result is in `[0.0, 100.0]`. Stored as `Signal.score`.

- **D-04:** Per-factor sub-scores are stored in `Signal.sub_scores` as a dict of ranks (not raw values):
  `{"price_momentum": 0.87, "volume_surge": 0.72, "range_position": 0.54, "news_activity": 0.30}`

### Score-Floor Guard (DISC-02)

- **D-05:** **Candle history is the only required factor.** If `/stock/candle` returns 403/404 or insufficient bars (< 20) for a ticker, skip that ticker entirely — no Signal, no MONITORING row.

- **D-06:** `/stock/metric` (range_position) and `/company-news` (news_activity) are **optional**. If unavailable or empty:
  - `range_position_rank = 0.0` (worst rank — conservative, not artificially boosting)
  - `news_activity_rank = 0.0`
  - Composite score still computed from all 4 factors; the missing factor contributes its worst-case 0.0 rank.

- **D-07:** Candle fetch must return at least 20 valid OHLCV bars with `v > 0`. A 403/404, empty response, or fewer than 20 valid bars drops the ticker.

### Score-to-Severity Thresholds (DISC-03, DISC-04)

- **D-08:** Composite score → severity mapping (Phase B only):
  - `score ≥ 80` → `ACTION_REQUIRED`
  - `60 ≤ score < 80` → `INFORMATIONAL`
  - `score < 60` → no Signal emitted (ticker silently dropped, not logged)

- **D-09:** These thresholds are initial guesses calibrated after Phase A observation. The researcher should treat them as Phase A starting values — plan for them to be configurable via config or constants (not hardcoded deep in logic).

### Phase A Behavior (DISC-03)

- **D-10:** In Phase A (`DISCOVERY_PHASE=A`):
  - Same thresholds apply — only tickers scoring ≥ 60 emit a Signal.
  - `Signal.severity` is computed normally (ACTION_REQUIRED or INFORMATIONAL based on score).
  - The agent **bypasses the router entirely** and calls `repository.insert_signal()` directly with `routing_status="MONITORING"`, overriding the computed severity's natural routing outcome.
  - Phase A Signal rows can be queried later: "what would Phase B have delivered?" by inspecting `severity` vs `routing_status`.

- **D-11:** In Phase B (`DISCOVERY_PHASE=B`):
  - `score_universe()` returns `list[Signal]` to the caller.
  - The caller (Phase 6 job) passes Signals to the Alert Router; the router sets `routing_status`.
  - Phase 4 itself has no routing logic in Phase B mode.

- **D-12:** `DISCOVERY_PHASE` is read from `config.DISCOVERY_PHASE` (already wired in Phase 1, defaults to `"A"`). The discovery agent reads this at call time — no agent-level state, no module-level caching of the phase value.

### Scan Audit Trail (DISC-05)

- **D-13:** Add two columns to the `runs` table via `_ensure_column()` in `init_db()`:
  - `tickers_scanned INTEGER` — count of tickers fetched (candles attempted)
  - `tickers_signaled INTEGER` — count of tickers that produced a Signal (score ≥ 60)
  - These are written via a new `repository.update_run_counts(run_id, tickers_scanned, tickers_signaled)` function.

### Module Structure

- **D-14:** Agent lives at `src/signal_system/discovery/discovery_agent.py`. Public interface:
  ```python
  def score_universe(
      tickers: list[str],
      run_id: str,
      date_iso: str,
  ) -> list[Signal]:
      ...
  ```
  Caller provides tickers (from `get_todays_universe()`), run_id (for DB audit), and date_iso (for `compute_alert_id` and DB timestamp). The agent writes `tickers_scanned`/`tickers_signaled` to `runs` before returning.

- **D-15:** `alert_id` uses `compute_alert_id(ticker, date_iso, "discovery", "discovery_agent")`. The "rule" component is fixed to `"discovery"` — consistent across weight changes so duplicate suppression works correctly across reruns on the same day.

- **D-16:** `Signal.agent` = `"discovery_agent"`. `Signal.ticker` = ticker symbol. `Signal.model_version` = `None` (no LLM call). `Signal.thesis_version_hash` = `None` (not thesis-driven).

- **D-17:** `Signal.title` format: `f"{ticker}: Discovery score {composite:.0f}"`. `Signal.body` format: per-factor sub-score breakdown, e.g. `"momentum=0.87 volume=0.72 range=0.54 news=0.30"`.

- **D-18:** `signal_price_snapshot` = last candle's close price (already fetched — no extra API call).

### Data Fetching Strategy

- **D-19:** Fetching order per ticker (3 calls when all succeed):
  1. `/stock/candle` — 20 daily bars, `resolution=D`, `count=20` (or date range). Required.
  2. `/stock/metric` — `metric=all`, extract `52WeekHigh` and `52WeekLow`. Optional.
  3. `/company-news` — last 7 days. Optional. Use existing `fetch_company_news()`.

- **D-20:** At 55 calls/min and ~3 calls/ticker × ~500 tickers: ~27 minutes per run. This is longer than the "9–18 minutes" estimate in CLAUDE.md (which assumed 1-2 calls/ticker). Acceptable for a nightly/daily job but researcher should flag this and evaluate whether to combine metric into candle call or reduce calls.

- **D-21:** The `/stock/candle` endpoint availability on Finnhub free-tier is a known risk (STATE.md, R-02-A1 through R-02-A5). Researcher must verify free-tier candle endpoint behavior. If candles are paid-tier, factor design must fall back to `/quote`-only (daily `dp` + `v` for volume, no 52w range).

### No New Dependencies

- **D-22:** No new Python dependencies. All calls go through the existing `finnhub_client.py` rate-limited wrapper. Add new `_fetch_candles()` and `_fetch_metric()` functions to `finnhub_client.py` following the existing `_fetch_single_quote` / `_fetch_company_news_raw` patterns.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Requirements
- `.planning/REQUIREMENTS.md` — DISC-01..DISC-05 (5 requirements for this phase)
- `.planning/ROADMAP.md` — Phase 4 success criteria (4 items)
- `.planning/phases/01-foundation/01-CONTEXT.md` — Signal dataclass fields, compute_alert_id, DISCOVERY_PHASE config, sub_scores pattern

### Existing Code (integration points)
- `src/signal_system/data/universe.py` — `get_todays_universe()` → list of tickers for today
- `src/signal_system/data/finnhub_client.py` — existing rate-limit + retry wrapper; extend with `_fetch_candles()` and `_fetch_metric()`
- `src/signal_system/models.py` — frozen `Signal` dataclass; `compute_alert_id()`; `sub_scores: dict[str, float]`
- `src/signal_system/state/repository.py` — `insert_signal()`, `insert_run()`, `update_run()`; extend with `update_run_counts()`
- `src/signal_system/config.py` — `config.DISCOVERY_PHASE` (already wired, D-12)

### Key Risk
- STATE.md blocker: Finnhub free-tier endpoint availability for `/stock/candle` and `/stock/metric` is LOW confidence. Researcher must validate before planner designs scoring code. If candles are unavailable, the entire factor design must pivot to `/quote`-only.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Patterns
- `finnhub_client._fetch_single_quote` → template for `_fetch_candles()` and `_fetch_metric()`: `_acquire_slot()` → try/except `FinnhubAPIException` → check `PAID_TIER_STATUS_CODES` → return `None` on 403/404 → `raise` on 429 → retry via `_RETRY_DECORATOR`
- `news_classifier.py` → template for agent module structure: lazy `_get_client()` singleton, module-level constants for thresholds, single public function with typed signature
- `repository.insert_signal()` → accepts `Signal` + `routing_status` kwarg; all DB inserts use `INSERT OR IGNORE` on `alert_id`
- `repository.update_run()` → pattern to follow for `update_run_counts(run_id, tickers_scanned, tickers_signaled)`

### Established Patterns
- All timestamps use `ZoneInfo("America/New_York")` — date-prefix queries rely on this
- `PAID_TIER_STATUS_CODES = frozenset({403, 404})` is a public constant in `finnhub_client.py` — reference it directly
- Connection-per-operation: open → PRAGMA busy_timeout → execute → commit → close in try/finally

</code_context>

<specifics>
## Specific Guidance

- **Cross-sectional ranking edge case:** If only 1 valid ticker survives the candle fetch, all ranks = 0.5 (middle — no meaningful ranking with one data point). If 0 tickers survive, `score_universe()` returns `[]` immediately.
- **Volume surge denominator:** If the 20-day avg volume is 0 (very illiquid ticker), treat `volume_surge` factor as optional-missing (rank = 0.0). Do not divide by zero.
- **Phase A direct insert:** In Phase A, `insert_signal()` is called with `routing_status="MONITORING"` regardless of the Signal's `severity` value. The `severity` field on the DB row preserves the computed severity for calibration queries.
- **No weight_version column yet:** STACK.md mentions a `weight_version` stamp for IC interpretability. This is out of scope for Phase 4 — defer to Phase 6 or V2.
- **Signal.body should capture the weights used**, e.g. `"weights=35/30/25/10 momentum=0.87 volume=0.72 range=0.54 news=0.30"` — makes the Phase A rows self-documenting after weight changes.

</specifics>

<deferred>
## Deferred Ideas

- `weight_version` column on `signals` for IC interpretability after weight changes — defer to Phase 6 or V2
- Adaptive thresholds (auto-tune 60/80 from Phase A calibration data) — operator tunes manually at quarterly review; no code automation
- Caching candle data across tickers in a single batch request — Finnhub free-tier doesn't support batch candles; sequential is correct
- Sharing news data between news_classifier and discovery_agent — separate jobs, separate runs; no data sharing in Phase 4

</deferred>

---

*Phase: 4-Discovery Agent*
*Context gathered: 2026-05-16*
