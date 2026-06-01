"""Ticker universe loader with deterministic md5 rotation partitioning.

The universe lives in universe.csv (operator-maintained, sibling to this file).
K-1 ETFs are excluded unconditionally at load time — never passed to agents.
Core holdings appear every day regardless of partition.

Partitioning uses hashlib.md5 (NOT Python's built-in hash(), which is salted
per-process and would produce different partitions on every restart — T-01-05).
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UNIVERSE_PATH = Path(__file__).parent / "universe.csv"


class EmptyUniverseError(RuntimeError):
    """Raised when the ticker universe is empty or universe.csv is missing.

    Core holdings are scanned unconditionally, so a populated universe.csv
    always yields at least them. An empty result therefore means the file is
    missing or has no usable rows — an operator-data outage, never a normal
    scan day. Surfacing it loudly (heartbeat /fail) is required by the
    project's no-silent-failure principle: a green "Scanned 0 tickers, 0
    alerts" digest would hide a real outage (the exact failure that occurred
    when universe.csv went missing from the production checkout).
    """


def require_non_empty_universe(tickers: list[str], *, job: str) -> list[str]:
    """Return *tickers* unchanged, raising EmptyUniverseError when it is empty.

    Call this in scheduled jobs right after loading the universe so a missing
    or empty universe.csv fails the run loudly instead of delivering a
    misleading empty digest.
    """
    if tickers:
        return tickers
    reason = (
        f"universe.csv is missing at {UNIVERSE_PATH}"
        if not UNIVERSE_PATH.exists()
        else f"universe.csv at {UNIVERSE_PATH} has no usable tickers"
    )
    raise EmptyUniverseError(
        f"{job} aborted: empty ticker universe ({reason}). "
        "Refusing to deliver a misleading 'Scanned 0 tickers' digest — "
        "restore universe.csv and re-run."
    )


def _md5_bucket(ticker: str) -> int:
    """Return deterministic 0/1/2 partition for *ticker*.

    Uses hashlib.md5 — stable across processes, days, Python versions, and OSes.
    Built-in hash() is explicitly NOT used (process-salted, non-deterministic — T-01-05).
    """
    return int(hashlib.md5(ticker.encode("utf-8")).hexdigest(), 16) % 3


def _today_bucket() -> int:
    """Return today's partition (0/1/2) based on ET day-of-year mod 3.

    Rotates daily: each partition is scanned roughly every 3 days.
    DST transitions do not affect the date, only the clock — safe.
    """
    return datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday % 3


def _is_truthy(value: str | None) -> bool:
    """Parse boolean columns that may be '1'/'0', 'true'/'false', or absent."""
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes")


def _is_data_row(row: dict) -> bool:
    """Return False for blank or comment rows emitted by DictReader from # lines."""
    ticker = row.get("ticker", "") or ""
    return bool(ticker.strip()) and not ticker.strip().startswith("#")


def get_core_holdings() -> list[str]:
    """Return core-holding tickers only, preserving CSV order and K-1 filtering."""
    if not UNIVERSE_PATH.exists():
        return []
    tickers: list[str] = []

    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _is_data_row(row):
                continue
            if _is_truthy(row.get("k1_etf")):
                continue
            if not _is_truthy(row.get("core_holding")):
                continue
            tickers.append(row["ticker"].strip().upper())

    return tickers


def get_todays_universe() -> list[str]:
    """Return the list of tickers to scan today.

    Includes:
    - All core holdings (core_holding=true/1) — always scanned every day.
    - Non-core tickers whose md5 bucket matches today's bucket.

    Excludes:
    - All K-1 ETFs (k1_etf=true/1) unconditionally — never passed to agents.
    - Comment rows and blank rows from the CSV.

    Returns:
        List of uppercase ticker symbols in CSV input order.
    """
    if not UNIVERSE_PATH.exists():
        return []
    todays_bucket = _today_bucket()
    tickers: list[str] = []

    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _is_data_row(row):
                continue
            if _is_truthy(row.get("k1_etf")):
                continue  # exclude K-1 ETFs at load time, unconditionally

            ticker = row["ticker"].strip().upper()
            is_core = _is_truthy(row.get("core_holding"))
            in_partition = _md5_bucket(ticker) == todays_bucket

            if is_core or in_partition:
                tickers.append(ticker)

    return tickers


def get_position_weights() -> dict[str, float]:
    """Return {ticker: weight_pct} for all non-K1 tickers in the universe.

    Tickers with missing or empty weight_pct get 0.0.
    K-1 ETFs are excluded (they never reach any agent).
    Returns {} when universe.csv is absent (operator hasn't populated it yet);
    callers fall back to confidence-only severity in that case.
    """
    if not UNIVERSE_PATH.exists():
        return {}
    weights: dict[str, float] = {}

    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _is_data_row(row):
                continue
            if _is_truthy(row.get("k1_etf")):
                continue
            ticker = row["ticker"].strip().upper()
            raw_weight = row.get("weight_pct", "").strip()
            try:
                weights[ticker] = float(raw_weight) if raw_weight else 0.0
            except ValueError:
                weights[ticker] = 0.0

    return weights
