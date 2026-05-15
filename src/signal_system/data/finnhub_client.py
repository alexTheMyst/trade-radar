import finnhub
from signal_system import config

_client: finnhub.Client | None = None


def _get_client() -> finnhub.Client:
    global _client
    if _client is None:
        _client = finnhub.Client(api_key=config.FINNHUB_API_KEY)
    return _client


def fetch_spy_close() -> float:
    """Return SPY close price; raises ValueError on missing or non-positive data."""
    response = _get_client().quote("SPY")
    close = response.get("c")
    if close is None or close <= 0:
        raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
    return float(close)
