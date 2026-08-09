"""Tests for the weekly WAL-safe database backup.

backup_database() uses VACUUM INTO (WAL-safe — a plain file copy can miss
uncheckpointed -wal pages), writes a dated signals-YYYYMMDD.db, and sanity-
checks the backup by counting its signals rows.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from signal_system.state import repository
from signal_system.models import Signal, compute_alert_id


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "signals.db")
    repository.init_db()
    return tmp_path / "signals.db"


def _signal(ticker: str, day: int) -> Signal:
    return Signal(
        ticker=ticker,
        score=70.0,
        severity="INFORMATIONAL",
        agent="discovery_agent",
        timestamp=datetime(2026, 6, day, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        alert_id=compute_alert_id(ticker, f"2026-06-{day:02d}", "discovery", "discovery_agent"),
        title=f"{ticker}: score 70",
    )


def _today_iso() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat().replace("-", "")


def test_backup_creates_dated_file_with_count_agreement(db, tmp_path):
    repository.insert_signal(_signal("AAPL", 1), routing_status="DELIVERED")
    repository.insert_signal(_signal("MSFT", 2), routing_status="DELIVERED")

    path, count = repository.backup_database(tmp_path)

    assert path.name == f"signals-{_today_iso()}.db"
    assert count == 2

    backup_conn = sqlite3.connect(path)
    backup_count = backup_conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    backup_conn.close()
    assert backup_count == 2


def test_backup_captures_wal_content(db, tmp_path):
    """Rows still in the -wal (uncheckpointed) must be present in the backup."""
    repository.insert_signal(_signal("NVDA", 3), routing_status="DELIVERED")

    path, count = repository.backup_database(tmp_path)

    assert count == 1
    backup_conn = sqlite3.connect(path)
    row = backup_conn.execute("SELECT ticker FROM signals").fetchone()
    backup_conn.close()
    assert row == ("NVDA",)


def test_backup_same_day_rerun_overwrites(db, tmp_path):
    repository.insert_signal(_signal("AAPL", 1), routing_status="DELIVERED")
    path1, count1 = repository.backup_database(tmp_path)
    repository.insert_signal(_signal("MSFT", 2), routing_status="DELIVERED")
    path2, count2 = repository.backup_database(tmp_path)

    assert path1 == path2
    assert count1 == 1
    assert count2 == 2
    matches = list(tmp_path.glob("signals-*.db"))
    assert len(matches) == 1


def test_backup_sanity_count_matches_live_count(db, tmp_path):
    repository.insert_signal(_signal("AAPL", 1), routing_status="SUPPRESSED")
    repository.insert_signal(_signal("MSFT", 2), routing_status="DELIVERED")
    repository.insert_signal(_signal("NVDA", 3), routing_status="MONITORING")

    _path, count = repository.backup_database(tmp_path)
    assert count == repository.count_signals()


def test_prune_keeps_newest_8_and_ignores_foreign_files(tmp_path):
    from signal_system.jobs.backup import _prune

    for i in range(10):
        (tmp_path / f"signals-202606{10+i}.db").write_text("x")
    notes = tmp_path / "notes.txt"
    notes.write_text("do not delete")

    _prune(tmp_path)

    remaining = sorted(p.name for p in tmp_path.glob("signals-*.db"))
    assert len(remaining) == 8
    assert "signals-20260610.db" not in remaining
    assert "signals-20260617.db" in remaining
    assert notes.exists()


def test_backup_job_success_and_heartbeat_url(db, tmp_path, monkeypatch):
    from signal_system.jobs import backup as backup_job

    monkeypatch.setattr(backup_job.config, "BACKUP_DIR", str(tmp_path / "backups"))
    repository.insert_signal(_signal("AAPL", 1), routing_status="DELIVERED")

    mock_post = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))
    with patch.dict("os.environ", {"HEALTHCHECKS_UUID_BACKUP": "backup-check-123"}):
        with patch("httpx.post", mock_post), \
             patch("signal_system.jobs.backup.send_message"):
            backup_job.run()

    urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert urls == [
        "https://hc-ping.com/backup-check-123/start",
        "https://hc-ping.com/backup-check-123",
    ]

    backups = list((tmp_path / "backups").glob("signals-*.db"))
    assert len(backups) == 1

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM runs WHERE job = 'backup'").fetchone()
    conn.close()
    assert status == ("success",)


def test_backup_job_raises_when_backup_empty_but_live_has_rows(db, tmp_path, monkeypatch):
    from signal_system.jobs import backup as backup_job

    monkeypatch.setattr(backup_job.config, "BACKUP_DIR", str(tmp_path / "backups"))
    repository.insert_signal(_signal("AAPL", 1), routing_status="DELIVERED")

    monkeypatch.setattr(repository, "backup_database", lambda dest_dir: (dest_dir / "x.db", 0))
    monkeypatch.setattr(backup_job, "heartbeat", _noop_heartbeat())

    with pytest.raises(RuntimeError, match="backup"):
        backup_job.run()


def _noop_heartbeat():
    import contextlib

    @contextlib.contextmanager
    def _cm(*_a, **_k):
        yield

    return _cm


def test_resolve_backup_dir_anchors_relative_at_repo_root(tmp_path, monkeypatch):
    from signal_system.jobs import backup as backup_job

    monkeypatch.setattr(backup_job.config, "BACKUP_DIR", "state/backups")
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "state" / "signals.db")
    resolved = backup_job._resolve_backup_dir()
    assert resolved == tmp_path / "state" / "backups"
