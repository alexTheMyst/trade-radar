"""Tests for the position-weight severity amplifier."""


def test_high_weight_lowers_threshold():
    """A 25% position (5x median) has thresholds shifted down by 20 (clamped at 4x)."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"SPY": 25.0, "AAPL": 12.0, "NVDA": 4.0, "KO": 1.0}
    # median of [25, 12, 4, 1] = 8.0
    # SPY ratio = 25/8 = 3.125 → shift = 10*log2(3.125) = 16.4
    # AR threshold = 80 - 16.4 = 63.6
    result = adjusted_severity(
        score=65.0,
        ticker="SPY",
        weights=weights,
        base_thresholds=(80.0, 60.0),
    )
    assert result == "ACTION_REQUIRED"


def test_low_weight_raises_threshold():
    """A 1% position (well below median) has thresholds shifted up."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"SPY": 25.0, "AAPL": 12.0, "NVDA": 4.0, "KO": 1.0}
    # KO ratio = 1/8 = 0.125 → clamped to 0.25 → shift = 10*log2(0.25) = -20
    # AR threshold = 80 - (-20) = 100
    # INFO threshold = 60 - (-20) = 80
    result = adjusted_severity(
        score=75.0,
        ticker="KO",
        weights=weights,
        base_thresholds=(80.0, 60.0),
    )
    assert result == "MONITORING"


def test_median_weight_no_shift():
    """A position at exactly the median gets no threshold shift."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"A": 10.0, "B": 10.0, "C": 10.0}
    # median = 10, ratio = 1.0, shift = 10*log2(1) = 0
    result = adjusted_severity(
        score=79.0,
        ticker="A",
        weights=weights,
        base_thresholds=(80.0, 60.0),
    )
    assert result == "INFORMATIONAL"


def test_unknown_ticker_no_shift():
    """A ticker not in weights dict gets base thresholds (no shift)."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"SPY": 25.0, "AAPL": 12.0}
    result = adjusted_severity(
        score=79.0,
        ticker="UNKNOWN",
        weights=weights,
        base_thresholds=(80.0, 60.0),
    )
    assert result == "INFORMATIONAL"


def test_empty_weights_no_shift():
    """Empty weights dict means no shift — base thresholds apply."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    result = adjusted_severity(
        score=85.0,
        ticker="AAPL",
        weights={},
        base_thresholds=(80.0, 60.0),
    )
    assert result == "ACTION_REQUIRED"


def test_zero_weight_gets_max_penalty():
    """A ticker with weight_pct=0 gets the maximum upward threshold shift."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"SPY": 25.0, "WATCHLIST": 0.0}
    # ratio = 0/12.5 → clamped to 0.25 → shift = 10*log2(0.25) = -20
    # AR threshold = 80+20 = 100, INFO threshold = 60+20 = 80
    result = adjusted_severity(
        score=79.0,
        ticker="WATCHLIST",
        weights=weights,
        base_thresholds=(80.0, 60.0),
    )
    assert result == "MONITORING"


def test_amplifier_for_news_classifier_thresholds():
    """Works correctly with the news classifier's base thresholds (85/60 on 0-100 scale)."""
    from signal_system.scoring.weight_amplifier import adjusted_severity

    weights = {"SPY": 25.0, "AAPL": 12.0, "NVDA": 4.0, "KO": 1.0}
    # SPY: shift ~16.4, AR threshold = 85 - 16.4 = 68.6
    result = adjusted_severity(
        score=70.0,
        ticker="SPY",
        weights=weights,
        base_thresholds=(85.0, 60.0),
    )
    assert result == "ACTION_REQUIRED"
