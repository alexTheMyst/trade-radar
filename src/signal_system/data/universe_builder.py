"""Universe builder — merges committed index-constituent CSVs with universe.csv.

Operator rows in universe.csv (core_holding, weight_pct, k1_etf) are preserved
verbatim. New tickers from the constituent bundles get defaults. K-1 denylisted
tickers are forced k1_etf=1. Sector/industry enrichment priority:
bundle → ticker_meta table → yfinance (stragglers only, cached forever).

Writes are atomic: write universe.csv.new, then os.replace, keeping a .bak.
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

from signal_system.data.universe import K1_DENYLIST, UNIVERSE_PATH
from signal_system.state import repository

# Committed seed CSVs: ticker,name,sector,industry (no runtime scraping).
DATA_DIR = Path(__file__).parent
CONSTITUENT_BUNDLES: dict[str, Path] = {
    "sp500": DATA_DIR / "constituents" / "sp500.csv",
    "nasdaq100": DATA_DIR / "constituents" / "nasdaq100.csv",
}

_OPERATOR_COLUMNS: tuple[str, ...] = (
    "core_holding", "weight_pct", "k1_etf",
)

# Fixed output columns, always present and always first in this order.
_BASE_COLUMNS: tuple[str, ...] = (
    "ticker", "name", "sector", "industry", "core_holding", "weight_pct", "k1_etf",
)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return [
            {k.strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(f)
            if row.get("ticker") and not row["ticker"].strip().startswith("#")
        ]


def _read_operator_universe() -> dict[str, dict]:
    """Read universe.csv preserving operator columns. Returns {ticker: row}."""
    result: dict[str, dict] = {}
    for row in _read_csv(UNIVERSE_PATH):
        ticker = row["ticker"].strip().upper()
        result[ticker] = row
    return result


def _read_bundles() -> dict[str, dict]:
    """Read constituent CSVs into {ticker: {sector, industry, name}}."""
    merged: dict[str, dict] = {}
    for _name, path in CONSTITUENT_BUNDLES.items():
        for row in _read_csv(path):
            ticker = row.get("ticker", "").strip().upper()
            if not ticker:
                continue
            merged[ticker] = {
                "name": row.get("name", ""),
                "sector": row.get("sector", ""),
                "industry": row.get("industry", ""),
            }
    return merged


def _enrich_with_yfinance(ticker: str) -> tuple[str | None, str | None]:
    """One-off yfinance lookup for stragglers. Returns (sector, industry) or (None, None)."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector")
        industry = info.get("industry")
        if sector or industry:
            return sector, industry
    except Exception:
        pass
    return None, None


def build_merged_universe() -> list[dict]:
    """Merge operator universe + constituent bundles into a full row list.

    Returns list of row dicts with columns:
      ticker, name, sector, industry, core_holding, weight_pct, k1_etf

    Existing operator rows keep core_holding/weight_pct/k1_etf verbatim; new
    tickers get empty defaults. Denylisted tickers are forced k1_etf=1.
    """
    operator = _read_operator_universe()
    bundles = _read_bundles()

    merged: dict[str, dict] = {}

    def add_row(ticker: str, bundle_row: dict | None) -> None:
        op = operator.get(ticker)
        base = dict(bundle_row) if bundle_row else {}
        base["ticker"] = ticker
        base["name"] = base.get("name", "")
        base["sector"] = base.get("sector", "")
        base["industry"] = base.get("industry", "")
        for col in _OPERATOR_COLUMNS:
            base[col] = (op or {}).get(col, "")
        # Round-trip any other operator-authored columns (e.g. pillar,
        # scan_group, account, notes) verbatim — unknown to this module,
        # but not ours to discard.
        for col, val in (op or {}).items():
            if col and col not in base:
                base[col] = val

        if ticker in K1_DENYLIST:
            base["k1_etf"] = "1"

        # Industry/sector enrichment priority: bundle → ticker_meta → yfinance
        if not base.get("industry") or not base.get("sector"):
            cached = repository.get_ticker_meta(ticker)
            if cached and cached["source"] == "yfinance":
                base["industry"] = base["industry"] or (cached["industry"] or "")
                base["sector"] = base["sector"] or (cached["sector"] or "")
        if not base.get("industry") or not base.get("sector"):
            sector, industry = _enrich_with_yfinance(ticker)
            if sector or industry:
                repository.upsert_ticker_meta(
                    ticker=ticker, sector=sector, industry=industry, source="yfinance"
                )
                base["sector"] = base["sector"] or (sector or "")
                base["industry"] = base["industry"] or (industry or "")

        merged[ticker] = base

    for ticker in sorted(bundles):
        add_row(ticker, bundles[ticker])
    for ticker in sorted(operator):
        if ticker not in merged:
            add_row(ticker, None)

    return [merged[t] for t in sorted(merged)]


def _rows_to_csv(rows: list[dict]) -> str:
    extra_columns: list[str] = []
    for row in rows:
        for col in row:
            if col and col not in _BASE_COLUMNS and col not in extra_columns:
                extra_columns.append(col)
    columns = list(_BASE_COLUMNS) + extra_columns
    import io as _io
    buffer = _io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
    return buffer.getvalue()


def apply_merged_universe(*, dry_run: bool = True) -> int:
    """Build the merged universe and (unless dry-run) write it atomically.

    Returns the number of rows that would be/were written.
    """
    rows = build_merged_universe()
    content = _rows_to_csv(rows)

    if dry_run:
        return len(rows)

    # Atomic write: .new → os.replace, keep .bak
    new_path = UNIVERSE_PATH.with_suffix(UNIVERSE_PATH.suffix + ".new")
    new_path.write_text(content)
    if UNIVERSE_PATH.exists():
        bak_path = UNIVERSE_PATH.with_suffix(UNIVERSE_PATH.suffix + ".bak")
        bak_path.write_text(UNIVERSE_PATH.read_text())
    os.replace(new_path, UNIVERSE_PATH)
    return len(rows)


def diff_universe() -> list[str]:
    """Return human-readable lines describing what a build would change."""
    operator = _read_operator_universe()
    merged = build_merged_universe()
    lines: list[str] = []
    for row in merged:
        ticker = row["ticker"]
        if ticker not in operator:
            lines.append(f"+ {ticker} (new) industry={row['industry'] or '?'}")
        else:
            op = operator[ticker]
            changed = [
                f"{col}={op.get(col, '')}->{row.get(col, '')}"
                for col in _OPERATOR_COLUMNS
                if (op.get(col) or "") != (row.get(col) or "")
            ]
            if changed:
                lines.append(f"~ {ticker} {' '.join(changed)}")
    return lines
