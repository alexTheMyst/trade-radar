import csv
import pytest
from pathlib import Path
from signal_system.data.holdings import (
    Holding,
    EmptyHoldingsError,
    load_holdings,
    require_non_empty_holdings,
)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("ticker,shares,cost_basis,account,thesis_pillar\n")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_load_holdings_basic(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    _write_csv(csv_path, [
        {"ticker": "FCX", "shares": "40", "cost_basis": "38.10", "account": "schwab_main", "thesis_pillar": "commodities"},
        {"ticker": "NVDA", "shares": "10", "cost_basis": "850.00", "account": "roth_ira", "thesis_pillar": "ai_semi"},
    ])
    holdings = load_holdings(csv_path)
    assert len(holdings) == 2
    assert holdings[0] == Holding(ticker="FCX", shares=40.0, cost_basis=38.10, account="schwab_main", thesis_pillar="commodities")
    assert holdings[1] == Holding(ticker="NVDA", shares=10.0, cost_basis=850.0, account="roth_ira", thesis_pillar="ai_semi")


def test_load_holdings_multi_account_same_ticker(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    _write_csv(csv_path, [
        {"ticker": "FCX", "shares": "40", "cost_basis": "38.10", "account": "schwab_main", "thesis_pillar": ""},
        {"ticker": "FCX", "shares": "20", "cost_basis": "40.00", "account": "roth_ira", "thesis_pillar": ""},
    ])
    holdings = load_holdings(csv_path)
    assert len(holdings) == 2
    assert holdings[0].account == "schwab_main"
    assert holdings[1].account == "roth_ira"


def test_load_holdings_skips_comment_and_blank_rows(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text(
        "ticker,shares,cost_basis,account,thesis_pillar\n"
        "# this is a comment\n"
        "\n"
        "FCX,40,38.10,schwab_main,\n"
    )
    holdings = load_holdings(csv_path)
    assert len(holdings) == 1
    assert holdings[0].ticker == "FCX"


def test_load_holdings_optional_thesis_pillar_is_none_when_empty(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    _write_csv(csv_path, [
        {"ticker": "FCX", "shares": "40", "cost_basis": "38.10", "account": "schwab_main", "thesis_pillar": ""},
    ])
    holdings = load_holdings(csv_path)
    assert holdings[0].thesis_pillar is None


def test_load_holdings_ticker_uppercased(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    _write_csv(csv_path, [
        {"ticker": "fcx", "shares": "40", "cost_basis": "38.10", "account": "schwab_main", "thesis_pillar": ""},
    ])
    holdings = load_holdings(csv_path)
    assert holdings[0].ticker == "FCX"


def test_load_holdings_missing_file_raises_with_helpful_message(tmp_path):
    with pytest.raises(EmptyHoldingsError, match="not found"):
        load_holdings(tmp_path / "nonexistent.csv")


def test_load_holdings_empty_file_raises(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text("ticker,shares,cost_basis,account,thesis_pillar\n")
    with pytest.raises(EmptyHoldingsError, match="no usable rows"):
        load_holdings(csv_path)


def test_require_non_empty_holdings_is_alias(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    _write_csv(csv_path, [
        {"ticker": "FCX", "shares": "40", "cost_basis": "38.10", "account": "schwab_main", "thesis_pillar": ""},
    ])
    result = require_non_empty_holdings(csv_path)
    assert len(result) == 1
    assert result[0].ticker == "FCX"
