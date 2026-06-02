from unittest.mock import MagicMock

import pytest

from signal_system.advisor import rationale as rationale_mod


_THESIS = "ai_semi: AI capex cycle, positive on GPU demand, negative on bubble concerns."

_FACTORS = {
    "price": 40.0, "sma50": 38.0, "sma200": 35.0, "news_net": 0.05,
    "close_high_20d": 42.0, "close_low_20d": 36.0, "cost_basis": 38.10,
}


def _mock_client(text: str | None = "FCX HOLD: trend bullish.", exc: Exception | None = None):
    client = MagicMock()
    if exc:
        client.messages.parse.side_effect = exc
    else:
        response = MagicMock()
        response.parsed_output = MagicMock(rationale=text)
        response.usage = MagicMock(
            input_tokens=100, output_tokens=40,
            cache_read_input_tokens=80, cache_creation_input_tokens=0,
        )
        client.messages.parse.return_value = response
    return client


def test_generate_rationale_returns_claude_text(tmp_path, monkeypatch):
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    monkeypatch.setattr(rationale_mod, "_client", _mock_client("FCX HOLD: trend bullish."))

    text, source = rationale_mod.generate_rationale(
        ticker="FCX", verdict="HOLD", mom_axis="bullish", news_axis="neutral",
        factors=_FACTORS, flags=[], thesis_text=_THESIS,
    )
    assert text == "FCX HOLD: trend bullish."
    assert source == "claude"


def test_generate_rationale_falls_back_to_template_on_exception(tmp_path, monkeypatch):
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    monkeypatch.setattr(rationale_mod, "_client", _mock_client(exc=ConnectionError("API down")))

    text, source = rationale_mod.generate_rationale(
        ticker="FCX", verdict="HOLD", mom_axis="bullish", news_axis="neutral",
        factors=_FACTORS, flags=[], thesis_text=_THESIS,
    )
    assert source == "template"
    assert "FCX" in text


def test_template_includes_wash_sale_note_when_flagged(tmp_path, monkeypatch):
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    monkeypatch.setattr(rationale_mod, "_client", _mock_client(exc=RuntimeError("down")))

    text, source = rationale_mod.generate_rationale(
        ticker="FCX", verdict="SELL", mom_axis="bearish", news_axis="bearish",
        factors=_FACTORS, flags=["wash_sale_caution"], thesis_text=_THESIS,
    )
    assert "wash-sale" in text.lower()
    assert source == "template"


def test_template_includes_thesis_break_note(tmp_path, monkeypatch):
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    monkeypatch.setattr(rationale_mod, "_client", _mock_client(exc=RuntimeError("down")))

    text, source = rationale_mod.generate_rationale(
        ticker="FCX", verdict="SELL", mom_axis="bullish", news_axis="bearish",
        factors=_FACTORS, flags=["thesis_break"], thesis_text=_THESIS,
    )
    assert "thesis" in text.lower()
    assert source == "template"


def test_none_parsed_output_falls_back_to_template(tmp_path, monkeypatch):
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    client = MagicMock()
    response = MagicMock()
    response.parsed_output = None
    response.usage = MagicMock(input_tokens=50, output_tokens=0,
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
    client.messages.parse.return_value = response
    monkeypatch.setattr(rationale_mod, "_client", client)

    text, source = rationale_mod.generate_rationale(
        ticker="FCX", verdict="HOLD", mom_axis="bullish", news_axis="neutral",
        factors=_FACTORS, flags=[], thesis_text=_THESIS,
    )
    assert source == "template"
