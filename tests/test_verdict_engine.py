"""Table-driven tests for verdict_engine.

Covers all 9 matrix cells, all 3 overlays (thesis-break, no-chasing, wash-sale),
not-held BUY/PASS logic, and confidence monotonicity invariant.
"""
import pytest
from signal_system.advisor.verdict_engine import (
    classify_trend,
    classify_news,
    compute_confidence,
    is_extended,
    compute_verdict,
    NEWS_BULLISH_THRESHOLD,
    NEWS_BEARISH_THRESHOLD,
    CONFIDENCE_ALIGNED_STRONG,
    CONFIDENCE_ONE_NEUTRAL,
    CONFIDENCE_CONFLICTING,
)


# --- classify_trend ---

@pytest.mark.parametrize("price,sma50,sma200,expected", [
    (110, 100, 90,  "bullish"),  # price > sma50 > sma200
    (80,  90,  100, "bearish"),  # price < sma50 < sma200
    (95,  90,  100, "neutral"),  # price > sma50 but sma50 < sma200
    (95,  100, 90,  "neutral"),  # price < sma50 but sma50 > sma200
    (100, 100, 100, "neutral"),  # all equal
])
def test_classify_trend(price, sma50, sma200, expected):
    assert classify_trend(price, sma50, sma200) == expected


# --- classify_news ---

@pytest.mark.parametrize("news_net,expected", [
    (NEWS_BULLISH_THRESHOLD + 0.01, "bullish"),
    (NEWS_BEARISH_THRESHOLD - 0.01, "bearish"),
    (0.0,                           "neutral"),
    (NEWS_BULLISH_THRESHOLD,        "neutral"),  # threshold is exclusive
    (NEWS_BEARISH_THRESHOLD,        "neutral"),  # threshold is exclusive
])
def test_classify_news(news_net, expected):
    assert classify_news(news_net) == expected


# --- compute_confidence ---

@pytest.mark.parametrize("mom,news,expected", [
    ("bullish", "bullish", CONFIDENCE_ALIGNED_STRONG),
    ("bearish", "bearish", CONFIDENCE_ALIGNED_STRONG),
    ("neutral", "bullish", CONFIDENCE_ONE_NEUTRAL),
    ("bullish", "neutral", CONFIDENCE_ONE_NEUTRAL),
    ("neutral", "neutral", CONFIDENCE_ONE_NEUTRAL),
    ("bullish", "bearish", CONFIDENCE_CONFLICTING),
    ("bearish", "bullish", CONFIDENCE_CONFLICTING),
])
def test_compute_confidence(mom, news, expected):
    assert compute_confidence(mom, news) == expected


# --- is_extended ---

def test_is_extended_true_when_price_near_top():
    # price=104 in range 80-105: (104-80)/(105-80) = 24/25 = 0.96 >= 0.90
    assert is_extended(price=104.0, close_high_20d=105.0, close_low_20d=80.0) is True


def test_is_extended_false_when_price_low():
    assert is_extended(price=80.0, close_high_20d=105.0, close_low_20d=80.0) is False


def test_is_extended_false_when_zero_range():
    assert is_extended(price=100.0, close_high_20d=100.0, close_low_20d=100.0) is False


# --- All 9 matrix cells (held=True) ---

_BULL_NEWS = NEWS_BULLISH_THRESHOLD + 0.01
_BEAR_NEWS = NEWS_BEARISH_THRESHOLD - 0.01

_BASE = dict(
    close_high_20d=120.0,  # above all test prices so the extension overlay doesn't fire here
    close_low_20d=85.0,
    cost_basis=90.0,
    held=True,
    thesis_break=False,
)


@pytest.mark.parametrize("price,sma50,sma200,news_net,expected", [
    # trend bullish (price=110>sma50=100>sma200=90)
    (110, 100, 90, _BULL_NEWS, "BUY"),
    (110, 100, 90, 0.0,        "HOLD"),
    (110, 100, 90, _BEAR_NEWS, "HOLD"),
    # trend neutral (price=95 — mixed SMAs)
    (95,  90,  100, _BULL_NEWS, "HOLD"),
    (95,  90,  100, 0.0,        "HOLD"),
    (95,  90,  100, _BEAR_NEWS, "SELL"),
    # trend bearish (price=70<sma50=90<sma200=100)
    (70,  90,  100, _BULL_NEWS, "HOLD"),
    (70,  90,  100, 0.0,        "SELL"),
    (70,  90,  100, _BEAR_NEWS, "SELL"),
])
def test_held_matrix_all_nine_cells(price, sma50, sma200, news_net, expected):
    result = compute_verdict(
        price=price, sma50=sma50, sma200=sma200, news_net=news_net, **_BASE
    )
    assert result.verdict == expected


# --- Overlay 1: thesis-break ---

def test_thesis_break_overrides_bullish_trend_to_sell():
    result = compute_verdict(
        price=110, sma50=100, sma200=90, news_net=_BULL_NEWS,
        **{**_BASE, "thesis_break": True}
    )
    assert result.verdict == "SELL"
    assert "thesis_break" in result.flags
    assert result.confidence == 0.90


# --- Overlay 2: no-chasing (BUY → HOLD when extended) ---

def test_no_chasing_buy_downgrades_to_hold_when_extended():
    # Trend bullish + news bullish → BUY normally.
    # price=104 in range 80-105 → extended (96%) → downgrades to HOLD
    result = compute_verdict(
        price=104.0, sma50=100.0, sma200=90.0, news_net=_BULL_NEWS,
        close_high_20d=105.0, close_low_20d=80.0, cost_basis=90.0,
        held=True, thesis_break=False,
    )
    assert result.verdict == "HOLD"
    assert "extended" in result.flags


def test_no_chasing_sell_is_not_affected_by_extension():
    # Trend neutral + news bearish → SELL; extension must not prevent SELL
    result = compute_verdict(
        price=104.0, sma50=90.0, sma200=100.0, news_net=_BEAR_NEWS,
        close_high_20d=105.0, close_low_20d=80.0, cost_basis=90.0,
        held=True, thesis_break=False,
    )
    assert result.verdict == "SELL"
    assert "extended" not in result.flags


# --- Overlay 3: wash-sale caution ---

def test_wash_sale_caution_flag_on_sell_at_loss():
    result = compute_verdict(
        price=70.0, sma50=90.0, sma200=100.0, news_net=0.0,
        close_high_20d=95.0, close_low_20d=68.0,
        cost_basis=95.0,  # price (70) < cost_basis (95) → at a loss
        held=True, thesis_break=False,
    )
    assert result.verdict == "SELL"
    assert "wash_sale_caution" in result.flags


def test_wash_sale_caution_not_added_when_at_profit():
    result = compute_verdict(
        price=70.0, sma50=90.0, sma200=100.0, news_net=0.0,
        close_high_20d=95.0, close_low_20d=68.0,
        cost_basis=50.0,  # price (70) > cost_basis (50) → profitable
        held=True, thesis_break=False,
    )
    assert result.verdict == "SELL"
    assert "wash_sale_caution" not in result.flags


# --- Not-held candidates ---

_NHELD = dict(
    close_high_20d=105.0, close_low_20d=85.0,
    cost_basis=None, held=False, thesis_break=False,
)


@pytest.mark.parametrize("price,sma50,sma200,news_net,expected", [
    (110, 100, 90, _BULL_NEWS, "BUY"),    # bullish trend + bullish news
    (110, 100, 90, 0.0,        "BUY"),    # bullish trend + neutral news
    (110, 100, 90, _BEAR_NEWS, "PASS"),   # bullish trend + bearish news
    (70,  90,  100, _BULL_NEWS, "PASS"),  # bearish trend (any news) → PASS
    (95,  90,  100, _BULL_NEWS, "PASS"),  # neutral trend → PASS
])
def test_not_held_buy_pass(price, sma50, sma200, news_net, expected):
    result = compute_verdict(
        price=price, sma50=sma50, sma200=sma200, news_net=news_net, **_NHELD
    )
    assert result.verdict == expected


# --- Confidence monotonicity ---

def test_confidence_ordering():
    assert CONFIDENCE_ALIGNED_STRONG > CONFIDENCE_ONE_NEUTRAL > CONFIDENCE_CONFLICTING > 0.0
