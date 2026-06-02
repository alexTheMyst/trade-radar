"""Holdings CSV loader.

holdings.csv is operator-maintained and gitignored (like universe.csv).
Load-path: Path(__file__).parent / "holdings.csv"
Raises EmptyHoldingsError — not a silent empty list — so a missing file
fails the advisor job loudly instead of producing a confusing empty digest.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

HOLDINGS_PATH = Path(__file__).parent / "holdings.csv"


class EmptyHoldingsError(RuntimeError):
    """Raised when holdings.csv is missing or has no usable rows."""


@dataclass(frozen=True, slots=True)
class Holding:
    ticker: str
    shares: float
    cost_basis: float
    account: str
    thesis_pillar: str | None = None


def _is_data_row(row: dict) -> bool:
    ticker = row.get("ticker", "") or ""
    return bool(ticker.strip()) and not ticker.strip().startswith("#")


def load_holdings(path: Path = HOLDINGS_PATH) -> list[Holding]:
    """Load holdings.csv and return a list of Holding objects.

    Raises:
        EmptyHoldingsError: if the file is missing or has no usable rows.
    """
    if not path.exists():
        raise EmptyHoldingsError(
            f"holdings.csv not found at {path}. "
            "Copy src/signal_system/data/holdings.csv.example to "
            "src/signal_system/data/holdings.csv and populate it."
        )

    holdings: list[Holding] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _is_data_row(row):
                continue
            holdings.append(
                Holding(
                    ticker=row["ticker"].strip().upper(),
                    shares=float(row["shares"]),
                    cost_basis=float(row["cost_basis"]),
                    account=row["account"].strip(),
                    thesis_pillar=row.get("thesis_pillar", "").strip() or None,
                )
            )

    if not holdings:
        raise EmptyHoldingsError(
            f"holdings.csv at {path} has no usable rows. "
            "Add at least one position row."
        )

    return holdings


def require_non_empty_holdings(path: Path = HOLDINGS_PATH) -> list[Holding]:
    """Alias for load_holdings — name mirrors universe.py's require_non_empty_universe."""
    return load_holdings(path)
