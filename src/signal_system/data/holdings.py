"""Holdings CSV loader.

holdings.csv is operator-maintained and gitignored (like universe.csv).
Load-path: Path(__file__).parent / "holdings.csv"
Raises EmptyHoldingsError — not a silent empty list — so a missing file
fails the sentinel job loudly instead of producing a confusing empty digest.
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


def load_holdings(path: Path | None = None) -> list[Holding]:
    """Load holdings.csv and return a list of Holding objects.

    *path* defaults to HOLDINGS_PATH and is resolved at call time (not bound
    at import) so callers and tests can inject an alternate probe path.

    Raises:
        EmptyHoldingsError: if the file is missing or has no usable rows.
    """
    path = path or HOLDINGS_PATH
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


def require_non_empty_holdings(path: Path | None = None) -> list[Holding]:
    """Alias for load_holdings — name mirrors universe.py's require_non_empty_universe."""
    return load_holdings(path)


def held_ticker_set(*, fallback_to_core: bool = False) -> set[str]:
    """Return operator-held tickers (holdings.csv) as a set.

    Fall back to the universe core-holding set when holdings.csv is missing,
    empty, or malformed and *fallback_to_core* is set. The core set is the old
    "held" approximation, so this preserves historical HELD tags when the
    operator has no holdings.csv, while the normal path uses actual positions.
    """
    try:
        return {holding.ticker for holding in load_holdings()}
    except (EmptyHoldingsError, ValueError, KeyError, TypeError):
        if fallback_to_core:
            from signal_system.data.universe import get_core_holdings

            return set(get_core_holdings())
        return set()


def holdings_universe_drift(
    holdings: list[Holding] | None = None,
    core_holdings: set[str] | None = None,
) -> list[str]:
    """Return held tickers missing from the discovery core-holding set.

    A position that is not a core_holding in universe.csv is invisible to the
    news-morning scan (which only scans core holdings). Surfacing this drift
    keeps the operator's thesis-relevant coverage honest.

    Args:
        holdings: Holding objects (default: load from HOLDINGS_PATH, empty on
            EmptyHoldingsError so the helper never raises).
        core_holdings: The universe core-holding set (default: universe.csv lookup).

    Returns:
        Sorted list of held tickers lacking core_holding=true.
    """
    if holdings is None:
        try:
            holdings = load_holdings()
        except (EmptyHoldingsError, ValueError, KeyError, TypeError, AttributeError):
            # Missing, empty, or malformed holdings.csv (bad floats, short rows
            # with None fields) must never take down the weekly-synthesis job
            # after the paid LLM call — best-effort drift.
            holdings = []
    if core_holdings is None:
        from signal_system.data.universe import get_core_holdings

        core_holdings = set(get_core_holdings())

    held_tickers = sorted({h.ticker for h in holdings})
    return [t for t in held_tickers if t not in core_holdings]
