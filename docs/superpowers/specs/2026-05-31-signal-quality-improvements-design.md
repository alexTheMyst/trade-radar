# Signal Quality Improvements — Design Spec

**Date:** 2026-05-31
**Scope:** Three independent improvements to signal generation quality, unified by Approach C (position weight as severity amplifier, not score modifier).

---

## 1. Discovery Agent — Multi-Day Momentum

### Problem

Single-day percent change (`dp`) is noise, not momentum. Cross-sectional ranking on 7-8 mega-caps per partition produces random ordering. News-count factor double-dips with the classifier and rewards crisis tickers.

### Solution

Replace the current scoring with multi-day momentum computed from `yfinance` historical daily candles (20-day lookback). Finnhub `/stock/candle` is confirmed 403 on free tier — Yahoo Finance provides free historical OHLCV with no API key and no warm-up period.

### New Factors

| Factor | Weight | Source | Meaning |
|--------|--------|--------|---------|
| `momentum_20d` | 50 | 20-day return from yfinance daily candles | Medium-term trend strength |
| `momentum_5d` | 30 | 5-day return from yfinance daily candles | Short-term acceleration |
| `range_vs_20d` | 20 | `(close - low_20d) / (high_20d - low_20d)` from yfinance | Position within 20-day range |

### Scoring Formula

```
composite = 100 * (0.50 * rank(return_20d) + 0.30 * rank(return_5d) + 0.20 * rank(range_position_20d))
```

### Removed

- `dp` (single-day percent change) — replaced by 5d/20d returns
- `news_activity` (article count) — removed entirely, no replacement
- `volume` factor — remains dropped (free tier unavailable)
- Phase A/B config toggle — Discovery goes straight to Phase B (routes through alert router)

### Edge Cases

- **Fewer than 20 candles returned:** If a ticker has fewer than 5 trading days of data, skip it entirely (can't compute momentum). If it has 5-19 days, compute momentum from available data (e.g., 12-day return instead of 20-day). The 5-day factor uses min(available, 5).
- **yfinance returns empty DataFrame:** Skip the ticker (same as current behavior when `/quote` fails). Log at DEBUG level.
- **All tickers skipped:** `score_universe()` returns empty list, run still marked success with 0 signals.
- **yfinance throttling:** Yahoo rarely throttles at 500 tickers/day. If it does, `yfinance` raises an exception — tenacity retry with backoff handles this.

### Data Source Split

| Purpose | Source | Rationale |
|---------|--------|-----------|
| Historical daily OHLCV (momentum) | `yfinance` | Finnhub `/stock/candle` is 403 on free tier |
| Real-time quote (price snapshot) | Finnhub `/quote` | Already used, confirmed free tier |
| Company news | Finnhub `/company-news` | Already used, confirmed free tier |

### API Budget

`yfinance` batch-downloads multiple tickers in one HTTP request (`yf.download(tickers, period="1mo")`). One batch call for the entire universe replaces ~500 individual Finnhub candle calls. Finnhub budget is unchanged (one `/quote` per ticker for price snapshot). Total wall time decreases significantly.

### Thresholds

- ACTION_REQUIRED: composite >= 80 (before position-weight adjustment)
- INFORMATIONAL: composite >= 60 (before position-weight adjustment)

### Changes to Files

- `src/signal_system/data/yahoo_client.py` — new module: `fetch_history(tickers, days=25)` returns `dict[str, DataFrame]` of daily OHLCV per ticker
- `src/signal_system/discovery/discovery_agent.py` — rewrite scoring logic, remove news/volume/dp factors, remove Phase A path, consume yahoo candle data
- `src/signal_system/jobs/discovery.py` — remove Phase A early-return branch
- `src/signal_system/config.py` — remove `DISCOVERY_PHASE` config
- `pyproject.toml` — add `yfinance` dependency

---

## 2. Enriched thesis.yaml Schema

### Problem

Two pillars with generic keywords produce low-precision classifications. The system prompt expects `positive_signals` / `negative_signals` that the YAML doesn't provide. No definition of what constitutes ACTION_REQUIRED confidence, so 0.85 threshold is unanchored.

### Solution

Expand the thesis YAML schema with explicit signal definitions, portfolio linkage, and threshold events.

### New Schema

```yaml
review_due: 2026-08-01  # ISO date; classifier refuses to run if past

pillars:
  - name: string                    # machine identifier (snake_case)
    description: string             # human-readable pillar description
    positive_signals: list[str]     # REQUIRED, at least 1
      - "concrete example of positive development"
    negative_signals: list[str]     # REQUIRED, at least 1
      - "concrete example of negative development"
    holdings_exposed: list[str]     # tickers affected by this pillar
    threshold_event: string | null  # defines ACTION_REQUIRED boundary
    keywords: list[str]             # DEPRECATED, optional, not sent to LLM
```

### Validation Rules (in `thesis_loader.py`)

- Each pillar must have at least one entry in `positive_signals` or `negative_signals`
- `holdings_exposed` defaults to `[]` if absent
- `threshold_event` defaults to `None` if absent
- `keywords` is accepted but ignored (backward compat)
- `review_due` must be a valid ISO date in the future (existing behavior)

### System Prompt Changes (in `news_classifier.py`)

- `_build_system_prompt()` renders `positive_signals` and `negative_signals` per pillar
- `threshold_event` is included as guidance: "Set confidence >= 0.85 only when the headline matches or approaches a threshold event for the relevant pillar"
- `holdings_exposed` is NOT sent to the LLM — used downstream for position-weight lookup only

### Changes to Files

- `src/signal_system/data/thesis_loader.py` — expand `Pillar` dataclass, add validation
- `src/signal_system/classifier/news_classifier.py` — update `_build_system_prompt()`
- `thesis.example.yaml` — rewrite with new schema as template

---

## 3. Position-Weight Severity Amplifier

### Problem

All tickers treated equally regardless of portfolio allocation. A headline about a 1% position and a 25% position get the same severity. The router's budget slots are consumed by whatever fires first, not what matters most to P&L.

### Solution

Add `weight_pct` to `universe.csv`. Apply a severity threshold adjustment based on position size relative to median. The raw score is never modified — only the severity classification changes.

### Approach: Threshold Shift (Approach C)

Position weight adjusts the ACTION_REQUIRED and INFORMATIONAL thresholds per signal:

```python
ratio = weight / median_weight
shift = 10.0 * log2(clamp(ratio, 0.25, 4.0))
adjusted_ar_threshold = base_ar_threshold - shift
adjusted_info_threshold = base_info_threshold - shift
```

### Behavioral Examples

| Ticker | weight_pct | Ratio to median (5%) | Shift | AR threshold | INFO threshold |
|--------|-----------|---------------------|-------|-------------|---------------|
| SPY | 25% | 5.0x (clamped to 4x) | +20 | 60 | 40 |
| QQQ | 20% | 4.0x | +20 | 60 | 40 |
| AAPL | 12% | 2.4x | +12.6 | 67 | 47 |
| NVDA | 4% | 0.8x | -3.2 | 83 | 63 |
| KO | 1% | 0.2x (clamped to 0.25) | -20 | 100 | 80 |

A Discovery score of 72 on SPY → ACTION_REQUIRED. Same score on KO → below INFORMATIONAL threshold (not emitted). Score in DB is unchanged.

### universe.csv Format

```csv
ticker,core_holding,k1_etf,weight_pct
SPY,1,0,25.0
QQQ,1,0,20.0
NVDA,0,0,4.0
KO,0,0,1.0
```

- `weight_pct` is required for all non-K1 tickers
- Tickers with `weight_pct=0` are watchlist-only (threshold shift = maximum penalty)

### Where Applied

- **Discovery Agent:** After computing composite score, before emitting Signal
- **News Classifier:** After receiving confidence from Claude. Uses the highest `weight_pct` among `holdings_exposed` for the matched pillar. If no `holdings_exposed` or no weight data, no shift applied (base thresholds used).

### What Does NOT Change

- Router daily budget (1 AR / 3 INFO) — unchanged
- `score` field in signals table — raw composite or confidence, never modified by weight
- IC measurement — score vs. outcome comparison remains uncontaminated
- Router slot competition — still uses `score` for ranking within severity tier

### Changes to Files

- `src/signal_system/data/universe.csv` — add `weight_pct` column
- `src/signal_system/data/universe.py` — add `get_position_weights()` function
- `src/signal_system/scoring/__init__.py` — new package
- `src/signal_system/scoring/weight_amplifier.py` — `adjusted_severity()` function
- `src/signal_system/discovery/discovery_agent.py` — call `adjusted_severity()` instead of hard threshold
- `src/signal_system/classifier/news_classifier.py` — call `adjusted_severity()` instead of `_severity_from_confidence()`
- `src/signal_system/jobs/news_morning.py` — pass position weights to classifier flow

---

## Cross-Cutting Concerns

### IC Measurement Preserved

Score is never modified by position weight. The `score` column in `signals` always contains the raw agent output. Quarterly review compares `score` vs. `outcome_price_30d`/`outcome_price_90d` without confounding.

### Router Interaction

The router sees signals *after* severity has been weight-adjusted. A high-weight ticker with moderate score can now arrive as ACTION_REQUIRED where it previously would have been INFORMATIONAL. The router's budget and slot-competition logic is unchanged.

### Backward Compatibility

- `thesis.yaml` files using the old `keywords`-only schema still load (keywords accepted, just not sent to LLM)
- `universe.csv` without `weight_pct` column: `get_position_weights()` returns empty dict, no shift applied
- Discovery Phase A config in `.env` is removed (breaking change, documented)

### New Dependency

- `yfinance` — added to `pyproject.toml` for historical daily OHLCV. Used only by `yahoo_client.py`. Does not replace Finnhub for real-time quotes or news.

### Testing Strategy

- Unit tests for `fetch_history()` with mocked yfinance responses
- Unit tests for momentum/range computation from candle data
- Unit tests for `adjusted_severity()` with various weight ratios
- Integration test: full Discovery pipeline with mocked candles → signals emitted with correct severity
- Integration test: News classifier with enriched thesis → position-weighted severity
- Existing tests updated to remove Phase A references
