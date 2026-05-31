"""Tests for yahoo_client — batch historical OHLCV fetcher."""
from unittest.mock import patch

import pandas as pd


def test_fetch_history_returns_dict_of_dataframes():
    """fetch_history returns {ticker: DataFrame} with expected columns."""
    from signal_system.data.yahoo_client import fetch_history

    dates = pd.date_range("2026-05-01", periods=20, freq="B")
    mock_df = pd.DataFrame(
        {
            ("AAPL", "Close"): range(150, 170),
            ("AAPL", "High"): range(155, 175),
            ("AAPL", "Low"): range(145, 165),
            ("AAPL", "Open"): range(148, 168),
            ("AAPL", "Volume"): [1000000] * 20,
            ("MSFT", "Close"): range(400, 420),
            ("MSFT", "High"): range(405, 425),
            ("MSFT", "Low"): range(395, 415),
            ("MSFT", "Open"): range(398, 418),
            ("MSFT", "Volume"): [2000000] * 20,
        },
        index=dates,
    )
    # pandas 3.x auto-creates MultiIndex from tuple keys — from_tuples is
    # needed only when the constructor fails to infer levels correctly.
    mock_df.columns = pd.MultiIndex.from_tuples(mock_df.columns)

    with patch("signal_system.data.yahoo_client.yf.download", return_value=mock_df):
        result = fetch_history(["AAPL", "MSFT"], days=25)

    assert set(result.keys()) == {"AAPL", "MSFT"}
    assert len(result["AAPL"]) == 20
    assert list(result["AAPL"].columns) == ["Close", "High", "Low"]


def test_fetch_history_empty_ticker_skipped():
    """Tickers with no data in the response are excluded from the result."""
    from signal_system.data.yahoo_client import fetch_history

    dates = pd.date_range("2026-05-01", periods=20, freq="B")
    mock_df = pd.DataFrame(
        {
            ("AAPL", "Close"): range(150, 170),
            ("AAPL", "High"): range(155, 175),
            ("AAPL", "Low"): range(145, 165),
            ("AAPL", "Open"): range(148, 168),
            ("AAPL", "Volume"): [1000000] * 20,
        },
        index=dates,
    )
    mock_df.columns = pd.MultiIndex.from_tuples(mock_df.columns)

    with patch("signal_system.data.yahoo_client.yf.download", return_value=mock_df):
        result = fetch_history(["AAPL", "BADTICKER"], days=25)

    assert "AAPL" in result
    assert "BADTICKER" not in result


def test_fetch_history_empty_tickers_returns_empty():
    """Empty ticker list returns empty dict without calling yfinance."""
    from signal_system.data.yahoo_client import fetch_history

    with patch("signal_system.data.yahoo_client.yf.download") as mock_dl:
        result = fetch_history([], days=25)

    assert result == {}
    mock_dl.assert_not_called()


def test_fetch_history_download_exception_returns_empty():
    """If yfinance raises, fetch_history returns empty dict (never crashes the job)."""
    from signal_system.data.yahoo_client import fetch_history

    with patch("signal_system.data.yahoo_client.yf.download", side_effect=Exception("network")):
        result = fetch_history(["AAPL"], days=25)

    assert result == {}
