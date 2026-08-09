# GSD Debug Knowledge Base

Resolved debug sessions. Used by `gsd-debugger` to surface known-pattern hypotheses at the start of new investigations.

---

## build-universe-keyerror-sector — KeyError('sector') in universe_builder.add_row() for operator-only tickers
- **Date:** 2026-08-09
- **Error patterns:** KeyError, sector, industry, universe_builder, add_row, build_merged_universe, build-universe, bundle_row
- **Root cause:** `add_row()` in `src/signal_system/data/universe_builder.py` set `base = dict(bundle_row) if bundle_row else {}`, which only guaranteed `sector`/`industry` keys when `bundle_row` was a real bundle dict. For operator-only tickers (bundle_row=None, e.g. ETFs like SMH not in the S&P500/Nasdaq100 bundles), base started as `{}` without those keys, and later code read `base["sector"]`/`base["industry"]` via unsafe subscript instead of `.get()`, raising KeyError as soon as ticker_meta-cache or yfinance enrichment found any data for that ticker.
- **Fix:** Added `base["sector"] = base.get("sector", "")` and `base["industry"] = base.get("industry", "")` immediately after the bundle_row ternary, mirroring the existing `base["name"]` default pattern, so both keys are always present regardless of which add_row() path fires.
- **Files changed:** src/signal_system/data/universe_builder.py
---
