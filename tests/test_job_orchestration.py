from __future__ import annotations

import csv
from contextlib import contextmanager
import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from signal_system.data import universe
from signal_system.models import Signal, compute_alert_id
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


def _sig(
    *,
    ticker: str,
    severity: str = "INFORMATIONAL",
    agent: str = "news_classifier",
    score: float = 0.75,
) -> Signal:
    timestamp = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc)
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent=agent,
        timestamp=timestamp,
        alert_id=compute_alert_id(ticker, "2026-05-19", f"rule-{ticker}", agent),
        title=f"{ticker}: important update",
        body=f"{ticker} detailed body",
    )


def _news_item(headline: str, dt: datetime) -> dict:
    return {
        "headline": headline,
        "datetime": int(dt.timestamp()),
        "source": "Reuters",
    }


@contextmanager
def _noop_heartbeat():
    yield


def test_routed_persistence_stores_every_tuple_with_demotions():
    from signal_system.jobs.common import persist_routed_signals

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    suppressed = _sig(ticker="MSFT", severity="INFORMATIONAL", score=0.71)

    with patch("signal_system.state.repository.insert_signal") as mock_insert:
        summary = persist_routed_signals(
            [
                (delivered, "DELIVERED", None),
                (suppressed, "SUPPRESSED", "outscored"),
            ]
        )

    assert summary.delivered_signals == [delivered]
    assert summary.status_counts == {"DELIVERED": 1, "SUPPRESSED": 1, "MONITORING": 0}
    mock_insert.assert_has_calls(
        [
            call(delivered, routing_status="DELIVERED", demoted_from=None),
            call(suppressed, routing_status="SUPPRESSED", demoted_from="outscored"),
        ]
    )


def test_shared_digest_details_only_delivered_and_counts_non_delivered():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        delivered_signals=[delivered],
        status_counts={"DELIVERED": 1, "SUPPRESSED": 2, "MONITORING": 1},
    )

    assert "News Morning" in payload.subject
    assert "Scanned 3 tickers, 1 alert" in payload.body
    assert "AAPL: important update" in payload.body
    assert "AAPL detailed body" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 1" in payload.body


def test_shared_digest_zero_alert_confirmation():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=4,
        delivered_signals=[],
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 3},
    )

    assert "Scanned 4 tickers, 0 alerts" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 3" in payload.body


def test_news_morning_requires_previous_daily_close_before_fetch_or_email(db):
    from signal_system.jobs import news_morning

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=None), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", side_effect=AssertionError("should not fetch")), \
         patch("signal_system.jobs.news_morning.email_sender.send_email") as mock_send:
        with pytest.raises(RuntimeError, match="daily-close"):
            news_morning.run()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'news-morning'").fetchone()
    conn.close()
    assert status == ("failed",)
    mock_send.assert_not_called()


def test_news_morning_thesis_failure_aborts_before_classification_or_digest(db):
    from signal_system.jobs import news_morning

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", side_effect=FileNotFoundError("missing thesis")) as mock_load, \
         patch("signal_system.jobs.news_morning.fetch_company_news") as mock_fetch, \
         patch("signal_system.jobs.news_morning.classify_headlines") as mock_classify, \
         patch("signal_system.jobs.news_morning.email_sender.send_email") as mock_send:
        with pytest.raises(FileNotFoundError, match="missing thesis"):
            news_morning.run()

    assert mock_load.call_count == 1
    mock_fetch.assert_not_called()
    mock_classify.assert_not_called()
    mock_send.assert_not_called()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'news-morning'").fetchone()
    conn.close()
    assert status == ("failed",)


def test_news_morning_core_holdings_only_and_zero_alert_digest(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc).astimezone()
    fetch_calls: list[tuple[date, date]] = []

    def fetch_side_effect(ticker: str, from_date: date, to_date: date):
        fetch_calls.append((from_date, to_date))
        return []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL", "MSFT"]), \
         patch("signal_system.jobs.news_morning.get_todays_universe", side_effect=AssertionError("wrong universe helper")), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", side_effect=fetch_side_effect) as mock_fetch, \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[]), \
         patch("signal_system.jobs.news_morning.email_sender.send_email") as mock_send:
        news_morning.run()

    assert mock_fetch.call_args_list[0].args[0] == "AAPL"
    assert mock_fetch.call_args_list[1].args[0] == "MSFT"
    assert fetch_calls == [(date(2026, 5, 16), fixed_now.date()), (date(2026, 5, 16), fixed_now.date())]
    assert mock_send.call_count == 1
    assert "Scanned 2 tickers, 0 alerts" in mock_send.call_args.kwargs["body"]

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'news-morning'").fetchone()
    conn.close()
    assert status == ("success",)


def test_news_morning_headline_cap_dedups_before_cap_and_persists_overflow(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc).astimezone()
    previous_close = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    captured_headlines: list[dict] = []
    items = [
        _news_item(f"Headline {index:02d}", previous_close.replace(minute=index % 60) if index == 0 else previous_close)
        for index in range(53)
    ]
    items = []
    for index in range(53):
        items.append(_news_item(f"Headline {index:02d}", previous_close.replace(hour=20 + (index // 60), minute=index % 60)))
    items.extend(
        [
            _news_item("Headline 00", previous_close.replace(hour=21, minute=59)),
            _news_item("Headline 01", previous_close.replace(hour=21, minute=58)),
        ]
    )

    def classify_side_effect(*, ticker, headlines, thesis, thesis_version_hash, dedup_seen):
        captured_headlines.extend(headlines)
        return []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=items), \
         patch("signal_system.jobs.news_morning.classify_headlines", side_effect=classify_side_effect), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.email_sender.send_email"):
        news_morning.run()

    assert len(captured_headlines) == 50
    assert captured_headlines[0]["headline"] == "Headline 52"
    assert captured_headlines[-1]["headline"] == "Headline 03"

    conn = sqlite3.connect(db)
    overflow_rows = conn.execute(
        """
        SELECT title, routing_status
        FROM signals
        WHERE agent = 'news_morning' AND routing_status = 'MONITORING'
        ORDER BY title
        """
    ).fetchall()
    conn.close()
    assert len(overflow_rows) == 3
    assert all("[volume_cap]" in row[0] for row in overflow_rows)


def test_news_morning_parse_failure_monitoring_bypasses_router_and_persists(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc).astimezone()
    monitoring_signal = _sig(ticker="AAPL", severity="MONITORING")
    delivered_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[monitoring_signal, delivered_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(delivered_signal, "DELIVERED", None)]) as mock_route, \
         patch("signal_system.jobs.news_morning.email_sender.send_email"):
        news_morning.run()

    assert mock_route.call_args.args[0] == [delivered_signal]

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT title, routing_status FROM signals ORDER BY routing_status, title"
    ).fetchall()
    conn.close()
    assert ("AAPL: important update", "DELIVERED") in rows
    assert ("AAPL: important update", "MONITORING") in rows


def test_news_morning_digest_counts_zero_alert_and_mismatch_guard(db):
    from signal_system.jobs import news_morning
    from signal_system.jobs.common import DigestPayload

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc).astimezone()
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")
    monitoring_signal = _sig(ticker="AAPL", severity="MONITORING", agent="news_classifier_parse")

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[monitoring_signal, suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", "outscored")]), \
         patch("signal_system.jobs.news_morning.email_sender.send_email") as mock_send:
        news_morning.run()

    assert mock_send.call_count == 1
    assert "Scanned 1 tickers, 0 alerts" in mock_send.call_args.kwargs["body"]
    assert "Suppressed: 1" in mock_send.call_args.kwargs["body"]
    assert "Monitoring: 1" in mock_send.call_args.kwargs["body"]

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch(
             "signal_system.jobs.news_morning.render_digest",
             return_value=DigestPayload(
                 subject="bad digest",
                 body="Scanned 1 tickers, 0 alerts",
                 status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 0},
             ),
         ), \
         patch("signal_system.jobs.news_morning.email_sender.send_email") as mismatch_send:
        with pytest.raises(RuntimeError, match="Digest counts"):
            news_morning.run()

    mismatch_send.assert_not_called()


def test_dispatcher_registers_news_morning():
    from signal_system import __main__
    from signal_system.jobs import news_morning

    assert __main__.JOBS["news-morning"] is news_morning.run
