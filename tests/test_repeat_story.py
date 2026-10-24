"""Plan 12c3 — cross-day repeat-story filter (pure text, no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from signal_system.classifier.news_classifier import is_repeat_story, story_tokens
from signal_system.models import Signal, compute_alert_id
from signal_system.state import repository

ET = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _sig(ticker: str, title: str) -> Signal:
    ts = datetime(2026, 10, 23, 9, 0, tzinfo=ET)
    return Signal(
        ticker=ticker,
        score=0.9,
        severity="INFORMATIONAL",
        agent="news_classifier",
        timestamp=ts,
        alert_id=compute_alert_id(ticker, ts.date().isoformat(), title[:16], "news_classifier"),
        title=title,
    )


def test_known_nvda_repeat_is_flagged():
    candidate = _sig(
        "NVDA",
        "ai_semi: Nvidia wins $35B RTX PRO 5500 order from Microsoft",
    )
    recent = ["ai_semi: Nvidia lands $35B RTX PRO 5500 order with Microsoft"]

    assert is_repeat_story(candidate, recent) is True


def test_known_amd_repeat_is_flagged():
    candidate = _sig(
        "AMD",
        "ai_semi: AMD joins the $1 Trillion Club as AI chip demand surges",
    )
    recent = ["ai_semi: AMD enters the $1 Trillion Club on AI chip optimism"]

    assert is_repeat_story(candidate, recent) is True


def test_micron_earnings_stories_are_not_repeats():
    candidate = _sig(
        "MU",
        "ai_semi: Micron Stock Jumps as Memory Prices Rally",
    )
    recent = [
        "ai_semi: Micron Posted a $54.23 Billion Quarter. Here Is What It Means"
    ]

    assert is_repeat_story(candidate, recent) is False


def test_defense_stories_are_not_repeats():
    candidate = _sig(
        "LMT",
        "defense: Lockheed Martin wins a new missile contract",
    )
    recent = ["defense: Lockheed Martin raises full-year guidance"]

    assert is_repeat_story(candidate, recent) is False


def test_short_titles_are_never_compared():
    candidate = _sig("NVDA", "ai_semi: Nvidia up")
    assert len(story_tokens(candidate.title, "NVDA")) < 4
    assert is_repeat_story(candidate, ["ai_semi: Nvidia down"]) is False


def test_repeat_filter_does_not_match_same_alert_on_rerun(db):
    """A failed-send rerun must not classify its own PENDING copy as a repeat."""
    candidate = _sig(
        "NVDA",
        "ai_semi: Nvidia wins $35B RTX PRO 5500 order from Microsoft",
    )
    repository.insert_signal(candidate, routing_status="PENDING")

    recent = repository.list_recent_delivered_news(
        "NVDA", candidate.timestamp.date() - timedelta(days=7)
    )
    assert [row["alert_id"] for row in recent] == [candidate.alert_id]

    comparison = [
        row["title"]
        for row in recent
        if row["alert_id"] not in {candidate.alert_id}
    ]
    assert is_repeat_story(candidate, comparison) is False


def test_repeat_filter_ignores_non_news_agents(db):
    candidate = _sig(
        "NVDA",
        "ai_semi: Nvidia wins $35B RTX PRO 5500 order from Microsoft",
    )
    sentinel = Signal(
        ticker=candidate.ticker,
        score=100.0,
        severity="INFORMATIONAL",
        agent="exit_sentinel",
        timestamp=candidate.timestamp,
        alert_id=candidate.alert_id,
        title=candidate.title,
    )
    repository.insert_signal(sentinel, routing_status="PENDING")

    recent = repository.list_recent_delivered_news(
        "NVDA", candidate.timestamp.date() - timedelta(days=7)
    )
    assert recent == []


def test_digest_renders_repeat_monitoring_bucket():
    from signal_system.jobs.common import render_digest

    payload = render_digest(
        job_name="news-morning",
        scanned_tickers=3,
        routed_signals=[],
        held_tickers=set(),
        score_deltas={},
        status_counts={"DELIVERED": 0, "SUPPRESSED": 0, "MONITORING": 2},
        monitoring_breakdown={"repeat": 2},
    )
    assert "Monitoring: 2 (repeat 2)" in payload.body


def test_macro_candidates_are_out_of_scope():
    candidate = _sig("X", "macro: Oil prices spike on supply fears")
    candidate = Signal(
        ticker=None,
        score=candidate.score,
        severity=candidate.severity,
        agent=candidate.agent,
        timestamp=candidate.timestamp,
        alert_id=candidate.alert_id,
        title=candidate.title,
    )
    assert is_repeat_story(candidate, ["macro: Oil prices spike on supply fears"]) is False
