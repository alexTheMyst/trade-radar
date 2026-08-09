"""Tests for the wash-sale trade ledger.

insert_wash_sale_trade/get_recent_trades operate on the dormant wash_sale
table (created in Phase 1 with the account CHECK). The `side` column is a
migration added here; cost_basis is reused as the per-share trade price.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_wash_sale_round_trip(db):
    repository.insert_wash_sale_trade(
        ticker="AAPL",
        account="schwab_main",
        trade_date=date(2026, 6, 1),
        side="buy",
        quantity=100,
        price=180.5,
        notes="opening position",
    )
    trades = repository.get_recent_trades("AAPL", since=date(2026, 5, 1))
    assert len(trades) == 1
    row = trades[0]
    assert row["ticker"] == "AAPL"
    assert row["account"] == "schwab_main"
    assert row["side"] == "buy"
    assert row["quantity"] == 100
    assert row["price"] == pytest.approx(180.5)
    assert row["notes"] == "opening position"
    assert row["trade_date"] == "2026-06-01"


def test_wash_sale_cross_account_lookup(db):
    """get_recent_trades must NOT filter by account — wash sales cross accounts."""
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 1),
        side="sell", quantity=10, price=190.0,
    )
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="roth_ira", trade_date=date(2026, 6, 2),
        side="buy", quantity=10, price=192.0,
    )
    trades = repository.get_recent_trades("AAPL", since=date(2026, 5, 1))
    assert len(trades) == 2


def test_wash_sale_side_filter(db):
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 1),
        side="buy", quantity=10, price=190.0,
    )
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 2),
        side="sell", quantity=5, price=200.0,
    )
    buys = repository.get_recent_trades("AAPL", since=date(2026, 5, 1), side="buy")
    assert len(buys) == 1
    assert buys[0]["side"] == "buy"


def test_wash_sale_invalid_side_valueerror(db):
    with pytest.raises(ValueError, match="side"):
        repository.insert_wash_sale_trade(
            ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 1),
            side="hold", quantity=1, price=100.0,
        )


def test_wash_sale_invalid_account_valueerror(db):
    with pytest.raises(ValueError, match="account"):
        repository.insert_wash_sale_trade(
            ticker="AAPL", account="unknown", trade_date=date(2026, 6, 1),
            side="buy", quantity=1, price=100.0,
        )


def test_wash_sale_side_column_added_idempotently(db):
    """The side migration must be idempotent across init_db() calls."""
    repository.init_db()
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(wash_sale)").fetchall()}
    conn.close()
    assert "side" in cols


def test_wash_sale_since_filter(db):
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 1),
        side="buy", quantity=10, price=190.0,
    )
    repository.insert_wash_sale_trade(
        ticker="AAPL", account="schwab_main", trade_date=date(2026, 6, 10),
        side="buy", quantity=10, price=195.0,
    )
    trades = repository.get_recent_trades("AAPL", since=date(2026, 6, 5))
    assert len(trades) == 1
    assert trades[0]["trade_date"] == "2026-06-10"
