"""Tests for the universe builder (data/universe_builder.py).

Covers merge logic: operator columns preserved verbatim, new tickers get
defaults, denylist forced k1_etf=1, dedup, dry-run writes nothing, atomic
write on apply.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signal_system.data import universe_builder as ub
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="") as f:
        f.write("ticker,name,sector,industry\n")
        for row in rows:
            f.write(",".join(row) + "\n")


@pytest.fixture
def bundle(tmp_path):
    sp500 = tmp_path / "sp500.csv"
    nasdaq = tmp_path / "nasdaq100.csv"
    _write_csv(sp500, [["AAPL", "Apple", "Technology", "Consumer Electronics"]])
    _write_csv(nasdaq, [["MSFT", "Microsoft", "Technology", "Software"]])
    return {"sp500": sp500, "nasdaq100": nasdaq}


def _make_op_universe(tmp_path, rows: list[list[str]] | None = None) -> Path:
    path = tmp_path / "universe.csv"
    if rows is None:
        rows = []
    with path.open("w", newline="") as f:
        f.write(
            "ticker,core_holding,weight_pct,k1_etf,industry\n"
            if rows
            else "ticker,core_holding,weight_pct,k1_etf,industry\n"
        )
        for row in rows:
            f.write(",".join(row) + "\n")
    return path


def test_merge_preserves_operator_columns_and_adds_new(tmp_path, monkeypatch, db, bundle):
    op_path = _make_op_universe(
        tmp_path,
        [["AAPL", "1", "12.5", "0", ""]],
    )
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    rows = ub.build_merged_universe()

    by_ticker = {r["ticker"]: r for r in rows}
    assert len(rows) == 2
    # AAPL: operator columns verbatim
    assert by_ticker["AAPL"]["core_holding"] == "1"
    assert by_ticker["AAPL"]["weight_pct"] == "12.5"
    assert by_ticker["AAPL"]["k1_etf"] == "0"
    # New MSFT: defaults
    assert by_ticker["MSFT"]["core_holding"] == ""
    assert by_ticker["MSFT"]["weight_pct"] == ""
    assert by_ticker["MSFT"]["k1_etf"] == ""
    # Industry enrichment from bundle
    assert by_ticker["AAPL"]["industry"] == "Consumer Electronics"
    assert by_ticker["MSFT"]["industry"] == "Software"


def test_merge_forces_denylist_k1(tmp_path, monkeypatch, db, bundle):
    """Denylisted tickers get k1_etf=1 even when the operator flagged them 0."""
    op_path = _make_op_universe(
        tmp_path,
        [["USO", "1", "5.0", "0", "Oil"]],
    )
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    rows = ub.build_merged_universe()
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["USO"]["k1_etf"] == "1"


def test_merge_dedups_across_bundles_and_operator(tmp_path, monkeypatch, db, bundle):
    """A ticker in both sp500 and nasdaq100 appears once."""
    op_path = _make_op_universe(tmp_path, [])
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    rows = ub.build_merged_universe()
    tickers = [r["ticker"] for r in rows]
    assert len(tickers) == len(set(tickers))


def test_dry_run_writes_nothing(tmp_path, monkeypatch, db, bundle):
    op_path = _make_op_universe(tmp_path, [])
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    ub.apply_merged_universe(dry_run=True)

    assert not (tmp_path / "universe.csv.new").exists()
    # original untouched
    assert op_path.read_text().strip() == "ticker,core_holding,weight_pct,k1_etf,industry"


def test_apply_writes_atomically_with_backup(tmp_path, monkeypatch, db, bundle):
    op_path = _make_op_universe(
        tmp_path,
        [["AAPL", "1", "12.5", "0", ""]],
    )
    original = op_path.read_text()
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    ub.apply_merged_universe(dry_run=False)

    assert op_path.exists()
    content = op_path.read_text()
    assert "MSFT" in content
    assert "Consumer Electronics" in content
    # backup created
    assert (tmp_path / "universe.csv.bak").exists()
    assert (tmp_path / "universe.csv.bak").read_text() == original
    # no .new left behind
    assert not (tmp_path / "universe.csv.new").exists()


def test_comment_rows_dropped(tmp_path, monkeypatch, db, bundle):
    op_path = _make_op_universe(tmp_path, [])
    # Append a comment row directly
    with op_path.open("a") as f:
        f.write("# this is a comment\n")
    monkeypatch.setattr(ub, "UNIVERSE_PATH", op_path)
    monkeypatch.setattr(ub, "CONSTITUENT_BUNDLES", bundle)

    rows = ub.build_merged_universe()
    assert all(not r["ticker"].startswith("#") for r in rows)


def test_rows_to_csv_quotes_fields_with_commas(tmp_path, monkeypatch, db, bundle):
    """csv.writer quoting keeps comma-containing names parseable by csv.DictReader."""
    import csv as _csv
    from signal_system.data.universe_builder import _rows_to_csv

    rows = [{"ticker": "TPR", "name": "Tapestry, Inc.", "sector": "Consumer",
             "industry": "Luxury Goods", "core_holding": "1", "weight_pct": "5.0", "k1_etf": "0"}]
    content = _rows_to_csv(rows)
    parsed = list(_csv.DictReader(content.splitlines()))
    assert parsed[0]["ticker"] == "TPR"
    assert parsed[0]["name"] == "Tapestry, Inc."
    assert parsed[0]["core_holding"] == "1"
