"""Repository tests for the MONITORING-corpus read path and measurement support.

Copies the `monkeypatch DB_PATH` fixture pattern from test_alert_router.py.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _sig(
    *,
    ticker: str = "AAPL",
    severity: str = "MONITORING",
    agent: str = "news_classifier",
    title: str = "[off_thesis] some headline",
    body: str = "rationale",
    timestamp: datetime | None = None,
) -> Signal:
    ts = timestamp or datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)
    return Signal(
        ticker=ticker,
        score=0.2,
        severity=severity,
        agent=agent,
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), f"rule-{title[:8]}", agent),
        title=title,
        body=body,
    )


def _insert(signal: Signal, routing_status: str = "MONITORING") -> None:
    repository.insert_signal(signal, routing_status=routing_status)


def test_get_monitoring_signals_since_filters_by_prefix_and_recency(db):
    old_off_thesis = _sig(title="[off_thesis] old headline", timestamp=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    recent_off_thesis = _sig(title="[off_thesis] fresh headline", timestamp=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc))
    volume_cap = _sig(title="[volume_cap] skipped due to cap", timestamp=datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc))
    parse_failure = _sig(title="[parse_failure] bad json", timestamp=datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc))
    unrelated = _sig(title="AAPL: important update", timestamp=datetime(2026, 7, 25, 11, 0, tzinfo=timezone.utc))
    delivered = _sig(
        title="[off_thesis] delivered row",
        timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )
    for sig in (old_off_thesis, recent_off_thesis, volume_cap, parse_failure, unrelated):
        _insert(sig)
    _insert(delivered, routing_status="DELIVERED")

    rows = repository.get_monitoring_signals_since(date(2026, 7, 15))

    titles = [row["title"] for row in rows]
    assert titles == [
        "[volume_cap] skipped due to cap",
        "[off_thesis] fresh headline",
    ]
    assert all(row["routing_status"] == "MONITORING" for row in rows)


def test_get_monitoring_signals_since_default_prefixes_exclude_parse_failure(db):
    off_thesis = _sig(title="[off_thesis] a headline")
    parse_failure = _sig(title="[parse_failure] bad json")
    _insert(off_thesis)
    _insert(parse_failure)

    rows = repository.get_monitoring_signals_since(date(2026, 7, 1))
    assert [row["title"] for row in rows] == ["[off_thesis] a headline"]


def test_get_monitoring_signals_since_newest_first_and_limit(db):
    signals = [
        _sig(title=f"[off_thesis] headline {i}", timestamp=datetime(2026, 7, 20 + i, 8, 0, tzinfo=timezone.utc))
        for i in range(4)
    ]
    for sig in signals:
        _insert(sig)

    rows = repository.get_monitoring_signals_since(date(2026, 7, 1), limit=2)
    assert [row["title"] for row in rows] == ["[off_thesis] headline 3", "[off_thesis] headline 2"]


def test_get_monitoring_signals_since_custom_prefixes(db):
    volume_cap = _sig(title="[volume_cap] a volume cap")
    off_thesis = _sig(title="[off_thesis] an off thesis")
    _insert(volume_cap)
    _insert(off_thesis)

    rows = repository.get_monitoring_signals_since(date(2026, 7, 1), title_prefixes=("[volume_cap]",))
    assert [row["title"] for row in rows] == ["[volume_cap] a volume cap"]


def test_get_monitoring_signals_since_empty_result(db):
    assert repository.get_monitoring_signals_since(date(2026, 7, 1)) == []


def test_get_monitoring_signals_since_row_shape(db):
    sig = _sig(title="[off_thesis] shaped headline", body="the rationale")
    _insert(sig)
    rows = repository.get_monitoring_signals_since(date(2026, 7, 1))
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {
        "alert_id", "timestamp", "agent", "severity", "ticker",
        "title", "body", "score", "routing_status",
    }
    assert row["body"] == "the rationale"
    assert row["score"] == 0.2
    assert row["agent"] == "news_classifier"
    assert row["routing_status"] == "MONITORING"


# ---------------------------------------------------------------------------
# benchmark_prices
# ---------------------------------------------------------------------------

def test_upsert_benchmark_price_inserts_and_replaces(db):
    repository.upsert_benchmark_price(ticker="SPY", date_iso="2026-07-20", close=591.42, source="finnhub_quote")
    repository.upsert_benchmark_price(ticker="SPY", date_iso="2026-07-20", close=592.00, source="yfinance_backfill")

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, date, close, source FROM benchmark_prices WHERE ticker='SPY'"
    ).fetchall()
    conn.close()

    assert rows == [("SPY", "2026-07-20", 592.00, "yfinance_backfill")]


def test_get_benchmark_close_on_or_after_returns_first_trading_day(db):
    for date_iso, close in [("2026-07-20", 591.0), ("2026-07-21", 592.0), ("2026-07-22", 593.0)]:
        repository.upsert_benchmark_price(ticker="SPY", date_iso=date_iso, close=close, source="finnhub_quote")

    # Target falls on a stored date → that exact close
    assert repository.get_benchmark_close_on_or_after("SPY", "2026-07-21") == 592.0
    # Target before first stored date → first trading day >= target
    assert repository.get_benchmark_close_on_or_after("SPY", "2026-07-15") == 591.0
    # Target after last stored date → None
    assert repository.get_benchmark_close_on_or_after("SPY", "2026-07-30") is None


def test_get_benchmark_close_on_or_after_missing_ticker(db):
    assert repository.get_benchmark_close_on_or_after("QQQ", "2026-07-20") is None


def test_get_earliest_signal_date_returns_oldest_signal(db):
    older = _sig(
        title="[off_thesis] old",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    newer = _sig(
        title="[off_thesis] new",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
    )
    repository.insert_signal(older, routing_status="DELIVERED")
    repository.insert_signal(newer, routing_status="DELIVERED")

    assert repository.get_earliest_signal_date() == date(2026, 6, 1)


def test_get_earliest_signal_date_empty(db):
    assert repository.get_earliest_signal_date() is None


def test_get_pillar_routing_counts_since(db):
    def _pillar_sig(pillar: str, title: str) -> Signal:
        sig = _sig(title=title, severity="INFORMATIONAL")
        return Signal(
            ticker=sig.ticker, score=sig.score, severity=sig.severity, agent=sig.agent,
            timestamp=sig.timestamp, alert_id=sig.alert_id, title=sig.title, body=sig.body,
            pillar=pillar,
        )

    repository.insert_signal(_pillar_sig("growth", "a"), routing_status="DELIVERED")
    repository.insert_signal(_pillar_sig("growth", "b"), routing_status="DELIVERED")
    repository.insert_signal(_pillar_sig("rates", "c"), routing_status="SUPPRESSED")

    counts = repository.get_pillar_routing_counts_since(date(2026, 7, 1))
    assert counts["growth"]["DELIVERED"] == 2
    assert counts["growth"]["SUPPRESSED"] == 0
    assert counts["rates"]["SUPPRESSED"] == 1


def test_get_agent_signals_since_filters_by_agent(db):
    _insert(_sig(ticker="AAPL", title="[off_thesis] a", agent="news_classifier"))
    _insert(_sig(ticker="MSFT", title="[off_thesis] b", agent="news_classifier"))
    repository.insert_signal(
        _sig(ticker="NVDA", title="disc", agent="discovery_agent", severity="INFORMATIONAL"),
        routing_status="DELIVERED",
    )
    rows = repository.get_agent_signals_since("news_classifier", date(2026, 7, 1))
    assert len(rows) == 2
    assert all(r["agent"] == "news_classifier" for r in rows)


def test_insert_synthesis_report_roundtrip(db):
    repository.insert_synthesis_report(
        run_id="run-1",
        week_start="2026-07-20",
        corpus_size=5,
        themes_json="[]",
        candidates_json="[]",
        model_version="m",
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT run_id, week_start, corpus_size, themes_json, candidates_json, model_version "
        "FROM synthesis_reports WHERE run_id='run-1'"
    ).fetchone()
    conn.close()
    assert row[0] == "run-1"
    assert row[1] == "2026-07-20"
    assert row[2] == 5
    assert row[5] == "m"


def test_insert_signal_persists_weight_version(db):
    sig = _sig(title="[off_thesis] headline", severity="INFORMATIONAL")
    repository.insert_signal(sig, routing_status="DELIVERED")

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT weight_version FROM signals WHERE alert_id=?", (sig.alert_id,)).fetchone()
    conn.close()
    assert row[0] is None  # NULL default for non-Discovery signals


# ---------------------------------------------------------------------------
# acted-recording CLI support
# ---------------------------------------------------------------------------

def test_find_signals_by_alert_prefix_matches_unique(db):
    sig = _sig(title="[off_thesis] headline", severity="INFORMATIONAL")
    repository.insert_signal(sig, routing_status="DELIVERED")

    matches = repository.find_signals_by_alert_prefix(sig.alert_id[:8])
    assert len(matches) == 1
    assert matches[0]["alert_id"] == sig.alert_id
    assert matches[0]["acted"] is None


def test_find_signals_by_alert_prefix_ambiguous_returns_all(db):
    base = _sig(title="[off_thesis] a", severity="INFORMATIONAL")
    other = _sig(title="[off_thesis] b", severity="INFORMATIONAL")
    # Two signals sharing the same 8-char prefix
    same_prefix = base.alert_id[:8]
    colliding = _sig(title="[off_thesis] c", severity="INFORMATIONAL")
    colliding = Signal(
        ticker=colliding.ticker, score=colliding.score, severity=colliding.severity,
        agent=colliding.agent, timestamp=colliding.timestamp,
        alert_id=same_prefix + "deadbeefcafe1234", title=colliding.title,
        body=colliding.body,
    )
    repository.insert_signal(base, routing_status="DELIVERED")
    repository.insert_signal(other, routing_status="DELIVERED")
    repository.insert_signal(colliding, routing_status="DELIVERED")

    matches = repository.find_signals_by_alert_prefix(same_prefix)
    assert len(matches) == 2


def test_find_signals_by_alert_prefix_invalid_raises(db):
    with pytest.raises(ValueError):
        repository.find_signals_by_alert_prefix("not-hex!")
    with pytest.raises(ValueError):
        repository.find_signals_by_alert_prefix("")


def test_update_signal_acted_stamps_acted_at(db):
    sig = _sig(title="[off_thesis] headline", severity="INFORMATIONAL")
    repository.insert_signal(sig, routing_status="DELIVERED")

    repository.update_signal_acted(sig.alert_id, acted=True, user_note="bought 10")

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT acted, acted_at, user_note FROM signals WHERE alert_id=?", (sig.alert_id,)
    ).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] is not None
    assert row[2] == "bought 10"


def test_update_signal_acted_passed_sets_zero(db):
    sig = _sig(title="[off_thesis] headline", severity="INFORMATIONAL")
    repository.insert_signal(sig, routing_status="DELIVERED")

    repository.update_signal_acted(sig.alert_id, acted=False, user_note=None)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT acted, acted_at, user_note FROM signals WHERE alert_id=?", (sig.alert_id,)).fetchone()
    conn.close()
    assert row[0] == 0
    assert row[1] is not None


def test_list_recent_delivered_signals_filters_by_window_and_status(db):
    older = _sig(
        title="[off_thesis] older",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
    )
    recent = _sig(
        title="[off_thesis] recent",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )
    suppressed = _sig(
        title="[off_thesis] suppressed",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
    )
    repository.insert_signal(older, routing_status="DELIVERED")
    repository.insert_signal(recent, routing_status="DELIVERED")
    repository.insert_signal(suppressed, routing_status="SUPPRESSED")

    rows = repository.list_recent_delivered_signals(days=14, now=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc))
    assert [row["alert_id"] for row in rows] == [recent.alert_id]


def test_count_unacted_delivered_older_than(db):
    unacted_old = _sig(
        title="[off_thesis] old unacted",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
    )
    acted_old = _sig(
        title="[off_thesis] old acted",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
    )
    recent_unacted = _sig(
        title="[off_thesis] recent",
        severity="INFORMATIONAL",
        timestamp=datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc),
    )
    repository.insert_signal(unacted_old, routing_status="DELIVERED")
    repository.insert_signal(acted_old, routing_status="DELIVERED")
    repository.update_signal_acted(acted_old.alert_id, acted=True)
    repository.insert_signal(recent_unacted, routing_status="DELIVERED")

    count = repository.count_unacted_delivered_older_than(days=7, now=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc))
    assert count == 1  # only unacted_old (7/1) is >7 days old and unacted
