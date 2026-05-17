from __future__ import annotations

import csv
import sqlite3
from datetime import date

import pytest

from signal_system.data import universe
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_core_holdings_filters_to_core_only_and_preserves_csv_order(tmp_path, monkeypatch):
    csv_path = tmp_path / "universe.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "core_holding", "k1_etf"])
        writer.writeheader()
        writer.writerow({"ticker": " spy ", "core_holding": "1", "k1_etf": "0"})
        writer.writerow({"ticker": "QQQ", "core_holding": "0", "k1_etf": "0"})
        writer.writerow({"ticker": "k1x", "core_holding": "1", "k1_etf": "1"})
        writer.writerow({"ticker": "AAPL", "core_holding": "1", "k1_etf": "0"})

    monkeypatch.setattr(universe, "UNIVERSE_PATH", csv_path)

    assert universe.get_core_holdings() == ["SPY", "AAPL"]


def test_latest_successful_run_date_returns_newest_success_only(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        """
        INSERT INTO runs (run_id, job, started_at, ended_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("run-1", "daily-close", "2026-05-14T16:00:00-04:00", "2026-05-14T16:01:00-04:00", "success"),
            ("run-2", "daily-close", "2026-05-15T16:00:00-04:00", "2026-05-15T16:01:00-04:00", "failed"),
            ("run-3", "daily-close", "2026-05-16T16:00:00-04:00", "2026-05-16T16:01:00-04:00", "success"),
            ("run-4", "daily-close", "2026-05-17T16:00:00-04:00", None, "running"),
            ("run-5", "news-morning", "2026-05-18T08:00:00-04:00", "2026-05-18T08:01:00-04:00", "success"),
        ],
    )
    conn.commit()
    conn.close()

    assert repository.get_latest_successful_run_date("daily-close") == date(2026, 5, 16)


def test_latest_successful_run_date_returns_none_when_absent(db):
    assert repository.get_latest_successful_run_date("daily-close") is None
