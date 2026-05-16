# Phase 4: Discovery Agent — Discussion Log

**Date:** 2026-05-16
**Mode:** Interactive (gsd-discuss-phase)

## Gray Areas Discussed

### Area 1: Scoring Factor Design

**Q1a: What are the 4 factors?**
User confirmed: price_momentum (35%), volume_surge (30%), range_position (25%), news_activity (10%). Multi-day candle data OK.

**Q1b: Data sources and scoring approach?**
- price_momentum → 20-day return from `/stock/candle`
- volume_surge → today's volume ÷ 20-day avg from same candle response
- range_position → `/stock/metric` basicFinancials (52WeekHigh/Low)
- news_activity → `/company-news` last 7 days (existing client)
- Scoring: cross-sectional rank normalization (not fixed thresholds)

### Area 2: Score-to-Severity Thresholds

- ACTION_REQUIRED: composite score ≥ 80
- INFORMATIONAL: composite score ≥ 60 and < 80
- Below 60: no Signal emitted (silently dropped)
- Sub-threshold tickers are NOT emitted as MONITORING

### Area 3: Score-Floor Guard Scope

- Required factor: candle history only. Ticker dropped if candles fail or < 20 bars.
- Optional: `/stock/metric` (range_position gets 0 rank if unavailable)
- Optional: `/company-news` (news_activity gets 0 rank if unavailable)

### Area 4: Phase A Behavior

- Threshold-respecting Phase A (only log tickers scoring ≥ 60)
- Router bypass: direct `insert_signal()` with routing_status=MONITORING in Phase A
- Signal.severity still computed normally (ACTION_REQUIRED/INFORMATIONAL) — stored for calibration queries
- Phase B: return Signals to caller, router handles routing_status

### Area 5: Scan Audit Trail

- Option A: Count columns only
- Add `tickers_scanned` and `tickers_signaled` columns to `runs` table via `_ensure_column()`
- New `repository.update_run_counts(run_id, tickers_scanned, tickers_signaled)` function

