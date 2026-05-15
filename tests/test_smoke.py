"""
Smoke tests for signal-system.

Three tests:
  1. init_db creates signals and runs tables.
  2. insert_signal returns a valid UUID v4 and persists the row.
  3. daily_close.run() completes and writes the expected signal row.

All external I/O (Finnhub, SMTP, healthchecks.io) is mocked.
"""

import smtplib
import sqlite3
import uuid
from unittest.mock import MagicMock, patch

import pytest

from signal_system.state import repository
from signal_system.jobs import daily_close


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


def test_insert_signal_returns_uuid(tmp_path, monkeypatch):
    """insert_signal() must return a valid UUID v4 and persist the correct row."""
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()

    alert_id = repository.insert_signal(
        agent="TEST",
        ticker="SPY",
        title="test signal",
        score=100.0,
    )

    # Raises ValueError if not a valid UUID
    uuid.UUID(alert_id)

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute(
        "SELECT agent, ticker FROM signals WHERE alert_id=?", (alert_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "TEST"
    assert row[1] == "SPY"


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
