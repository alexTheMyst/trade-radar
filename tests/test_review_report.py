"""Tests for the review-report computation (review/report.py).

Uses hand-computed fixtures so the excess-return math, direction sign flip,
neutral exclusion, IC calculation, missing-benchmark counting, splits, and
cache warning are all pinned to explicit expectations.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from signal_system.review import report
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _insert_signal(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    agent: str,
    severity: str,
    routing_status: str,
    ticker: str,
    timestamp: datetime,
    score: float | None,
    snapshot: float,
    outcome_30d: float | None = None,
    outcome_90d: float | None = None,
    direction: str | None = None,
    acted: int | None = None,
    weight_version: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO signals (
            alert_id, timestamp, agent, severity, ticker, title, body, score,
            routing_status, signal_price_snapshot, outcome_price_30d, outcome_price_90d,
            direction, acted, weight_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_id, timestamp.isoformat(), agent, severity, ticker,
            f"{alert_id} title", "body", score, routing_status, snapshot,
            outcome_30d, outcome_90d, direction, acted, weight_version,
        ),
    )


def _spy(conn: sqlite3.Connection, date_iso: str, close: float) -> None:
    repository.upsert_benchmark_price(ticker="SPY", date_iso=date_iso, close=close, source="yfinance_backfill")


def test_excess_return_math_and_hit_rate(db):
    """raw_ret = outcome/snapshot - 1; excess = raw - spy_ret; hit = signed>0."""
    conn = sqlite3.connect(db)
    # All on 2026-01-05 (spy 100). 30d outcome 2026-02-04 (spy 110).
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 110.0)
    _spy(conn, "2026-04-06", 120.0)

    # Discovery: snapshot 50, 30d 55 → raw +10%, spy +10% → excess 0
    _insert_signal(conn, alert_id="d1", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=80.0, snapshot=50.0, outcome_30d=55.0)
    # Discovery: snapshot 100, 30d 110 → raw +10%, spy +10% → excess 0
    _insert_signal(conn, alert_id="d2", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="BBB", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=70.0, snapshot=100.0, outcome_30d=110.0)
    conn.commit()
    conn.close()

    result = report.compute_report()

    disc = result.per_agent["discovery_agent"]
    assert disc.n_30d == 2
    # both excess exactly 0.0 → mean 0, hit_rate 0
    assert disc.mean_signed_excess_30d == pytest.approx(0.0)
    assert disc.hit_rate_30d == pytest.approx(0.0)


def test_news_direction_sign_flip_and_neutral_exclusion(db):
    """News negative direction flips excess sign; neutral rows excluded from hit-rate/IC."""
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 110.0)

    # positive: snapshot 50, 30d 55 → raw +10%, spy +10% → excess 0 → signed 0
    _insert_signal(conn, alert_id="n1", agent="news_classifier", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=0.8, snapshot=50.0, outcome_30d=55.0, direction="positive")
    # negative: snapshot 50, 30d 55 → raw +10%, spy +10% → excess 0 → signed 0
    _insert_signal(conn, alert_id="n2", agent="news_classifier", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="BBB", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=0.7, snapshot=50.0, outcome_30d=55.0, direction="negative")
    # neutral: snapshot 50, 30d 55 → excluded from hit-rate/IC but counted
    _insert_signal(conn, alert_id="n3", agent="news_classifier", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="CCC", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=0.6, snapshot=50.0, outcome_30d=55.0, direction="neutral")
    conn.commit()
    conn.close()

    result = report.compute_report()
    news = result.per_agent["news_classifier"]
    assert news.n_30d == 2  # neutral row excluded from hit-rate/IC set
    assert news.neutral_count_30d == 1
    assert news.hit_rate_30d == 0.0  # signed all 0
    assert news.ic_30d is None  # n_effective == 2 < 10


def test_spearman_ic_matches_hand_ranked_expectation(db):
    """Spearman IC over score vs signed_excess for discovery."""
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 105.0)

    # 12 rows: higher score → higher outcome (perfect monotonic) → IC ≈ 1
    for i in range(12):
        _insert_signal(
            conn, alert_id=f"ic{i}", agent="discovery_agent", severity="INFORMATIONAL",
            routing_status="DELIVERED", ticker=f"T{i:02d}",
            timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
            score=float(50 + i), snapshot=100.0, outcome_30d=100.0 + i,
        )
    conn.commit()
    conn.close()

    result = report.compute_report()
    disc = result.per_agent["discovery_agent"]
    assert disc.n_30d == 12
    assert disc.ic_30d is not None
    assert disc.ic_30d > 0.99


def test_ic_suppressed_when_n_less_than_10(db):
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 105.0)
    for i in range(5):
        _insert_signal(conn, alert_id=f"few{i}", agent="discovery_agent", severity="INFORMATIONAL",
                       routing_status="DELIVERED", ticker=f"T{i}", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                       score=50.0 + i, snapshot=100.0, outcome_30d=100.0 + i)
    conn.commit()
    conn.close()

    result = report.compute_report()
    assert result.per_agent["discovery_agent"].ic_30d is None


def test_missing_benchmark_excluded_and_counted(db):
    """Rows missing a SPY benchmark lookup are excluded and counted."""
    conn = sqlite3.connect(db)
    # No SPY row at all → every signal is missing_benchmark
    _insert_signal(conn, alert_id="mb1", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=80.0, snapshot=50.0, outcome_30d=55.0)
    conn.commit()
    conn.close()

    result = report.compute_report()
    disc = result.per_agent["discovery_agent"]
    assert disc.n_30d == 0
    assert disc.missing_benchmark_30d == 1


def test_router_validation_delivered_vs_suppressed(db):
    """Flags when SUPPRESSED beats DELIVERED on mean excess."""
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 110.0)
    # DELIVERED excess 0; SUPPRESSED excess +5%
    _insert_signal(conn, alert_id="dv", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=80.0, snapshot=50.0, outcome_30d=55.0)
    _insert_signal(conn, alert_id="sp", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="SUPPRESSED", ticker="BBB", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=70.0, snapshot=100.0, outcome_30d=115.0)  # raw +15%, spy +10% → excess +5%
    conn.commit()
    conn.close()

    result = report.compute_report()
    assert len(result.router_flags) == 1
    assert result.router_flags[0].startswith(
        "discovery_agent SUPPRESSED beats DELIVERED on 30d mean excess"
    )


def test_acted_vs_ignored_split(db):
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 110.0)
    # acted: snapshot 100, 30d 120 → raw +20%, spy +10% → excess +10%
    _insert_signal(conn, alert_id="acted1", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=80.0, snapshot=100.0, outcome_30d=120.0, acted=1)
    # ignored: snapshot 100, 30d 100 → excess -10%
    _insert_signal(conn, alert_id="ignored1", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="BBB", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=70.0, snapshot=100.0, outcome_30d=100.0, acted=0)
    conn.commit()
    conn.close()

    result = report.compute_report()
    metrics = result.per_agent["discovery_agent"]
    split = metrics.acted_split_30d
    assert split["acted_mean"] == pytest.approx(0.10)
    assert split["ignored_mean"] == pytest.approx(-0.10)
    assert metrics.unacted_count == 0


def test_weight_version_breakdown(db):
    conn = sqlite3.connect(db)
    _spy(conn, "2026-01-05", 100.0)
    _spy(conn, "2026-02-04", 110.0)
    _insert_signal(conn, alert_id="v2", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="AAA", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=80.0, snapshot=50.0, outcome_30d=55.0, weight_version="xsect-50/30/20-v2")
    _insert_signal(conn, alert_id="legacy", agent="discovery_agent", severity="INFORMATIONAL",
                   routing_status="DELIVERED", ticker="BBB", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
                   score=70.0, snapshot=50.0, outcome_30d=55.0, weight_version=None)
    conn.commit()
    conn.close()

    result = report.compute_report()
    breakdown = result.per_agent["discovery_agent"].weight_version_counts
    assert breakdown == {"xsect-50/30/20-v2": 1, "legacy-mixed": 1}


def test_llm_cache_warning_when_never_cached(db):
    conn = sqlite3.connect(db)
    for _ in range(20):
        conn.execute(
            """
            INSERT INTO llm_calls (job, model_version, input_tokens, output_tokens,
                                   cache_read_input_tokens, cache_creation_input_tokens, timestamp)
            VALUES ('news_classifier', 'm', 100, 10, 0, 0, '2026-07-01T08:00:00-04:00')
            """
        )
    conn.commit()
    conn.close()

    result = report.compute_report()
    assert result.cache_warning is True


def test_llm_no_warning_when_cached(db):
    conn = sqlite3.connect(db)
    for i in range(20):
        conn.execute(
            """
            INSERT INTO llm_calls (job, model_version, input_tokens, output_tokens,
                                   cache_read_input_tokens, cache_creation_input_tokens, timestamp)
            VALUES ('news_classifier', 'm', 100, 10, ?, ?, '2026-07-01T08:00:00-04:00')
            """,
            (i * 10, i * 10),
        )
    conn.commit()
    conn.close()

    result = report.compute_report()
    assert result.cache_warning is False
