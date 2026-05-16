"""
Smoke tests for signal-system.

All external I/O (Finnhub, SMTP, healthchecks.io) is mocked.
"""

import smtplib
import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from signal_system.state import repository
from signal_system.jobs import daily_close


# ---------------------------------------------------------------------------
# Phase 2 helpers
# ---------------------------------------------------------------------------

def _make_finnhub_exc(status_code: int):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = {"error": f"http {status_code}"}
    from finnhub.exceptions import FinnhubAPIException
    return FinnhubAPIException(r)


def test_init_db_creates_tables(tmp_path, monkeypatch):
    """init_db() must create both 'signals' and 'runs' tables."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    conn = sqlite3.connect(tmp_path / "test.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "signals" in tables
    assert "runs" in tables



def test_signal_is_frozen(tmp_path, monkeypatch):
    """Signal must be immutable — assigning to any field raises FrozenInstanceError."""
    import dataclasses
    from datetime import datetime, timezone
    from signal_system.models import Signal, compute_alert_id

    now = datetime.now(timezone.utc)
    alert_id = compute_alert_id("AAPL", "2026-05-15", "r", "news")
    signal = Signal(
        ticker="AAPL",
        score=0.85,
        severity="INFORMATIONAL",
        agent="news",
        timestamp=now,
        alert_id=alert_id,
        title="Test signal",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        signal.score = 0.5  # type: ignore[misc]


def test_compute_alert_id_deterministic():
    """compute_alert_id must be deterministic and SHA-256 based."""
    from signal_system.models import compute_alert_id

    expected = "7c35b5226a16a95fc5004a595e16e853bdbe762cbe0e16a7aaacf6af1a249be9"
    result = compute_alert_id("AAPL", "2026-05-15", "r", "news")
    assert result == expected, f"Got {result}"
    assert result == compute_alert_id("AAPL", "2026-05-15", "r", "news"), "Not deterministic"

    # Changing any arg must change the digest
    assert compute_alert_id("MSFT", "2026-05-15", "r", "news") != result
    assert compute_alert_id("AAPL", "2026-05-16", "r", "news") != result
    assert compute_alert_id("AAPL", "2026-05-15", "r2", "news") != result
    assert compute_alert_id("AAPL", "2026-05-15", "r", "disc") != result

    # None ticker must normalize to '_' without raising
    none_result = compute_alert_id(None, "2026-05-15", "r", "news")
    assert isinstance(none_result, str) and len(none_result) == 64


def test_daily_close_smoke(tmp_path, monkeypatch):
    """daily_close.run() must write a DAILY_CLOSE signal row with score == SPY close price."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    with (
        patch(
            "signal_system.data.finnhub_client.fetch_spy_close",
            return_value=591.42,
        ),
        patch("signal_system.delivery.email_sender.send_email"),
        patch(
            "httpx.post",
            return_value=MagicMock(raise_for_status=MagicMock()),
        ),
    ):
        daily_close.run()

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute(
        "SELECT ticker, score FROM signals WHERE agent='DAILY_CLOSE'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "SPY"
    assert abs(row[1] - 591.42) < 0.01


def test_daily_close_finnhub_failure(tmp_path, monkeypatch):
    """When fetch_spy_close raises, /fail ping must fire and run marked 'failed'."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    mock_post = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))

    with patch("signal_system.data.finnhub_client.fetch_spy_close", side_effect=ValueError("API down")), \
         patch("httpx.post", mock_post):
        with pytest.raises(ValueError, match="API down"):
            daily_close.run()

    # Confirm /fail ping was sent
    call_urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert any(url.endswith("/fail") for url in call_urls), f"Expected /fail ping, got: {call_urls}"

    # Confirm run is marked failed in DB
    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute("SELECT status FROM runs").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "failed"


def test_daily_close_email_failure(tmp_path, monkeypatch):
    """When email fails after signal insert, run is marked failed and signal row is retained."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    mock_post = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))

    with patch("signal_system.data.finnhub_client.fetch_spy_close", return_value=591.42), \
         patch("signal_system.delivery.email_sender.send_email", side_effect=smtplib.SMTPException("SMTP down")), \
         patch("httpx.post", mock_post):
        with pytest.raises(smtplib.SMTPException):
            daily_close.run()

    # Signal was inserted before email failed — row should exist
    conn = sqlite3.connect(tmp_path / "test.db")
    signal_row = conn.execute("SELECT ticker FROM signals WHERE agent='DAILY_CLOSE'").fetchone()
    run_row = conn.execute("SELECT status FROM runs").fetchone()
    conn.close()
    assert signal_row is not None, "Signal should be persisted even if email fails"
    assert run_row[0] == "failed"


def test_config_optional_fallback_and_phase_validation(monkeypatch):
    """DISCOVERY_PHASE=invalid must raise RuntimeError; THESIS_PATH defaults to 'thesis.yaml'."""
    import importlib
    import signal_system.config as config_module

    # Test invalid DISCOVERY_PHASE raises RuntimeError on reload
    monkeypatch.setenv("DISCOVERY_PHASE", "invalid")
    with pytest.raises(RuntimeError, match="DISCOVERY_PHASE"):
        importlib.reload(config_module)

    # Restore valid state
    monkeypatch.setenv("DISCOVERY_PHASE", "A")
    importlib.reload(config_module)

    # THESIS_PATH defaults to 'thesis.yaml' when env is unset
    monkeypatch.delenv("THESIS_PATH", raising=False)
    importlib.reload(config_module)
    assert config_module.THESIS_PATH == "thesis.yaml"


def test_init_db_idempotent_and_new_schema(tmp_path, monkeypatch):
    """init_db() must be idempotent and add new columns + tables to the schema."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")

    # Call twice — must not raise on second call
    repository.init_db()
    repository.init_db()

    conn = sqlite3.connect(tmp_path / "test.db")
    # New columns on signals table
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
    for col in ("routing_status", "signal_price_snapshot", "model_version", "thesis_version_hash"):
        assert col in col_names, f"Column {col!r} missing from signals"

    # New tables
    table_names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "wash_sale" in table_names, "wash_sale table not found"
    assert "llm_calls" in table_names, "llm_calls table not found"
    conn.close()


def test_insert_signal_idempotent(tmp_path, monkeypatch):
    """insert_signal(Signal) must return True on first insert, False on duplicate."""
    from datetime import datetime, timezone
    from signal_system.models import Signal, compute_alert_id

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    alert_id = compute_alert_id("SPY", "2026-05-15", "idempotent_test", "TEST")
    signal = Signal(
        ticker="SPY",
        score=100.0,
        severity="INFORMATIONAL",
        agent="TEST",
        timestamp=datetime.now(timezone.utc),
        alert_id=alert_id,
        title="Idempotency test signal",
    )

    first = repository.insert_signal(signal)
    second = repository.insert_signal(signal)

    assert first is True, "First insert must return True"
    assert second is False, "Duplicate insert must return False"

    conn = sqlite3.connect(tmp_path / "test.db")
    count = conn.execute("SELECT COUNT(*) FROM signals WHERE alert_id=?", (alert_id,)).fetchone()[0]
    conn.close()
    assert count == 1, f"Expected exactly 1 row, got {count}"


def test_count_delivered_today_filters_by_routing_status(tmp_path, monkeypatch):
    """count_delivered_today() must only count DELIVERED signals from today's ET date."""
    from datetime import datetime, date, timedelta
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    et = ZoneInfo("America/New_York")
    today_iso = datetime.now(et).date().isoformat()
    yesterday_iso = (datetime.now(et).date() - timedelta(days=1)).isoformat()

    conn = sqlite3.connect(tmp_path / "test.db")
    # DELIVERED signal today
    conn.execute("""
        INSERT INTO signals (alert_id, timestamp, agent, severity, ticker, title, routing_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("aid-1", today_iso + "T12:00:00", "TEST", "INFORMATIONAL", "SPY", "signal 1", "DELIVERED"))
    # NULL routing_status today (should be excluded)
    conn.execute("""
        INSERT INTO signals (alert_id, timestamp, agent, severity, ticker, title, routing_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("aid-2", today_iso + "T12:01:00", "TEST", "INFORMATIONAL", "AAPL", "signal 2", None))
    # DELIVERED yesterday (should be excluded)
    conn.execute("""
        INSERT INTO signals (alert_id, timestamp, agent, severity, ticker, title, routing_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("aid-3", yesterday_iso + "T12:00:00", "TEST", "ACTION_REQUIRED", "MSFT", "signal 3", "DELIVERED"))
    conn.commit()
    conn.close()

    result = repository.count_delivered_today()
    assert result.get("INFORMATIONAL", 0) == 1, f"Expected 1 INFORMATIONAL delivered today, got {result}"
    assert result.get("ACTION_REQUIRED", 0) == 0, f"Expected 0 ACTION_REQUIRED today, got {result}"


def test_load_thesis_happy_path(tmp_path):
    """load_thesis() on a future-dated YAML returns (Thesis, version_hash)."""
    from datetime import date
    from signal_system.data.thesis_loader import load_thesis

    thesis_yaml = tmp_path / "thesis.yaml"
    thesis_yaml.write_text(
        "review_due: 2027-01-01\n"
        "pillars:\n"
        "  - name: monetary_policy\n"
        "    description: Fed policy\n"
        "    keywords: [rate cut, FOMC]\n"
        "  - name: ai_capex\n"
        "    description: AI infrastructure spend\n"
        "    keywords: [GPU, data center]\n"
    )

    thesis, version_hash = load_thesis(thesis_yaml)
    assert thesis.review_due > date.today()
    assert len(thesis.pillars) >= 2
    assert len(version_hash) == 64 and all(c in "0123456789abcdef" for c in version_hash)


def test_load_thesis_stale_raises(tmp_path):
    """load_thesis() on a past-dated YAML raises ThesisStaleError (RuntimeError subclass)."""
    from signal_system.data.thesis_loader import load_thesis, ThesisStaleError

    thesis_yaml = tmp_path / "thesis.yaml"
    thesis_yaml.write_text(
        "review_due: 2020-01-01\n"
        "pillars:\n"
        "  - name: old_pillar\n"
        "    description: Outdated\n"
        "    keywords: [old]\n"
    )

    assert issubclass(ThesisStaleError, RuntimeError)
    with pytest.raises(ThesisStaleError):
        load_thesis(thesis_yaml)


def test_load_thesis_invalid_schema_raises_validation_error(tmp_path):
    """load_thesis() on YAML missing required 'pillars' raises pydantic.ValidationError."""
    from pydantic import ValidationError
    from signal_system.data.thesis_loader import load_thesis

    thesis_yaml = tmp_path / "thesis.yaml"
    thesis_yaml.write_text("review_due: 2027-01-01\n")  # missing pillars field

    with pytest.raises(ValidationError):
        load_thesis(thesis_yaml)


def test_md5_bucket_deterministic():
    """_md5_bucket must return the same value across two calls and match direct hashlib computation."""
    import hashlib
    from signal_system.data.universe import _md5_bucket

    result1 = _md5_bucket("AAPL")
    result2 = _md5_bucket("AAPL")
    assert result1 == result2, "Not deterministic across two calls"

    expected = int(hashlib.md5("AAPL".encode()).hexdigest(), 16) % 3
    assert result1 == expected, f"Bucket mismatch: got {result1}, expected {expected}"
    assert result1 in (0, 1, 2), f"Bucket out of range: {result1}"


def test_get_todays_universe_excludes_k1(tmp_path, monkeypatch):
    """K-1 ETFs (k1_etf=1) must never appear in get_todays_universe() output."""
    from pathlib import Path
    import signal_system.data.universe as universe_mod

    csv_content = "ticker,core_holding,k1_etf\nAAPL,1,0\nUSO,0,1\nUNG,0,1\n"
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text(csv_content)
    monkeypatch.setattr(universe_mod, "UNIVERSE_PATH", csv_path)

    result = universe_mod.get_todays_universe()
    assert "USO" not in result, "USO (K-1 ETF) must be excluded"
    assert "UNG" not in result, "UNG (K-1 ETF) must be excluded"
    assert "AAPL" in result, "AAPL (core holding) must always be included"


def test_get_todays_universe_includes_core_excludes_off_partition(tmp_path, monkeypatch):
    """Core holdings always appear; non-core tickers in wrong partition are excluded."""
    import signal_system.data.universe as universe_mod
    from signal_system.data.universe import _md5_bucket, _today_bucket

    today_bucket = _today_bucket()

    # Find a ticker from probe set that is NOT in today's bucket
    off_partition_ticker = None
    for probe in ["FOO", "BAR", "BAZ", "QUX", "ZZZ", "AAA"]:
        if _md5_bucket(probe) != today_bucket:
            off_partition_ticker = probe
            break
    assert off_partition_ticker is not None, "Could not find a ticker off today's partition"

    csv_content = (
        "ticker,core_holding,k1_etf\n"
        f"AAPL,1,0\n"
        f"{off_partition_ticker},0,0\n"
    )
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text(csv_content)
    monkeypatch.setattr(universe_mod, "UNIVERSE_PATH", csv_path)

    result = universe_mod.get_todays_universe()
    assert "AAPL" in result, "Core holding AAPL must always be included"
    assert off_partition_ticker not in result, (
        f"{off_partition_ticker} (bucket {_md5_bucket(off_partition_ticker)}) "
        f"must be excluded when today's bucket is {today_bucket}"
    )


def test_phase1_integration_imports(tmp_path, monkeypatch):
    """Phase 1 integration check: all public surfaces importable + init_db + get_todays_universe."""
    from signal_system.models import Signal, compute_alert_id
    from signal_system.data.thesis_loader import load_thesis, ThesisStaleError
    from signal_system.data.universe import get_todays_universe
    from signal_system.state.repository import count_delivered_today, init_db
    from signal_system import config

    # DB operations work against a tmp_path DB
    from signal_system.state import repository
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "signals.db")
    init_db()

    # get_todays_universe uses the committed universe.csv (not monkeypatched here)
    tickers = get_todays_universe()
    assert isinstance(tickers, list)
    assert len(tickers) > 0, "Expected at least core holdings in universe"
    k1_etfs = {"USO", "UNG", "DBC", "GSG"}
    for k1 in k1_etfs:
        assert k1 not in tickers, f"{k1} (K-1 ETF) must not appear in universe"

    # count_delivered_today returns a dict (may be empty if no signals)
    result = count_delivered_today()
    assert isinstance(result, dict)

    # config exports all three new Phase 1 vars
    assert config.ANTHROPIC_MODEL
    assert config.THESIS_PATH
    assert config.DISCOVERY_PHASE in ("A", "B")


# ---------------------------------------------------------------------------
# Phase 2: T2 (RED) — token bucket, fetch_quotes, 429 retry
# ---------------------------------------------------------------------------

def test_token_bucket_calls_sleep(monkeypatch):
    """_acquire_slot() must call time.sleep with a positive value on the second call."""
    import signal_system.data.finnhub_client as fc

    counter = [0]
    time_seq = [0.0, 0.0, 1.2]

    def mono():
        val = time_seq[min(counter[0], len(time_seq) - 1)]
        counter[0] += 1
        return val

    sleep_calls = []

    monkeypatch.setattr(fc.time, "monotonic", mono)
    monkeypatch.setattr(fc.time, "sleep", lambda s: sleep_calls.append(s))
    fc._next_call_at = 0.0

    fc._acquire_slot()  # first call: now=0.0, _next_call_at=0.0 → wait=0 → no positive sleep
    fc._acquire_slot()  # second call: now=0.0, _next_call_at=_MIN_INTERVAL → wait=_MIN_INTERVAL

    positive_sleeps = [s for s in sleep_calls if s > 0]
    assert len(positive_sleeps) >= 1, f"Expected sleep with positive value, got: {sleep_calls}"


def test_fetch_quotes_returns_dict(monkeypatch):
    """fetch_quotes returns a dict mapping each ticker to its quote dict."""
    import signal_system.data.finnhub_client as fc

    fake_quote = {"c": 150.0, "h": 151.0, "l": 149.0, "o": 149.5, "pc": 148.0, "t": 1700000000}
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.quote.return_value = fake_quote
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc.fetch_quotes(["AAPL", "MSFT"])

    assert isinstance(result, dict)
    assert "AAPL" in result and "MSFT" in result
    assert result["AAPL"]["c"] > 0
    assert result["MSFT"]["c"] > 0


def test_fetch_quotes_none_on_zero_price(monkeypatch):
    """fetch_quotes returns None for a ticker whose close price is 0."""
    import signal_system.data.finnhub_client as fc

    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.quote.return_value = {"c": 0, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc.fetch_quotes(["UNKNOWN"])
    assert result["UNKNOWN"] is None


def test_retry_on_429(monkeypatch):
    """_fetch_single_quote retries up to 5 times on 429 then raises FinnhubAPIException."""
    import signal_system.data.finnhub_client as fc
    from finnhub.exceptions import FinnhubAPIException

    exc = _make_finnhub_exc(429)
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.quote.side_effect = exc
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    with pytest.raises(FinnhubAPIException):
        fc._fetch_single_quote("AAPL")

    assert mock_client.quote.call_count == 5, (
        f"Expected 5 retry attempts, got {mock_client.quote.call_count}"
    )


def test_no_retry_on_403(monkeypatch):
    """_fetch_single_quote returns None on 403 without retrying (exactly 1 call)."""
    import signal_system.data.finnhub_client as fc

    exc = _make_finnhub_exc(403)
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.quote.side_effect = exc
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc._fetch_single_quote("AAPL")
    assert result is None
    assert mock_client.quote.call_count == 1, (
        f"Expected exactly 1 call (no retry), got {mock_client.quote.call_count}"
    )


# ---------------------------------------------------------------------------
# Phase 2: T4 (RED) — 404 paid-tier, fetch_company_news behaviors
# ---------------------------------------------------------------------------

def test_paid_tier_404_returns_none(monkeypatch):
    """_fetch_single_quote returns None on 404 without retrying (exactly 1 call)."""
    import signal_system.data.finnhub_client as fc

    exc = _make_finnhub_exc(404)
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.quote.side_effect = exc
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc._fetch_single_quote("UNKNOWN")
    assert result is None
    assert mock_client.quote.call_count == 1, (
        f"Expected exactly 1 call (no retry on 404), got {mock_client.quote.call_count}"
    )


def test_company_news_returns_list(monkeypatch):
    """fetch_company_news returns a list with headline and source keys."""
    import signal_system.data.finnhub_client as fc

    fake_news = [
        {
            "headline": "Test headline",
            "source": "Reuters",
            "datetime": 1700000000,
            "url": "",
            "summary": "",
            "id": 1,
            "image": "",
            "category": "",
            "related": "AAPL",
        }
    ]
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.company_news.return_value = fake_news
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc.fetch_company_news("AAPL", date(2026, 5, 1), date(2026, 5, 15))
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["headline"] == "Test headline"
    assert result[0]["source"] == "Reuters"


def test_company_news_empty_on_no_results(monkeypatch):
    """fetch_company_news returns [] when company_news returns empty list."""
    import signal_system.data.finnhub_client as fc

    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.company_news.return_value = []
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc.fetch_company_news("TICKER", date(2026, 5, 1), date(2026, 5, 15))
    assert result == []


def test_company_news_returns_empty_on_paid_tier(monkeypatch):
    """fetch_company_news returns [] on 403 paid-tier error (no exception propagates)."""
    import signal_system.data.finnhub_client as fc

    exc = _make_finnhub_exc(403)
    monkeypatch.setattr(fc, "_acquire_slot", lambda: None)
    mock_client = MagicMock()
    mock_client.company_news.side_effect = exc
    monkeypatch.setattr(fc, "_get_client", lambda: mock_client)

    result = fc.fetch_company_news("TICKER", date(2026, 5, 1), date(2026, 5, 15))
    assert result == []


def test_company_news_date_format(monkeypatch):
    """fetch_company_news passes from_date and to_date as YYYY-MM-DD strings."""
    import signal_system.data.finnhub_client as fc

    captured = {}

    def fake_raw(ticker, from_str, to_str):
        captured["from_str"] = from_str
        captured["to_str"] = to_str
        return []

    monkeypatch.setattr(fc, "_fetch_company_news_raw", fake_raw)

    fc.fetch_company_news("AAPL", date(2026, 5, 1), date(2026, 5, 15))

    assert captured["from_str"] == "2026-05-01", f"Bad from_str: {captured['from_str']!r}"
    assert captured["to_str"] == "2026-05-15", f"Bad to_str: {captured['to_str']!r}"


# ---------------------------------------------------------------------------
# Phase 2: T6 — Integration smoke test
# ---------------------------------------------------------------------------

def test_phase2_public_api_importable():
    """All Phase 2 public surfaces are importable and have correct signatures."""
    import inspect
    from signal_system.data.finnhub_client import (
        fetch_spy_close,
        fetch_quotes,
        fetch_company_news,
        PAID_TIER_STATUS_CODES,
    )

    sig = inspect.signature(fetch_quotes)
    assert "tickers" in sig.parameters

    sig2 = inspect.signature(fetch_company_news)
    assert set(sig2.parameters.keys()) == {"ticker", "from_date", "to_date"}

    assert 403 in PAID_TIER_STATUS_CODES
    assert 404 in PAID_TIER_STATUS_CODES
