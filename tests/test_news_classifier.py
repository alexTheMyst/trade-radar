"""Unit tests for the news classifier — focused on Signal construction details.

These tests mock the Anthropic call and Finnhub quote fetch so they never touch
the network. They verify that routable signals carry a price-at-signal snapshot
(required for outcome backfill / IC measurement per CLAUDE.md).

signal_system imports happen inside test bodies so conftest.py's env setup runs
first (matches the convention in test_job_orchestration.py).
"""

from __future__ import annotations

from types import SimpleNamespace


def _usage():
    return SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _patch_common(monkeypatch, nc):
    """Stub out the system-prompt build and telemetry write."""
    monkeypatch.setattr(nc, "_build_system_prompt", lambda thesis: "SYS")
    monkeypatch.setattr(nc.repository, "insert_llm_call", lambda **kwargs: None)


def test_routable_signal_carries_price_snapshot(monkeypatch):
    from signal_system.classifier import news_classifier as nc

    _patch_common(monkeypatch, nc)
    result = nc.ClassificationResult(
        pillar_name="ai_semi", confidence=0.9, direction="positive", rationale="x"
    )
    monkeypatch.setattr(nc, "_call_with_retry", lambda h, s: (result, _usage()))
    monkeypatch.setattr(nc, "fetch_quotes", lambda tickers: {"NVDA": {"c": 123.45}})

    signals = nc.classify_headlines(
        "NVDA",
        [{"headline": "NVDA soars on earnings"}],
        thesis=object(),
        thesis_version_hash="h",
    )

    assert len(signals) == 1
    assert signals[0].severity == "ACTION_REQUIRED"
    assert signals[0].signal_price_snapshot == 123.45


def test_no_quote_call_for_offthesis_headlines(monkeypatch):
    from signal_system.classifier import news_classifier as nc

    _patch_common(monkeypatch, nc)
    result = nc.ClassificationResult(
        pillar_name=None, confidence=0.2, direction="neutral", rationale="off"
    )
    monkeypatch.setattr(nc, "_call_with_retry", lambda h, s: (result, _usage()))

    def _boom(tickers):
        raise AssertionError("fetch_quotes must not be called when nothing is routable")

    monkeypatch.setattr(nc, "fetch_quotes", _boom)

    signals = nc.classify_headlines(
        "NVDA",
        [{"headline": "unrelated headline"}],
        thesis=object(),
        thesis_version_hash="h",
    )

    assert signals == []


def test_missing_quote_still_emits_signal_without_snapshot(monkeypatch):
    from signal_system.classifier import news_classifier as nc

    _patch_common(monkeypatch, nc)
    result = nc.ClassificationResult(
        pillar_name="ai_semi", confidence=0.9, direction="positive", rationale="x"
    )
    monkeypatch.setattr(nc, "_call_with_retry", lambda h, s: (result, _usage()))
    monkeypatch.setattr(nc, "fetch_quotes", lambda tickers: {"NVDA": None})

    signals = nc.classify_headlines(
        "NVDA",
        [{"headline": "NVDA soars on earnings"}],
        thesis=object(),
        thesis_version_hash="h",
    )

    assert len(signals) == 1
    assert signals[0].signal_price_snapshot is None
