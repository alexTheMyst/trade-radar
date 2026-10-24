"""held_ticker_set(): holdings.csv is the HELD source; core holdings only as a fallback."""

from __future__ import annotations

from signal_system.data import holdings, universe


def test_held_ticker_set_reads_holdings_csv(tmp_path, monkeypatch):
    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text(
        "ticker,shares,cost_basis,account\n"
        "can,10,5.0,IRA\n"
        "MU,2,100.0,Taxable\n"
        "MU,1,110.0,IRA\n"
    )
    monkeypatch.setattr(holdings, "HOLDINGS_PATH", csv_path)
    monkeypatch.setattr(universe, "get_core_holdings", lambda: ["SMH"])

    assert holdings.held_ticker_set(fallback_to_core=True) == {"CAN", "MU"}


def test_held_ticker_set_missing_file_falls_back_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(holdings, "HOLDINGS_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(universe, "get_core_holdings", lambda: ["SMH", "NVDA"])

    assert holdings.held_ticker_set(fallback_to_core=True) == {"SMH", "NVDA"}
    assert holdings.held_ticker_set(fallback_to_core=False) == set()
