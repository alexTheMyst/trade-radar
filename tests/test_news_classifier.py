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


# ---------------------------------------------------------------------------
# _fix_encoding — mojibake repair and typographic normalisation
# ---------------------------------------------------------------------------

def test_fix_encoding_repairs_cp1252_mojibake():
    """U+2019 RIGHT SINGLE QUOTATION MARK mangled as cp1252 → repaired + mapped to ASCII."""
    from signal_system.classifier.news_classifier import _fix_encoding

    # Simulate the mojibake: encode U+2019 as UTF-8 (b'\xe2\x80\x99'),
    # then pretend those bytes were decoded as cp1252 character-by-character.
    mojibake = "\u2019".encode("utf-8").decode("cp1252")  # produces â€™
    assert _fix_encoding(mojibake) == "'"


def test_fix_encoding_repairs_left_double_quote_mojibake():
    """U+201C LEFT DOUBLE QUOTATION MARK mojibake is repaired and mapped to ASCII "."""
    from signal_system.classifier.news_classifier import _fix_encoding

    mojibake = "\u201c".encode("utf-8").decode("cp1252")
    assert _fix_encoding(mojibake) == '"'


def test_fix_encoding_normalises_clean_unicode_typographics():
    """Already-correct Unicode typographic chars are mapped to ASCII even without mojibake."""
    from signal_system.classifier.news_classifier import _fix_encoding

    assert _fix_encoding("Nvidia\u2019s results") == "Nvidia's results"
    assert _fix_encoding("BofA\u2019s forecast") == "BofA's forecast"
    assert _fix_encoding("revenue\u2014record high") == "revenue-record high"
    assert _fix_encoding("growth\u2026") == "growth..."


def test_fix_encoding_leaves_normal_ascii_unchanged():
    """Plain ASCII strings pass through without modification."""
    from signal_system.classifier.news_classifier import _fix_encoding

    text = "Apple reports Q4 earnings; beats estimates by 3%"
    assert _fix_encoding(text) == text


def test_fix_encoding_leaves_non_cp1252_unicode_unchanged():
    """Strings with codepoints outside cp1252 range are not corrupted."""
    from signal_system.classifier.news_classifier import _fix_encoding

    text = "Toyota \u30c8\u30e8\u30bf Q3 results"  # Japanese katakana — outside cp1252
    assert _fix_encoding(text) == text


def test_sanitize_headline_repairs_mojibake_end_to_end():
    """_sanitize_headline repairs mojibake before wrapping in <headline> tags."""
    from signal_system.classifier.news_classifier import _sanitize_headline

    mojibake = "Nvidia\u2019s".encode("utf-8").decode("cp1252") + " $5T market cap"
    result = _sanitize_headline(mojibake)
    assert result == "<headline>Nvidia's $5T market cap</headline>"
    assert "\ufffd" not in result  # no replacement characters
