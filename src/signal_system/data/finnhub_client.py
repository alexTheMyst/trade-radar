import finnhub
from signal_system import config

_client: finnhub.Client | None = None


def _get_client() -> finnhub.Client:
    """
    Lazily initialize and return the Finnhub client.

    The client is created once and reused for all subsequent calls.
    """
    global _client
    if _client is None:
        _client = finnhub.Client(api_key=config.FINNHUB_API_KEY)
    return _client


def fetch_spy_close() -> float:
    """
    Fetch the current close price (or last quote) for SPY (S&P 500 proxy).

    Uses SPY instead of ^GSPC (which is not available on Finnhub free tier).
    SPY (SPDR S&P 500 ETF) is a standard equity symbol that works on the free tier.

    Returns:
        float: The close price of SPY.

    Raises:
        ValueError: If the response is empty, "c" field is missing, or close price is
                   0 or negative (indicating bad data).
    """
    response = _get_client().quote("SPY")
    close = response.get("c")
    if not close or close <= 0:
        raise ValueError(f"Invalid SPY quote response from Finnhub: {response!r}")
    return float(close)
