# Plan 03 — Holdings ↔ Universe Reconciliation

**Type:** Add · **Priority:** P1 · **Est. effort:** ~1 day

## Problem

`news-morning` scans only **core holdings** (`get_core_holdings()` — tickers flagged
`core_holding=TRUE` in `universe.csv`). A live check on real data found:

- **35** positions in `holdings.csv`
- **16** core holdings in `universe.csv`
- **19** held tickers that are **not** core, and therefore never news-scanned:
  `AAPL, CAN, CDE, CMC, ESEA, ETH, FCX, JNUG, MARA, MU, ORCL, PANW, S, SCHB, SCHF, SGOV,
  SNFCA, T, TEVA`

So the News Classifier — whose entire job is "never miss a thesis-relevant event on a held
position" — is blind to **54% of the book**, even when running perfectly. This is a quiet
correctness failure that no test or job currently surfaces.

There is also a latent code bug in the drift helper's dependency: `load_holdings(path:
Path = HOLDINGS_PATH)` binds the path as a **default argument at import**, so
`monkeypatch.setattr(holdings, "HOLDINGS_PATH", ...)` cannot override it. Three
`test_holdings_loader.py` tests fail because of this, and any caller trying to redirect the
path (preflight, reconciliation dry-run) is affected. Fix it here since this plan is the
natural owner of `holdings.py`.

## Goals

1. **Make drift visible and impossible to ignore** — surface it in the daily digest and the
   watchdog, not just on demand.
2. **Make reconciliation one command** — promote held tickers into the universe as core (or
   explicitly exclude them) without hand-editing CSV.
3. **Fix the `HOLDINGS_PATH` default-arg bug** so path injection works (unblocks tests +
   preflight).

## Design

### 3a. Fix `load_holdings` path binding (prerequisite)

```python
# before
def load_holdings(path: Path = HOLDINGS_PATH) -> list[Holding]: ...

# after
def load_holdings(path: Path | None = None) -> list[Holding]:
    path = path or HOLDINGS_PATH
    ...
```

Apply the same change to `require_non_empty_holdings(path=...)`. Re-run
`tests/test_holdings_loader.py` — the 3 monkeypatch failures should clear. No behavior
change for real callers (default still resolves to the real file).

### 3b. Drift as a first-class, surfaced signal

`holdings_universe_drift()` already computes the set. Wire its output into places the
operator actually sees:

- **Daily digest (`news-morning`):** append a one-line footer when drift is non-empty:
  `COVERAGE GAP: 19 held tickers not scanned (run reconcile-universe). e.g. AAPL, FCX, MU…`
  This reuses the existing digest body; it does not create new alert-budget signals.
- **Watchdog (Plan 01):** add a non-fatal WARN line if drift count exceeds a threshold
  (e.g. > 0). Keeps it in the periodic health surface.
- **Preflight (Plan 02):** already lists drift under check [E].

### 3c. `reconcile-universe` operator command

A dry-run-by-default CLI mirroring the existing `build-universe` ergonomics
(`build-universe [--apply]`, which already round-trips operator columns via
`universe_builder.apply_merged_universe` / `diff_universe`).

Behavior:

```
uv run python -m signal_system reconcile-universe            # dry run: print a plan
uv run python -m signal_system reconcile-universe --apply    # write universe.csv
uv run python -m signal_system reconcile-universe --apply --exclude TEVA,SGOV,SCHB,SCHF
```

Logic:
1. Load holdings and current universe.
2. For each held ticker **not** already a universe row: propose adding it with
   `core_holding=TRUE` (so it is scanned), preserving/looking up `sector`/`industry` via
   `ticker_meta` where available (reuse `upsert_ticker_meta` / `get_ticker_meta`).
3. For each held ticker present but `core_holding` blank/FALSE: propose flipping to TRUE.
4. Honor `--exclude` for positions the operator does **not** want news-scanned (cash-like
   or index ETFs — e.g. `SGOV`, `SCHB`, `SCHF` money-market/broad-index sleeves). Excluded
   tickers are recorded with a `notes` marker so they don't reappear as drift each run.
5. Respect the **K-1 denylist** — never auto-add `USO/UNG/DBC/GSG` or flagged `k1_etf`
   rows to the scan set (backstopped by `universe.py` `K1_DENYLIST`).
6. Print a diff (added / promoted / excluded / unchanged); write only under `--apply`,
   going through `universe_builder.apply_merged_universe` so unknown operator columns
   round-trip (per commit `0ba1fd0`).

### Judgment the operator still owns

Not every held ticker *should* be news-scanned. Broad-market and cash sleeves (`SGOV`,
`SCHB`, `SCHF`) generate noise, not thesis signal. The command **proposes** promotion but
lets the operator `--exclude`; the dry-run default ensures nothing changes without review.
The point is to force an explicit decision on all 19, not to blindly scan them.

## Files touched

| File | Change |
|------|--------|
| `src/signal_system/data/holdings.py` | Fix default-arg binding in `load_holdings` / `require_non_empty_holdings`. |
| `src/signal_system/data/universe_builder.py` | Add `propose_holdings_reconciliation(holdings, universe, exclude) -> MergePlan` reusing existing merge/diff. |
| `src/signal_system/__main__.py` | Add `reconcile-universe` command + usage banner entry. |
| `src/signal_system/jobs/news_morning.py` | Append coverage-gap footer to digest when drift > 0. |
| `src/signal_system/jobs/common.py` | If digest rendering lives here, add the footer hook there instead. |
| `src/signal_system/jobs/watchdog.py` | (Plan 01) add drift WARN line. |
| `tests/test_holdings_loader.py` | The 3 failing tests should pass after 3a; add a path-injection test. |
| `tests/test_universe_builder.py` | Add reconciliation-plan cases (add / promote / exclude / K-1 skip). |
| `docs/plans/03-...` | This file. |

## Testing

- **3a regression:** the 3 currently-failing `holdings_universe_drift_*` tests pass; add
  `test_load_holdings_respects_injected_path`.
- `test_reconcile_proposes_missing_as_core` — held ticker absent from universe → proposed
  add with `core_holding=TRUE`.
- `test_reconcile_promotes_noncore_held` — present but non-core → propose promote.
- `test_reconcile_honors_exclude` — `--exclude SGOV` → SGOV not promoted, marked excluded,
  absent from next drift.
- `test_reconcile_skips_k1_denylist` — a K-1 ETF is never auto-added to the scan set.
- `test_reconcile_dry_run_writes_nothing` — without `--apply`, `universe.csv` byte-identical.
- `test_digest_shows_coverage_gap` — drift > 0 renders the footer; drift == 0 omits it.

## Rollout

1. Ship 3a (bug fix) + tests — safe, unblocks preflight and the failing suite.
2. Ship the drift footer — pure additive visibility.
3. Ship `reconcile-universe` (dry-run default).
4. Operator runs it once, decides the 19 (promote the real single-names — `AAPL, FCX, MU,
   ORCL, PANW, T, TEVA, MARA, ...`; exclude the cash/broad sleeves — `SGOV, SCHB, SCHF`),
   then `--apply`.
5. Confirm next `news-morning` scans the promoted set and the digest footer clears.

## Risks

- **Alert volume up** — scanning ~15 more names increases classify calls and possibly
  delivered alerts. Mitigated by the Alert Router's daily budget (1 AR / 3 INFO) which is
  unchanged; extra signal lands in MONITORING/SUPPRESSED, not the inbox.
- **Finnhub call budget** — more tickers per `news-morning` run; still sequential through
  the token bucket, wall-time cost only. Validate against 60/min.
- **Over-promotion noise** from index/cash sleeves — mitigated by `--exclude` + dry-run
  review.

## Acceptance criteria

- `test_holdings_loader.py` is fully green (0 failures).
- After operator reconciliation, `holdings_universe_drift()` returns only the explicitly
  excluded tickers, and the daily digest footer disappears for scanned names.
- `reconcile-universe` without `--apply` never mutates `universe.csv`.
