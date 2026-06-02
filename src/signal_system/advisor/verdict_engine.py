"""Deterministic two-axis verdict engine for the Advisor Agent.

Axis 1 (Trend):  price vs 50-day / 200-day SMA -> bullish / neutral / bearish
Axis 2 (News):   net (direction x confidence) from recent signals -> bullish / neutral / bearish
Matrix + overlays -> BUY / HOLD / SELL / PASS

All threshold constants are initial guesses -- tune at quarterly IC review.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Axis = Literal["bullish", "neutral", "bearish"]
VerdictStr = Literal["BUY", "HOLD", "SELL", "PASS", "NO_DATA"]

# --- Tunable constants (quarterly review) ---
SMA_SHORT_DAYS: int = 50
SMA_LONG_DAYS: int = 200
HISTORY_DAYS: int = 400        # calendar days -> ~285 trading days; enough for 200d SMA
NEWS_LOOKBACK_DAYS: int = 14

NEWS_BULLISH_THRESHOLD: float = 0.15   # net score above this -> bullish
NEWS_BEARISH_THRESHOLD: float = -0.15  # net score below this -> bearish

EXTENDED_RANGE_FRAC: float = 0.90     # price at >=90% of 20d close range -> extended

CONFIDENCE_ALIGNED_STRONG: float = 0.80  # both axes agree, non-neutral
CONFIDENCE_ONE_NEUTRAL: float = 0.45     # one axis neutral
CONFIDENCE_CONFLICTING: float = 0.20    # axes disagree (bullish vs bearish)

_HELD_MATRIX: dict[tuple[Axis, Axis], VerdictStr] = {
    ("bullish", "bullish"):  "BUY",
    ("bullish", "neutral"):  "HOLD",
    ("bullish", "bearish"):  "HOLD",
    ("neutral", "bullish"):  "HOLD",
    ("neutral", "neutral"):  "HOLD",
    ("neutral", "bearish"):  "SELL",
    ("bearish", "bullish"):  "HOLD",
    ("bearish", "neutral"):  "SELL",
    ("bearish", "bearish"):  "SELL",
}


@dataclass(frozen=True, slots=True)
class VerdictResult:
    verdict: VerdictStr
    confidence: float
    mom_axis: Axis
    news_axis: Axis
    factors: dict
    flags: list[str]


def classify_trend(price: float, sma50: float, sma200: float) -> Axis:
    if price > sma50 > sma200:
        return "bullish"
    if price < sma50 < sma200:
        return "bearish"
    return "neutral"


def classify_news(news_net: float) -> Axis:
    if news_net > NEWS_BULLISH_THRESHOLD:
        return "bullish"
    if news_net < NEWS_BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def compute_confidence(mom_axis: Axis, news_axis: Axis) -> float:
    if mom_axis == news_axis and mom_axis != "neutral":
        return CONFIDENCE_ALIGNED_STRONG
    if mom_axis == "neutral" or news_axis == "neutral":
        return CONFIDENCE_ONE_NEUTRAL
    return CONFIDENCE_CONFLICTING  # one bullish, one bearish


def is_extended(price: float, close_high_20d: float, close_low_20d: float) -> bool:
    """True when price is at >=90% of its 20-day closing range (don't chase highs)."""
    range_20d = close_high_20d - close_low_20d
    if range_20d <= 0:
        return False
    return (price - close_low_20d) / range_20d >= EXTENDED_RANGE_FRAC


def compute_verdict(
    *,
    price: float,
    sma50: float,
    sma200: float,
    news_net: float,
    close_high_20d: float,
    close_low_20d: float,
    cost_basis: float | None,
    held: bool,
    thesis_break: bool = False,
) -> VerdictResult:
    """Apply the two-axis matrix and overlays to produce a verdict.

    Args:
        price: Current price.
        sma50: 50-day SMA of closing prices.
        sma200: 200-day SMA of closing prices.
        news_net: Net (direction x confidence) of recent news signals. Positive = bullish.
        close_high_20d: Highest closing price in last 20 trading days.
        close_low_20d: Lowest closing price in last 20 trading days.
        cost_basis: Average cost per share; None for not-held candidates.
        held: True = evaluate as held position; False = evaluate as new-buy candidate.
        thesis_break: True if a high-confidence negative news hit was detected.
    """
    mom_axis = classify_trend(price, sma50, sma200)
    news_axis = classify_news(news_net)
    confidence = compute_confidence(mom_axis, news_axis)
    flags: list[str] = []

    factors = {
        "price": price,
        "sma50": round(sma50, 4),
        "sma200": round(sma200, 4),
        "news_net": round(news_net, 4),
        "close_high_20d": close_high_20d,
        "close_low_20d": close_low_20d,
        "cost_basis": cost_basis,
    }

    # Overlay 1: thesis-break overrides all -- asymmetric exit discipline
    if thesis_break:
        flags.append("thesis_break")
        return VerdictResult(
            verdict="SELL",
            confidence=0.90,
            mom_axis=mom_axis,
            news_axis=news_axis,
            factors=factors,
            flags=flags,
        )

    if held:
        verdict: VerdictStr = _HELD_MATRIX[(mom_axis, news_axis)]

        # Overlay 2: no-chasing -- BUY (add) -> HOLD when position is extended
        if verdict == "BUY" and is_extended(price, close_high_20d, close_low_20d):
            verdict = "HOLD"
            flags.append("extended")

        # Overlay 3: wash-sale caution flag on SELL at a loss
        if verdict == "SELL" and cost_basis is not None and price < cost_basis:
            flags.append("wash_sale_caution")
    else:
        # Not-held: only trend-bullish + news not-bearish -> BUY; everything else -> PASS
        verdict = "BUY" if mom_axis == "bullish" and news_axis in ("bullish", "neutral") else "PASS"

    return VerdictResult(
        verdict=verdict,
        confidence=confidence,
        mom_axis=mom_axis,
        news_axis=news_axis,
        factors=factors,
        flags=flags,
    )
