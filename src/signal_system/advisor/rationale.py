"""Claude-powered rationale generator for advisor verdicts.

Never raises -- falls back to a templated rationale on any Claude failure.
The verdict itself is deterministic (verdict_engine); Claude writes cosmetics only.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from signal_system import config
from signal_system.state import repository

log = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


class _RationaleOutput(BaseModel):
    rationale: str


def _build_system_prompt(thesis_text: str) -> str:
    return (
        "You are a financial decision-support assistant. "
        "Given a stock's trend and news context, write a concise 2-3 sentence rationale "
        "for the given verdict. Be factual and neutral. Reference only the specific "
        "factors provided. Do not recommend dollar amounts or trade sizes.\n\n"
        f"<thesis>\n{thesis_text}\n</thesis>"
    )


def generate_rationale(
    *,
    ticker: str,
    verdict: str,
    mom_axis: str,
    news_axis: str,
    factors: dict,
    flags: list[str],
    thesis_text: str,
    job: str = "advisor",
) -> tuple[str, str]:
    """Return (rationale_text, source) where source is 'claude' or 'template'.

    Never raises.
    """
    system_prompt = _build_system_prompt(thesis_text)
    price = factors.get("price") or 0.0
    sma50 = factors.get("sma50") or 0.0
    sma200 = factors.get("sma200") or 0.0
    news_net = factors.get("news_net") or 0.0

    user_content = (
        f"Ticker: {ticker}\n"
        f"Verdict: {verdict}\n"
        f"Trend axis: {mom_axis} (price {price:.2f} vs 50d SMA {sma50:.2f} / 200d SMA {sma200:.2f})\n"
        f"News axis: {news_axis} (net signal score {news_net:.3f} over last 14 days)\n"
        f"Flags: {', '.join(flags) if flags else 'none'}"
    )

    try:
        response = _get_client().messages.parse(
            model=config.ANTHROPIC_MODEL,
            max_tokens=256,
            temperature=0.0,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            output_format=_RationaleOutput,
        )
        repository.insert_llm_call(
            job=job,
            model_version=config.ANTHROPIC_MODEL,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        if response.parsed_output is None:
            raise ValueError("messages.parse returned None parsed_output")
        return response.parsed_output.rationale, "claude"

    except Exception as exc:
        log.warning("Rationale generation failed for %s (%s): %s", ticker, verdict, exc)
        return _template_rationale(ticker, verdict, mom_axis, news_axis, flags), "template"


def _template_rationale(
    ticker: str, verdict: str, mom_axis: str, news_axis: str, flags: list[str]
) -> str:
    notes = ""
    if "thesis_break" in flags:
        notes += " High-confidence thesis-negative signal triggered exit override."
    if "extended" in flags:
        notes += " Position is extended near 20-day highs -- not chasing."
    if "wash_sale_caution" in flags:
        notes += " Verify 30-day wash-sale window across all accounts before executing."
    if "no_data" in flags:
        return f"{ticker}: insufficient price history -- manual review required."
    return (
        f"{ticker} {verdict}: trend {mom_axis}, news {news_axis}.{notes} "
        f"(Template -- Claude unavailable.)"
    )
