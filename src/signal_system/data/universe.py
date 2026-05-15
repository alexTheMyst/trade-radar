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


def get_todays_universe() -> list[str]:
    """Return the list of tickers to scan today.

    Includes:
    - All core holdings (core_holding=1) — always scanned every day.
    - Non-core tickers whose md5 bucket matches today's bucket.

    Excludes:
    - All K-1 ETFs (k1_etf=1) unconditionally — never passed to agents.

    Returns:
        List of uppercase ticker symbols in CSV input order.
    """
    todays_bucket = _today_bucket()
    tickers: list[str] = []

    with UNIVERSE_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["k1_etf"]):
                continue  # exclude K-1 ETFs at load time, unconditionally

            ticker = row["ticker"].strip().upper()
            is_core = bool(int(row["core_holding"]))
            in_partition = _md5_bucket(ticker) == todays_bucket

            if is_core or in_partition:
                tickers.append(ticker)

    return tickers
