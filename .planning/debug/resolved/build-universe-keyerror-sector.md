---
status: resolved
trigger: "build-universe crashes with KeyError: 'sector' in src/signal_system/data/universe_builder.py, reproduced via `uv run python -m signal_system build-universe` (dry-run) against real universe.csv and constituent bundle data"
created: 2026-08-09T08:20:00-04:00
updated: 2026-08-09T08:52:00-04:00
---

## Current Focus
<!-- OVERWRITE on each update - reflects NOW -->

reasoning_checkpoint:
  hypothesis: "In add_row() (src/signal_system/data/universe_builder.py:98-124), `base = dict(bundle_row) if bundle_row else {}` (line 100) only guarantees 'sector'/'industry' keys when bundle_row is a real bundle dict. When bundle_row is None (operator-only tickers, e.g. SMH), base starts as {} and only ticker/name/_OPERATOR_COLUMNS get set (lines 101-104). The unsafe subscript reads at lines 113-114 (ticker_meta cache hit) and 121-122 (yfinance enrichment) then raise KeyError('sector') as soon as that branch executes for such a ticker."
  confirming_evidence:
    - "Full read of universe_builder.py (lines 1-186) confirms lines 110/115 use base.get() (safe) but 113/114/121/122 use base[\"sector\"]/base[\"industry\"] subscript (unsafe), and only the bundle_row=None path (line 100 ternary) skips initializing those keys."
    - "Traceback in Symptoms.errors points exactly to line 121 (`base[\"sector\"] = base[\"sector\"] or (sector or \"\")`) reached via build_merged_universe() line 130 `add_row(ticker, None)` — the operator-only loop."
    - "Confirmed real universe.csv contains operator tickers (e.g. SMH) structurally absent from both sp500.csv/nasdaq100.csv bundles, so bundle_row=None fires routinely, not as a rare edge case."
  falsification_test: "If base dict already contained 'sector'/'industry' keys (e.g. via dict.setdefault or explicit init) and the crash still occurred, hypothesis would be wrong. No such initialization exists anywhere in add_row for the None-bundle_row path — confirmed by full file read."
  fix_rationale: "Initialize base['sector'] and base['industry'] to '' immediately after line 100 (same pattern already used for base['name'] at line 102), restoring the invariant that later unsafe subscript reads assume. This fixes the root cause (missing keys) rather than symptom-patching individual subscript sites, and matches the existing code's own pattern (name already defaults this way)."
  blind_spots: "Have not yet re-run the full test suite or a live dry-run against real data post-fix. Have not checked whether any other caller depends on 'sector'/'industry' being absent (unlikely, but diff_universe() line 175 uses row['industry'] subscript downstream — will verify this becomes safe post-fix since sector/industry will now always be present)."

hypothesis: In build_merged_universe()/add_row() (src/signal_system/data/universe_builder.py), when bundle_row is None (an operator ticker not present in the S&P500/Nasdaq100 constituent bundles), `base = dict(bundle_row) if bundle_row else {}` never initializes "sector"/"industry" keys, and lines 113-114/121-122 read `base["sector"]`/`base["industry"]` via subscript (not .get), raising KeyError once ticker_meta cache or yfinance enrichment finds any data for that ticker.
test: Re-run `uv run python -m signal_system build-universe` (dry-run) against the repo's real universe.csv (38 operator tickers, several are ETFs like SMH not in either index) and constituent bundles; confirm KeyError('sector') traceback points to line 121 inside add_row via the operator-only loop (`for ticker in sorted(operator): if ticker not in merged: add_row(ticker, None)`).
expecting: Confirms the operator-only branch (bundle_row=None) is the trigger, and that initializing sector/industry defaults (e.g. base.setdefault) or switching the unsafe subscript reads to .get() eliminates the crash without changing behavior for the bundle_row-present path.
next_action: Fix applied and self-verified (clean dry-run + pytest with no new failures). Awaiting human confirmation that `--apply` run against real universe.csv produces the expected merged CSV before archiving this session.

## Symptoms
<!-- Written during gathering, then IMMUTABLE -->

expected: `uv run python -m signal_system build-universe` (dry-run, no --apply) prints a human-readable diff of proposed universe.csv changes without writing anything, merging the committed S&P500/Nasdaq100 constituent bundles with the operator's existing universe.csv rows.
actual: Command crashes with an unhandled KeyError before printing any diff output.
errors: |
  Traceback (most recent call last):
    File "<frozen runpy>", line 203, in _run_module_as_main
    File "<frozen runpy>", line 88, in _run_code
    File "C:\Users\alexe\trade-radar\src\signal_system\__main__.py", line 364, in <module>
      _cmd_build_universe(sys.argv[2:])
    File "C:\Users\alexe\trade-radar\src\signal_system\__main__.py", line 116, in _cmd_build_universe
      lines = diff_universe()
    File "C:\Users\alexe\trade-radar\src\signal_system\data\universe_builder.py", line 170, in diff_universe
      merged = build_merged_universe()
    File "C:\Users\alexe\trade-radar\src\signal_system\data\universe_builder.py", line 130, in build_merged_universe
      add_row(ticker, None)
    File "C:\Users\alexe\trade-radar\src\signal_system\data\universe_builder.py", line 121, in add_row
      base["sector"] = base["sector"] or (sector or "")
  KeyError: 'sector'
started: First run since PR #8 ("feat: profit loop + new-idea discovery", merged 2026-08-08) introduced build-universe / universe_builder.py — the feature has never successfully completed against this repo's real operator data (zero prior successful runs).
reproduction: |
  From repo root: `uv run python -m signal_system build-universe`
  Real data involved: src/signal_system/data/universe.csv (38 operator rows, includes ETF tickers like SMH that are not members of the S&P500 or Nasdaq100 indices) and src/signal_system/data/constituents/{sp500,nasdaq100}.csv (committed index bundles).
  Deterministic: crashes on the first operator ticker not present in either constituent bundle whose sector/industry enrichment (ticker_meta cache hit, or a successful yfinance lookup) returns any non-empty value.

## Eliminated
<!-- APPEND only - prevents re-investigating -->

## Evidence
<!-- APPEND only - facts discovered -->

- timestamp: 2026-08-09T08:20:00-04:00
  checked: Full read of src/signal_system/data/universe_builder.py (build_merged_universe, add_row, diff_universe, apply_merged_universe).
  found: "add_row(ticker, bundle_row) sets `base = dict(bundle_row) if bundle_row else {}`. When bundle_row is a real bundle dict (first loop, `for ticker in sorted(bundles): add_row(ticker, bundles[ticker])`), base always has \"sector\"/\"industry\" keys because _read_bundles() always includes them (possibly as empty string). When bundle_row is None (second loop, `for ticker in sorted(operator): if ticker not in merged: add_row(ticker, None)`), base starts as `{}` and only gets \"ticker\", \"name\", and the three _OPERATOR_COLUMNS (\"core_holding\", \"weight_pct\", \"k1_etf\") set — never \"sector\"/\"industry\"."
  implication: Any operator-only ticker (present in universe.csv but absent from both index bundles) reaches lines 110 / 113-114 / 115 / 121-122 without \"sector\"/\"industry\" keys present. Lines 110 and 115 use base.get() (safe, returns None). Lines 113, 114, 121, 122 use base["sector"]/base["industry"] (subscript, unsafe) — these raise KeyError as soon as the enrichment branch actually executes (ticker_meta cache hit at 113-114, or a successful yfinance lookup at 121-122).

- timestamp: 2026-08-09T08:20:00-04:00
  checked: "Confirmed operator universe.csv content via `_read_operator_universe()` (38 rows: NVDA, AVGO, PLTR, SMH, CRWV, ...) and bundle content via `_read_bundles()` (518 rows across sp500.csv + nasdaq100.csv)."
  found: Several real operator tickers (e.g. SMH, an ETF) are structurally guaranteed to be absent from the S&P500/Nasdaq100 single-stock constituent bundles, so the `bundle_row=None` / `add_row(ticker, None)` path is not a rare edge case for this operator's actual portfolio — it fires routinely.
  implication: This will reproduce on every build-universe run against this operator's real holdings, deterministically, on the first such ticker for which enrichment finds any sector/industry data (yfinance is live and returns real sector data for SMH, PLTR-style names, etc., so the crash is not intermittent in practice).

## Resolution
<!-- OVERWRITE as understanding evolves -->

root_cause: "add_row() in src/signal_system/data/universe_builder.py sets `base = dict(bundle_row) if bundle_row else {}` (line 100), which only guarantees 'sector'/'industry' keys when bundle_row is a real bundle dict. For operator-only tickers (bundle_row=None, e.g. SMH — not in either index bundle), base starts as {} without those keys. The ticker_meta cache-hit branch (lines 113-114) and yfinance-enrichment branch (lines 121-122) read base['sector']/base['industry'] via unsafe subscript instead of .get(), raising KeyError('sector') as soon as either branch executes for such a ticker — which happens routinely against real operator data."
fix: "Added `base[\"sector\"] = base.get(\"sector\", \"\")` and `base[\"industry\"] = base.get(\"industry\", \"\")` immediately after the bundle_row ternary (universe_builder.py, right after line 100), matching the existing `base[\"name\"]` default pattern. Guarantees both keys are always present regardless of which add_row() call path (bundle-sourced or operator-only) is taken, so the later unsafe subscript reads (lines 113-114, 121-122, and diff_universe()'s row['industry'] at line 175) are always safe."
verification: "1) `uv run python -m signal_system build-universe` (dry-run) against real universe.csv + constituent bundles now completes cleanly: prints full diff (538 rows, e.g. SMH-style operator-only tickers included) ending 'Dry-run: 538 rows would be written. Re-run with --apply to commit.' — no KeyError. 2) `uv run pytest -q`: 466 passed, 3 failed (tests/test_holdings_loader.py::test_holdings_universe_drift_{default_loads,malformed_csv,short_row}_never_raises). Confirmed via `git stash` that these 3 failures pre-exist on main before this fix (same failures, same assertion) — unrelated to universe_builder.py, not a regression introduced by this change. 3) Human-verify checkpoint: orchestrator ran `uv run python -m signal_system build-universe --apply` for real against this operator's live universe.csv. No crash; 538 rows written; automatic .bak created. KeyError('sector') fix confirmed correct end-to-end."
files_changed:
  - src/signal_system/data/universe_builder.py

## Follow-up Finding (out of scope for this session's root cause, recorded for visibility)

While verifying the real --apply run, discovered that `apply_merged_universe()` / `_rows_to_csv()` only emits a fixed column set (`ticker,name,sector,industry,core_holding,weight_pct,k1_etf`) and silently drops any other columns present in the operator's original universe.csv. This operator's real file also carries `pillar`, `scan_group`, and `account`/`notes` columns (56 rows including comments) that were NOT in `_OPERATOR_COLUMNS` and got discarded entirely by the real --apply run. No code path currently reads those three columns (`universe.py`'s rotation is a pure md5 hash of the ticker, not a stored `scan_group` column; no discovery/advisor module references `pillar` or `account` from universe.csv), so this did not break any running job — but it is real operator-authored data loss with no warning. The orchestrator restored universe.csv from the auto-generated `.bak` immediately after discovering this, so the live file is unaffected. This is a separate, distinct gap in `build-universe` (not the KeyError root-caused here) and is being surfaced to the user directly as its own decision — whether to extend `_OPERATOR_COLUMNS`/`_rows_to_csv()` to round-trip unknown columns before running --apply for real again.
