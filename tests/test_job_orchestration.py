from __future__ import annotations

import csv
from contextlib import contextmanager
import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest

from signal_system.data import universe
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


@pytest.fixture(autouse=True)
def _no_network_general_news(monkeypatch):
    """Every news-morning run test must not hit the live general-news endpoint.

    Individual tests that specifically exercise general news override this by
    patching fetch_general_news themselves later in their own patch context.
    """
    monkeypatch.setattr(
        "signal_system.jobs.news_morning.fetch_general_news",
        lambda *a, **kw: [],
    )


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
    timestamp = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
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


def test_routed_persistence_stores_every_tuple_with_demotions_and_suppression_reasons():
    from signal_system.jobs.common import persist_routed_signals

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    demoted = _sig(ticker="MSFT", severity="INFORMATIONAL", score=0.71)
    suppressed = _sig(ticker="SNOW", severity="INFORMATIONAL", score=0.60)

    with patch("signal_system.state.repository.insert_signal") as mock_insert:
        summary = persist_routed_signals(
            [
                (delivered, "DELIVERED", None, None),
                (demoted, "DELIVERED", "ACTION_REQUIRED", None),
                (suppressed, "SUPPRESSED", None, "outscored"),
            ]
        )

    assert summary.delivered_signals == [delivered, demoted]
    assert summary.status_counts == {"DELIVERED": 2, "SUPPRESSED": 1, "MONITORING": 0}
    mock_insert.assert_has_calls(
        [
            call(delivered, routing_status="PENDING", demoted_from=None, suppression_reason=None),
            call(demoted, routing_status="PENDING", demoted_from="ACTION_REQUIRED", suppression_reason=None),
            call(suppressed, routing_status="SUPPRESSED", demoted_from=None, suppression_reason="outscored"),
        ],
        any_order=True,
    )


def test_shared_digest_details_only_delivered_and_counts_non_delivered():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.91)
    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        routed_signals=[(delivered, "DELIVERED", None, None)],
        held_tickers=set(),
        score_deltas={},
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
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 3},
    )

    assert "Scanned 4 tickers, 0 alerts" in payload.body
    assert "Suppressed: 2" in payload.body
    assert "Monitoring: 3" in payload.body


def test_render_digest_surfaces_demotion_with_outscored_winner():
    from signal_system.jobs.common import render_digest

    # 83.0/81.0 are Discovery composites, so the agent must be discovery_agent —
    # see test_news_classifier_demoted_delivery_names_winner_confidence for the
    # confidence-scaled counterpart of this same rendering path.
    winner = _sig(ticker="SCHD", severity="ACTION_REQUIRED", agent="discovery_agent", score=83.0)
    demoted = _sig(ticker="AAPL", severity="INFORMATIONAL", agent="discovery_agent", score=81.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[
            (winner, "DELIVERED", None, None),
            (demoted, "DELIVERED", "ACTION_REQUIRED", None),
        ],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 2, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert (
        "AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED, outscored by SCHD 83)"
        in payload.body
    )


def test_render_digest_demotion_without_batch_winner_omits_outscored_detail():
    from signal_system.jobs.common import render_digest

    demoted = _sig(ticker="AAPL", severity="INFORMATIONAL", score=81.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(demoted, "DELIVERED", "ACTION_REQUIRED", None)],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED)" in payload.body
    assert "outscored by" not in payload.body


def test_render_digest_tags_held_tickers_on_delivered_lines():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="NVDA", severity="ACTION_REQUIRED", score=88.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=10,
        routed_signals=[(delivered, "DELIVERED", None, None)],
        held_tickers={"NVDA"},
        score_deltas={},
        status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "NVDA — ACTION_REQUIRED — HELD" in payload.body


def test_render_digest_suppressed_section_cap_exhausted_with_held_and_delta(db):
    from signal_system.jobs.common import render_digest

    nvda = Signal(
        ticker="NVDA", score=74.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("NVDA", "2026-05-19", "discovery", "discovery_agent"),
        title="NVDA: Discovery score 74",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(nvda, "SUPPRESSED", None, "cap_exhausted")],
        held_tickers={"NVDA"},
        score_deltas={nvda.alert_id: "+12"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "Suppressed (1)" in payload.body
    assert "-" * len("Suppressed (1)") in payload.body
    assert "NVDA — 74 (+12) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD" in payload.body


def test_render_digest_suppressed_section_ar_cap_exhausted_fraction():
    from signal_system.jobs.common import render_digest

    # score=91.0 is a Discovery composite, so the agent must be discovery_agent —
    # a news_classifier score is a confidence in [0.0, 1.0] and renders as a
    # percentage. The subject of this test is the "AR 1/1" cap fraction.
    sig = _sig(ticker="XOM", severity="ACTION_REQUIRED", agent="discovery_agent", score=91.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=5,
        routed_signals=[(sig, "SUPPRESSED", None, "cap_exhausted")],
        held_tickers=set(),
        score_deltas={sig.alert_id: "NEW"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "XOM — 91 (NEW) — ACTION_REQUIRED — cap_exhausted (AR 1/1)" in payload.body


def test_render_digest_suppressed_section_outscored_no_cap_suffix():
    from signal_system.jobs.common import render_digest

    rtx = Signal(
        ticker="RTX", score=69.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-19", "discovery", "discovery_agent"),
        title="RTX: Discovery score 69",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(rtx, "SUPPRESSED", None, "outscored")],
        held_tickers=set(),
        score_deltas={rtx.alert_id: "NEW"},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "RTX — 69 (NEW) — INFORMATIONAL — outscored" in payload.body
    assert "cap_exhausted" not in payload.body


def test_render_digest_omits_suppressed_section_when_none():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="discovery",
        scanned_tickers=5,
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert "Suppressed (" not in payload.body


def test_render_digest_suppressed_section_sorted_by_score_descending():
    from signal_system.jobs.common import render_digest

    low = _sig(ticker="CDE", severity="INFORMATIONAL", score=61.0)
    high = _sig(ticker="NVDA", severity="INFORMATIONAL", score=74.0)
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[
            (low, "SUPPRESSED", None, "cap_exhausted"),
            (high, "SUPPRESSED", None, "cap_exhausted"),
        ],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 2, "MONITORING": 0},
    )

    nvda_pos = payload.body.index("NVDA")
    cde_pos = payload.body.index("CDE")
    assert nvda_pos < cde_pos


def test_render_digest_monitoring_breakdown_renders_all_categories():
    from signal_system.jobs.common import render_digest

    delivered = _sig(ticker="AAPL", severity="INFORMATIONAL", score=0.75)
    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        routed_signals=[(delivered, "DELIVERED", None, None)],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 1, "SUPPRESSED": 0, "MONITORING": 5},
        monitoring_breakdown={
            "off_thesis": 2,
            "parse_failure": 1,
            "volume_cap": 1,
            "reconciled": 1,
        },
    )

    assert (
        "Monitoring: 5 (off-thesis 2, parse-fail 1, volume-cap 1, reconciled 1)"
        in payload.body
    )


def test_render_digest_monitoring_breakdown_omits_zero_categories():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=2,
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 2},
        monitoring_breakdown={"off_thesis": 2},
    )

    assert "Monitoring: 2 (off-thesis 2)" in payload.body
    assert "parse-fail" not in payload.body
    assert "volume-cap" not in payload.body
    assert "reconciled" not in payload.body


def test_render_digest_monitoring_breakdown_none_renders_bare_count():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=2,
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 3},
    )

    assert "Monitoring: 3" in payload.body
    assert "(off-thesis" not in payload.body


def test_news_morning_requires_previous_daily_close_before_fetch_or_delivery(db):
    from signal_system.jobs import news_morning

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=None), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", side_effect=AssertionError("should not fetch")), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
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
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
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

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    fetch_calls: list[tuple[date, date]] = []

    def fetch_side_effect(ticker: str, from_date: date, to_date: date):
        fetch_calls.append((from_date, to_date))
        return []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL", "MSFT"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0, "MSFT": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", side_effect=fetch_side_effect) as mock_fetch, \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    assert mock_fetch.call_args_list[0].args[0] == "AAPL"
    assert mock_fetch.call_args_list[1].args[0] == "MSFT"
    assert fetch_calls == [(date(2026, 5, 16), fixed_now.date()), (date(2026, 5, 16), fixed_now.date())]
    assert mock_send.call_count == 1
    assert "Scanned 2 tickers, 0 alerts" in mock_send.call_args.args[0]

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'news-morning'").fetchone()
    conn.close()
    assert status == ("success",)


def test_news_morning_headline_cap_dedups_before_cap_and_persists_overflow(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    previous_close = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    captured_headlines: list[dict] = []
    items = []
    for index in range(53):
        items.append(_news_item(f"Headline {index:02d}", previous_close.replace(hour=20 + (index // 60), minute=index % 60)))
    items.extend(
        [
            _news_item("Headline 00", previous_close.replace(hour=21, minute=59)),
            _news_item("Headline 01", previous_close.replace(hour=21, minute=58)),
        ]
    )

    def classify_side_effect(*, ticker, headlines, thesis, thesis_version_hash, dedup_seen, weights=None):
        captured_headlines.extend(headlines)
        return []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=items), \
         patch("signal_system.jobs.news_morning.classify_headlines", side_effect=classify_side_effect), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
        news_morning.run()

    assert len(captured_headlines) == 50
    assert captured_headlines[0]["headline"] == "Headline 00"
    assert captured_headlines[-1]["headline"] == "Headline 05"

    conn = sqlite3.connect(db)
    overflow_rows = conn.execute(
        """
        SELECT title, routing_status, timestamp
        FROM signals
        WHERE agent = 'news_classifier' AND routing_status = 'MONITORING'
        ORDER BY title
        """
    ).fetchall()
    # No signal should be written under the legacy 'news_morning' agent name.
    legacy_count = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM signals WHERE agent = 'news_morning'"
    ).fetchone()[0]
    conn.close()
    assert len(overflow_rows) == 3
    assert all("[volume_cap]" in row[0] for row in overflow_rows)
    # timestamp is signal-generation time (the run), NOT article publication time
    assert all(row[2] == fixed_now.isoformat() for row in overflow_rows)
    assert legacy_count == 0


def test_dedupe_collapses_same_headline_across_tickers():
    """Same article surfaced under two tickers (no Finnhub id) is kept once."""
    from signal_system.jobs import news_morning

    dt = datetime(2026, 5, 20, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    headline = "Nvidia's $5.7 Trillion Market Cap Faces Its Toughest Test"
    candidates = [
        ("NVDA", {"headline": headline, "datetime": int(dt.timestamp())}, dt),
        ("MSFT", {"headline": headline, "datetime": int(dt.timestamp())}, dt),
    ]

    kept, overflow = news_morning._dedupe_and_cap_headlines(candidates)

    assert len(kept) == 1
    assert kept[0][0] == "NVDA"  # first occurrence wins
    assert overflow == []


def test_dedupe_collapses_same_article_id_across_tickers():
    """Same Finnhub article id across tickers collapses even if headlines differ."""
    from signal_system.jobs import news_morning

    dt = datetime(2026, 5, 20, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    candidates = [
        ("NVDA", {"id": 999, "headline": "Nvidia leads AI rally", "datetime": int(dt.timestamp())}, dt),
        ("MSFT", {"id": 999, "headline": "AI rally, Nvidia leads", "datetime": int(dt.timestamp())}, dt),
    ]

    kept, overflow = news_morning._dedupe_and_cap_headlines(candidates)

    assert len(kept) == 1
    assert overflow == []


def test_dedupe_keeps_distinct_articles_for_same_ticker():
    """Two genuinely different articles for one ticker are both kept."""
    from signal_system.jobs import news_morning

    dt = datetime(2026, 5, 20, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    candidates = [
        ("NVDA", {"id": 1, "headline": "Nvidia earnings beat", "datetime": int(dt.timestamp())}, dt),
        ("NVDA", {"id": 2, "headline": "Nvidia new chip launch", "datetime": int(dt.timestamp())}, dt),
    ]

    kept, overflow = news_morning._dedupe_and_cap_headlines(candidates)

    assert len(kept) == 2


def test_news_morning_parse_failure_monitoring_bypasses_router_and_persists(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    monitoring_signal = _sig(ticker="AAPL", severity="MONITORING", agent="news_classifier_parse")
    delivered_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[monitoring_signal, delivered_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(delivered_signal, "DELIVERED", None, None)]) as mock_route, \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
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

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")
    monitoring_signal = _sig(ticker="AAPL", severity="MONITORING", agent="news_classifier_parse")

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[monitoring_signal, suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "outscored")]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    assert mock_send.call_count == 1
    assert "Scanned 1 tickers, 0 alerts" in mock_send.call_args.args[0]
    assert "Suppressed: 1" in mock_send.call_args.args[0]
    assert "Monitoring: 1" in mock_send.call_args.args[0]

    body = mock_send.call_args.args[0]
    assert "Suppressed (1)" in body
    # News Classifier scores are confidences in [0.0, 1.0] and render as
    # percentages — a bare ":.0f" would collapse 0.75 to "1".
    assert "AAPL — 75% (NEW) — INFORMATIONAL — outscored — HELD" in body

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
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
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mismatch_send:
        with pytest.raises(RuntimeError, match="Digest counts"):
            news_morning.run()

    mismatch_send.assert_not_called()


def test_news_morning_digest_monitoring_breakdown_composed_end_to_end(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    previous_close = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    off_thesis = _sig(ticker="AAPL", severity="MONITORING", agent="news_classifier")
    off_thesis = Signal(
        ticker=off_thesis.ticker,
        score=off_thesis.score,
        severity=off_thesis.severity,
        agent=off_thesis.agent,
        timestamp=off_thesis.timestamp,
        alert_id=off_thesis.alert_id,
        title="[off_thesis] unrelated story",
        body="not about a pillar",
    )
    reconciled_loser = _sig(ticker="AAPL", severity="INFORMATIONAL", agent="news_classifier")

    items = []
    for index in range(51):
        items.append(
            _news_item(
                f"Headline {index:02d}",
                previous_close.replace(hour=20 + (index // 60), minute=index % 60),
            )
        )

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=items), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[off_thesis]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch(
             "signal_system.jobs.news_morning.reconcile_directions",
             return_value=([], [reconciled_loser]),
         ), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    assert mock_send.call_count == 1
    body = mock_send.call_args.args[0]
    assert "Monitoring: 3 (off-thesis 1, volume-cap 1, reconciled 1)" in body


def test_news_morning_appends_unacted_nag_when_overdue(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=3), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    body = mock_send.call_args.args[0]
    assert "3 delivered signal(s) from 7+ days ago are unreviewed" in body


def test_news_morning_no_nag_when_none_unacted(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=0), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    body = mock_send.call_args.args[0]
    assert "unreviewed" not in body


def test_news_morning_macro_headline_renders_macro_in_digest(db):
    """A classified macro headline (ticker=None) renders as MACRO in the digest."""
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    macro_signal = Signal(
        ticker=None, score=0.9, severity="ACTION_REQUIRED", agent="news_classifier",
        timestamp=fixed_now, alert_id=compute_alert_id(None, "2026-05-19", "r", "news_classifier"),
        title="Fed hikes: rates",
    )

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[]), \
         patch("signal_system.jobs.news_morning.fetch_general_news", return_value=[_news_item("Fed hikes", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[macro_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(macro_signal, "DELIVERED", None, None)]), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=0), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    body = mock_send.call_args.args[0]
    assert "MACRO — ACTION_REQUIRED" in body


def test_news_morning_cross_feed_dedup_keeps_one(db):
    """The same article id in both company and general feeds alerts only once."""
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    previous_close = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    item = {**_news_item("Shared Fed story", previous_close), "id": 7777}

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[item]), \
         patch("signal_system.jobs.news_morning.fetch_general_news", return_value=[item]), \
         patch("signal_system.jobs.news_morning.classify_headlines") as mock_classify, \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=0), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
        news_morning.run()

    assert mock_classify.call_count == 1, "Cross-feed dedup should classify the article once"


def test_news_morning_general_cap_independent_of_company_cap(db):
    """General news is capped at 20 independently of the 50-company cap."""
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    previous_close = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    general_items = [
        {**_news_item(f"General story {i}", previous_close), "id": 1000 + i}
        for i in range(25)
    ]
    captured: list[tuple] = []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[]), \
         patch("signal_system.jobs.news_morning.fetch_general_news", return_value=general_items), \
         patch("signal_system.jobs.news_morning.classify_headlines",
               side_effect=lambda ticker, headlines, **kw: captured.extend(headlines) or []), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[]), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=0), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
        news_morning.run()

    assert len(captured) == 20, f"General cap should keep 20, classified {len(captured)}"


def test_news_morning_macro_reconciliation_groups_macro_headlines(db):
    """Two contradicting macro headlines on one pillar reconcile (one loser)."""
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    pos = Signal(
        ticker=None, score=0.95, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=fixed_now, alert_id=compute_alert_id(None, "2026-05-19", "pos", "news_classifier"),
        title="Fed cuts: positive", direction="positive", pillar="monetary_policy",
    )
    neg = Signal(
        ticker=None, score=0.60, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=fixed_now, alert_id=compute_alert_id(None, "2026-05-19", "neg", "news_classifier"),
        title="Fed hikes: negative", direction="negative", pillar="monetary_policy",
    )
    routed: list = []

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[]), \
         patch("signal_system.jobs.news_morning.fetch_general_news", return_value=[]), \
         patch("signal_system.jobs.news_morning._classify_kept_headlines",
               return_value=([pos, neg], {"off_thesis": 0, "parse_failure": 0}, 0)), \
         patch("signal_system.jobs.news_morning.route_signals",
               side_effect=lambda sigs: routed.extend(sigs) or []), \
         patch("signal_system.jobs.news_morning.repository.count_unacted_delivered_older_than", return_value=0), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
        news_morning.run()

    assert len(routed) == 1, "Only the reconciled winner should reach the router"


def test_discovery_routes_persists_and_sends_digest(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    delivered_signal = _sig(
        ticker="AAPL",
        severity="ACTION_REQUIRED",
        agent="discovery_agent",
        score=88.0,
    )
    suppressed_signal = _sig(
        ticker="MSFT",
        severity="INFORMATIONAL",
        agent="discovery_agent",
        score=71.0,
    )

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL", "MSFT"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=["MSFT"]), \
         patch(
             "signal_system.jobs.discovery.score_universe",
             return_value=[delivered_signal, suppressed_signal],
         ), \
         patch(
             "signal_system.jobs.discovery.route_signals",
             return_value=[
                 (delivered_signal, "DELIVERED", None, None),
                 (suppressed_signal, "SUPPRESSED", None, "cap_exhausted"),
             ],
         ) as mock_route, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        discovery.run()

    mock_route.assert_called_once_with([delivered_signal, suppressed_signal])
    assert mock_send.call_count == 1
    body = mock_send.call_args.args[0]
    assert "Scanned 2 tickers, 1 alert" in body
    assert "AAPL: important update" in body
    assert "Suppressed: 1" in body
    assert "Monitoring: 0" in body
    assert "Suppressed (1)" in body
    assert "cap_exhausted (INFO 3/3)" in body
    assert "MSFT — 71" in body.split("Suppressed (1)")[1]
    assert "— HELD" in body.split("Suppressed (1)")[1]

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ticker, routing_status, demoted_from, suppression_reason FROM signals ORDER BY ticker"
    ).fetchall()
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert rows == [
        ("AAPL", "DELIVERED", None, None),
        ("MSFT", "SUPPRESSED", None, "cap_exhausted"),
    ]
    assert status == ("success",)


def test_news_morning_suppressed_signal_shows_held_flag_end_to_end(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL", score=0.76)

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "cap_exhausted")]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    body = mock_send.call_args.args[0]
    assert "Suppressed (1)" in body
    assert "cap_exhausted (INFO 3/3)" in body
    assert "AAPL" in body.split("Suppressed (1)")[1]
    assert "— HELD" in body.split("Suppressed (1)")[1]


def test_discovery_zero_alert_digest_even_when_score_returns_empty(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=[]), \
         patch("signal_system.jobs.discovery.route_signals", return_value=[]) as mock_route, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        discovery.run()

    mock_route.assert_called_once_with([])
    assert mock_send.call_count == 1
    assert "Scanned 1 tickers, 0 alerts" in mock_send.call_args.args[0]
    assert "Suppressed: 0" in mock_send.call_args.args[0]
    assert "Monitoring: 0" in mock_send.call_args.args[0]

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert status == ("success",)


def test_discovery_fails_on_digest_count_mismatch(db):
    from signal_system.jobs import discovery
    from signal_system.jobs.common import DigestPayload

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    routed_signal = _sig(ticker="AAPL", agent="discovery_agent", score=88.0)

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["AAPL"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=[routed_signal]), \
         patch(
             "signal_system.jobs.discovery.route_signals",
             return_value=[(routed_signal, "DELIVERED", None, None)],
         ), \
         patch(
             "signal_system.jobs.discovery.render_digest",
             return_value=DigestPayload(
                 subject="bad digest",
                 body="Scanned 1 tickers, 0 alerts",
                 status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 0},
             ),
         ), \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        with pytest.raises(RuntimeError, match="Digest counts"):
            discovery.run()

    mock_send.assert_not_called()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert status == ("failed",)


def test_require_non_empty_universe_raises_on_empty_and_passes_through():
    from signal_system.data.universe import EmptyUniverseError, require_non_empty_universe

    assert require_non_empty_universe(["AAPL"], job="discovery") == ["AAPL"]
    with pytest.raises(EmptyUniverseError, match="empty ticker universe"):
        require_non_empty_universe([], job="discovery")


def test_discovery_fails_loudly_on_empty_universe(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=[]), \
         patch("signal_system.jobs.discovery.score_universe", side_effect=AssertionError("should not score")) as mock_score, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message") as mock_send:
        with pytest.raises(RuntimeError, match="empty ticker universe"):
            discovery.run()

    mock_score.assert_not_called()
    mock_send.assert_not_called()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'discovery'").fetchone()
    conn.close()
    assert status == ("failed",)


def test_news_morning_fails_loudly_on_empty_universe(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=[]), \
         patch("signal_system.jobs.news_morning.fetch_company_news", side_effect=AssertionError("should not fetch")) as mock_fetch, \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        with pytest.raises(RuntimeError, match="empty ticker universe"):
            news_morning.run()

    mock_fetch.assert_not_called()
    mock_send.assert_not_called()

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'news-morning'").fetchone()
    conn.close()
    assert status == ("failed",)


def test_dispatcher_registers_news_morning():
    from signal_system import __main__
    from signal_system.jobs import discovery, news_morning

    assert __main__.JOBS["news-morning"] is news_morning.run
    assert __main__.JOBS["discovery"] is discovery.run


def test_compute_score_deltas_new_when_no_prior_signal(db):
    from signal_system.jobs.common import compute_score_deltas

    sig = _sig(ticker="AAPL", agent="discovery_agent", score=76.0)
    deltas = compute_score_deltas([(sig, "SUPPRESSED", None, "cap_exhausted")])

    assert deltas == {sig.alert_id: "NEW"}


def test_compute_score_deltas_formats_signed_difference(db):
    from signal_system.jobs.common import compute_score_deltas

    earlier = Signal(
        ticker="AAPL", score=45.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 17, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-17", "discovery", "discovery_agent"),
        title="AAPL: Discovery score 45",
    )
    repository.insert_signal(earlier, routing_status="DELIVERED")

    later = Signal(
        ticker="AAPL", score=76.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-19", "discovery", "discovery_agent"),
        title="AAPL: Discovery score 76",
    )

    deltas = compute_score_deltas([(later, "DELIVERED", None, None)])

    assert deltas == {later.alert_id: "+31"}


def test_compute_score_deltas_formats_negative_difference(db):
    from signal_system.jobs.common import compute_score_deltas

    earlier = Signal(
        ticker="RTX", score=78.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 17, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-17", "discovery", "discovery_agent"),
        title="RTX: Discovery score 78",
    )
    repository.insert_signal(earlier, routing_status="SUPPRESSED")

    later = Signal(
        ticker="RTX", score=69.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("RTX", "2026-05-19", "discovery", "discovery_agent"),
        title="RTX: Discovery score 69",
    )

    deltas = compute_score_deltas([(later, "SUPPRESSED", None, "outscored")])

    assert deltas == {later.alert_id: "-9"}


def test_compute_score_deltas_skips_signals_without_ticker_or_score(db):
    from signal_system.jobs.common import compute_score_deltas

    no_ticker = Signal(
        ticker=None, score=50.0, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id(None, "2026-05-19", "r1", "news_classifier"),
        title="untagged",
    )
    no_score = Signal(
        ticker="MSFT", score=None, severity="MONITORING", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("MSFT", "2026-05-19", "r2", "news_classifier"),
        title="no score",
    )

    deltas = compute_score_deltas([
        (no_ticker, "DELIVERED", None, None),
        (no_score, "MONITORING", None, None),
    ])

    assert deltas == {}


# ---------------------------------------------------------------------------
# Agent-native score scales: Discovery composites are 0-100, News Classifier
# scores are confidences in [0.0, 1.0]. A shared ":.0f" collapses every
# classifier score to "0"/"1" and every delta to "+0"/"-0".
# ---------------------------------------------------------------------------

def test_news_classifier_score_and_delta_render_as_percentages(db):
    """A confidence-scaled signal renders as "76%" with a "+12pp" delta."""
    from signal_system.jobs.common import compute_score_deltas, render_digest

    earlier = Signal(
        ticker="AAPL", score=0.64, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 18, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-18", "news:a", "news_classifier"),
        title="AAPL: prior-day headline",
    )
    repository.insert_signal(earlier, routing_status="DELIVERED")

    later = Signal(
        ticker="AAPL", score=0.76, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-19", "news:b", "news_classifier"),
        title="AAPL: today's headline",
    )
    routed = [(later, "SUPPRESSED", None, "cap_exhausted")]

    deltas = compute_score_deltas(routed)
    assert deltas == {later.alert_id: "+12pp"}

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=6,
        routed_signals=routed,
        held_tickers={"AAPL"},
        score_deltas=deltas,
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert (
        "AAPL — 76% (+12pp) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD"
        in payload.body
    )


def test_news_classifier_negative_delta_renders_as_percentage_points(db):
    from signal_system.jobs.common import compute_score_deltas

    earlier = Signal(
        ticker="MSFT", score=0.88, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 18, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("MSFT", "2026-05-18", "news:a", "news_classifier"),
        title="MSFT: prior-day headline",
    )
    repository.insert_signal(earlier, routing_status="SUPPRESSED")

    later = Signal(
        ticker="MSFT", score=0.65, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("MSFT", "2026-05-19", "news:b", "news_classifier"),
        title="MSFT: today's headline",
    )

    assert compute_score_deltas([(later, "SUPPRESSED", None, "outscored")]) == {
        later.alert_id: "-23pp"
    }


def test_discovery_scale_score_and_delta_formatting_is_unchanged(db):
    """Guard the non-news-classifier branch: bare integers, no % or pp suffix."""
    from signal_system.jobs.common import compute_score_deltas, render_digest

    earlier = Signal(
        ticker="NVDA", score=62.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 17, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("NVDA", "2026-05-17", "discovery", "discovery_agent"),
        title="NVDA: Discovery score 62",
    )
    repository.insert_signal(earlier, routing_status="DELIVERED")

    later = Signal(
        ticker="NVDA", score=74.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("NVDA", "2026-05-19", "discovery", "discovery_agent"),
        title="NVDA: Discovery score 74",
    )
    routed = [(later, "SUPPRESSED", None, "cap_exhausted")]

    deltas = compute_score_deltas(routed)
    assert deltas == {later.alert_id: "+12"}

    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=routed,
        held_tickers={"NVDA"},
        score_deltas=deltas,
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert (
        "NVDA — 74 (+12) — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD"
        in payload.body
    )
    # No percentage/percentage-point suffixes leak onto a 0-100-scale line.
    # (Checked on the line itself — the word "Suppressed" contains "pp".)
    suppressed_line = next(
        line for line in payload.body.splitlines() if line.startswith("NVDA")
    )
    assert "%" not in suppressed_line
    assert "pp" not in suppressed_line


def test_render_digest_suppressed_line_surfaces_demotion(db):
    """An AR signal demoted then suppressed shows BOTH facts on its line."""
    from signal_system.jobs.common import render_digest

    demoted_and_cut = Signal(
        ticker="AAPL", score=81.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("AAPL", "2026-05-19", "discovery", "discovery_agent"),
        title="AAPL: Discovery score 81",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(demoted_and_cut, "SUPPRESSED", "ACTION_REQUIRED", "cap_exhausted")],
        held_tickers={"AAPL"},
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert (
        "AAPL — 81 — INFORMATIONAL (demoted from ACTION_REQUIRED) "
        "— cap_exhausted (INFO 3/3) — HELD"
    ) in payload.body


def test_render_digest_suppressed_line_omits_demotion_when_absent(db):
    """Never-demoted suppressed signals keep the original, unadorned line."""
    from signal_system.jobs.common import render_digest

    sig = Signal(
        ticker="CDE", score=61.0, severity="INFORMATIONAL", agent="discovery_agent",
        timestamp=datetime(2026, 5, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id("CDE", "2026-05-19", "discovery", "discovery_agent"),
        title="CDE: Discovery score 61",
    )
    payload = render_digest(
        job_name="discovery",
        scanned_tickers=23,
        routed_signals=[(sig, "SUPPRESSED", None, "outscored")],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    assert "CDE — 61 — INFORMATIONAL — outscored" in payload.body
    assert "demoted from" not in payload.body


# ---------------------------------------------------------------------------
# Deltas must be computed BEFORE persistence, so get_previous_score() cannot
# see this run's own rows. (get_previous_score() also excludes same-ET-day
# rows, but a run that straddles ET midnight stamps two dates — belt and
# braces, and the ordering is the invariant callers must not silently break.)
# ---------------------------------------------------------------------------

def _record_ordering_patches(module_path: str, status_counts: dict[str, int]):
    from signal_system.jobs.common import PersistenceSummary

    order: list[str] = []

    def record_deltas(routed):
        order.append("compute_score_deltas")
        return {}

    def record_persist(routed):
        order.append("persist_routed_signals")
        return PersistenceSummary(delivered_signals=[], status_counts=dict(status_counts))

    return order, (
        patch(f"{module_path}.compute_score_deltas", side_effect=record_deltas),
        patch(f"{module_path}.persist_routed_signals", side_effect=record_persist),
    )


def test_news_morning_computes_deltas_before_persisting(db):
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")
    order, (patch_deltas, patch_persist) = _record_ordering_patches(
        "signal_system.jobs.news_morning",
        {"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]), \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "outscored")]), \
         patch_deltas, patch_persist, \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message"):
        news_morning.run()

    assert order == ["compute_score_deltas", "persist_routed_signals"]


def test_discovery_computes_deltas_before_persisting(db):
    from signal_system.jobs import discovery

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="MSFT", severity="INFORMATIONAL", agent="discovery_agent", score=71.0)
    order, (patch_deltas, patch_persist) = _record_ordering_patches(
        "signal_system.jobs.discovery",
        {"DELIVERED": 0, "SUPPRESSED": 1, "MONITORING": 0},
    )

    with patch("signal_system.jobs.discovery._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.discovery.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.discovery.get_todays_universe", return_value=["MSFT"]), \
         patch("signal_system.jobs.discovery.get_core_holdings", return_value=["MSFT"]), \
         patch("signal_system.jobs.discovery.score_universe", return_value=[suppressed_signal]), \
         patch(
             "signal_system.jobs.discovery.route_signals",
             return_value=[(suppressed_signal, "SUPPRESSED", None, "cap_exhausted")],
         ), \
         patch_deltas, patch_persist, \
         patch("signal_system.jobs.discovery.telegram_sender.send_message"):
        discovery.run()

    assert order == ["compute_score_deltas", "persist_routed_signals"]


def test_news_morning_reads_core_holdings_once(db):
    """news-morning scans only core holdings, so `tickers` IS the HELD set."""
    from signal_system.jobs import news_morning

    fixed_now = datetime(2026, 5, 19, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    suppressed_signal = _sig(ticker="AAPL", severity="INFORMATIONAL")

    with patch("signal_system.jobs.news_morning.heartbeat.heartbeat", _noop_heartbeat), \
         patch("signal_system.jobs.news_morning._now_et", return_value=fixed_now), \
         patch("signal_system.jobs.news_morning.get_core_holdings", return_value=["AAPL"]) as mock_holdings, \
         patch("signal_system.jobs.news_morning.get_position_weights", return_value={"AAPL": 10.0}), \
         patch("signal_system.jobs.news_morning.repository.get_latest_successful_run_date", return_value=date(2026, 5, 16)), \
         patch("signal_system.jobs.news_morning.load_thesis", return_value=(object(), "thesis-hash")), \
         patch("signal_system.jobs.news_morning.fetch_company_news", return_value=[_news_item("AAPL event", fixed_now)]), \
         patch("signal_system.jobs.news_morning.classify_headlines", return_value=[suppressed_signal]), \
         patch("signal_system.jobs.news_morning.route_signals", return_value=[(suppressed_signal, "SUPPRESSED", None, "outscored")]), \
         patch("signal_system.jobs.news_morning.telegram_sender.send_message") as mock_send:
        news_morning.run()

    assert mock_holdings.call_count == 1
    # HELD still tagged from that single read
    assert "AAPL — 75% (NEW) — INFORMATIONAL — outscored — HELD" in mock_send.call_args.args[0]


def test_news_classifier_demoted_delivery_names_winner_confidence(db):
    """The motivating news-morning case, end-to-end through the real router.

    A batch is never mixed across agents, but news-morning can classify two
    held tickers as ACTION_REQUIRED in one run. One wins the single AR slot;
    the other is delivered demoted, and its header names the winner's score.
    That score is a confidence in [0.0, 1.0], so a bare ":.0f" would render
    the winner's 0.91 as "1" in the very digest this feature is about.
    """
    from signal_system.jobs.common import render_digest
    from signal_system.router import route_signals

    winner = _sig(ticker="NVDA", severity="ACTION_REQUIRED", score=0.91)
    runner_up = _sig(ticker="AAPL", severity="ACTION_REQUIRED", score=0.88)
    assert winner.agent == "news_classifier"  # _sig()'s default; the point of this test

    routed = route_signals([winner, runner_up])

    # AR cap is 1: NVDA takes it, AAPL demotes into the (empty) INFO pool.
    statuses = {t[0].ticker: (t[1], t[0].severity, t[2]) for t in routed}
    assert statuses["NVDA"] == ("DELIVERED", "ACTION_REQUIRED", None)
    assert statuses["AAPL"] == ("DELIVERED", "INFORMATIONAL", "ACTION_REQUIRED")

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=6,
        routed_signals=routed,
        held_tickers={"NVDA", "AAPL"},
        score_deltas={},
        status_counts={"DELIVERED": 2, "SUPPRESSED": 0, "MONITORING": 0},
    )

    assert (
        "AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED, outscored by NVDA 91%) — HELD"
        in payload.body
    )
    # The pre-fix rendering must not survive anywhere in the digest.
    assert "outscored by NVDA 1)" not in payload.body
