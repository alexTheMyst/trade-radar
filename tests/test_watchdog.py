"""Tests for the watchdog meta-monitor job.

check_jobs() is pure over repository state + EXPECTED_MAX_AGE_HOURS, so these
tests seed the `runs` table directly (raw SQL, matching the established test
pattern) and assert on the (failing, stale) lists with a fixed `now`. The
run()-level tests patch the sender and assert exactly one Telegram message is
emitted when something is wrong, and none when everything is healthy.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from signal_system.jobs.watchdog import (
    EXPECTED_MAX_AGE_HOURS,
    check_jobs,
    render_watchdog_alert,
)
from signal_system.state import repository

_ET = ZoneInfo("America/New_York")

MONITORED_JOBS = [job for job, max_age in EXPECTED_MAX_AGE_HOURS.items() if max_age is not None]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def _insert_run(job: str, started_at: datetime, status: str) -> None:
    conn = sqlite3.connect(str(repository.DB_PATH))
    conn.execute(
        "INSERT INTO runs (run_id, job, started_at, ended_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), job, started_at.isoformat(), started_at.isoformat(), status),
    )
    conn.commit()
    conn.close()


def _seed_healthy(at: datetime, *, exclude: set[str] | None = None) -> None:
    for job in MONITORED_JOBS:
        if exclude and job in exclude:
            continue
        _insert_run(job, at - timedelta(hours=1), "success")


def _latest_run_status(job: str) -> str | None:
    conn = sqlite3.connect(str(repository.DB_PATH))
    row = conn.execute(
        "SELECT status FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 1", (job,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_watchdog_flags_stale_job(db):
    """An old success beyond the SLA lands in the stale list."""
    now = datetime(2026, 8, 12, 18, 0, tzinfo=_ET)  # Wednesday
    _seed_healthy(now, exclude={"news-morning"})
    _insert_run("news-morning", now - timedelta(hours=48), "success")

    failing, stale = check_jobs(now=now)
    assert any(job == "news-morning" for job, _ in stale)
    assert failing == []


def test_watchdog_run_sends_one_message_when_stale(db):
    """run() emits exactly one Telegram message for a genuinely stale job."""
    from signal_system.jobs import watchdog

    _seed_healthy(datetime.now(_ET), exclude={"news-morning"})
    _insert_run("news-morning", datetime.now(_ET) - timedelta(days=60), "success")

    mock_send = MagicMock()
    with patch("httpx.post", MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))), \
         patch("signal_system.jobs.watchdog.send_message", mock_send):
        watchdog.run()

    assert mock_send.call_count == 1
    assert "news-morning" in mock_send.call_args.args[0]


def test_watchdog_flags_failing_last_run(db):
    """A most-recent failed run is flagged even when an older success is fresh."""
    now = datetime(2026, 8, 12, 18, 0, tzinfo=_ET)
    _seed_healthy(now)
    _insert_run("daily-close", now - timedelta(minutes=5), "failed")

    failing, stale = check_jobs(now=now)

    assert any(job == "daily-close" for job, _ in failing)
    assert not any(job == "daily-close" for job, _ in stale)


def test_watchdog_silent_when_all_healthy(db):
    """A fully healthy system sends nothing and records a success run row."""
    _seed_healthy(datetime.now(_ET))

    from signal_system.jobs import watchdog

    mock_send = MagicMock()
    mock_post = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))
    with patch.dict("os.environ", {"HEALTHCHECKS_UUID_WATCHDOG": "watchdog-check-123"}):
        with patch("httpx.post", mock_post), patch(
            "signal_system.jobs.watchdog.send_message", mock_send
        ):
            watchdog.run()

    assert mock_send.call_count == 0
    urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert urls == [
        "https://hc-ping.com/watchdog-check-123/start",
        "https://hc-ping.com/watchdog-check-123",
    ]
    assert _latest_run_status("watchdog") == "success"


def test_watchdog_skips_none_cadence_jobs(db):
    """Jobs mapped to None are never flagged, even with a failed last run."""
    now = datetime(2026, 8, 12, 18, 0, tzinfo=_ET)
    _seed_healthy(now)
    _insert_run("review-report", now - timedelta(minutes=5), "failed")
    _insert_run("outcome-backfill", now - timedelta(minutes=5), "failed")

    failing, stale = check_jobs(now=now)

    assert failing == []
    assert stale == []


def test_watchdog_never_succeeded(db):
    """A monitored job with zero runs is flagged 'never succeeded'."""
    now = datetime(2026, 8, 12, 18, 0, tzinfo=_ET)
    _seed_healthy(now, exclude={"sentinel"})

    failing, stale = check_jobs(now=now)

    assert ("sentinel", "never succeeded") in stale


def test_watchdog_friday_success_keeps_monday_green(db):
    """Weekend hours don't count against the 30h SLA for weekday jobs."""
    monday_0930 = datetime(2026, 8, 10, 9, 30, tzinfo=_ET)
    friday_1715 = datetime(2026, 8, 7, 17, 15, tzinfo=_ET)
    for job in MONITORED_JOBS:
        _insert_run(job, friday_1715, "success")

    failing, stale = check_jobs(now=monday_0930)

    assert failing == []
    assert stale == []


def test_watchdog_flags_missed_monday_on_tuesday(db):
    """Once Monday is skipped, the same Friday success is stale by Tuesday."""
    tuesday_0930 = datetime(2026, 8, 11, 9, 30, tzinfo=_ET)
    friday_0900 = datetime(2026, 8, 7, 9, 0, tzinfo=_ET)
    for job in MONITORED_JOBS:
        _insert_run(job, friday_0900, "success")

    failing, stale = check_jobs(now=tuesday_0930)
    stale_jobs = {job for job, _ in stale}

    assert "news-morning" in stale_jobs
    assert "daily-close" in stale_jobs
    assert failing == []


def test_render_watchdog_alert_format():
    text = render_watchdog_alert(
        failing=[("news-morning", "last run 2026-08-09 09:00 ET (failed)")],
        stale=[("sentinel", "last success 3 days ago (SLA 30h)")],
    )

    assert "⚠️ SIGNAL SYSTEM WATCHDOG — 2 job(s) need attention" in text
    assert "FAILING (most recent run failed):" in text
    assert "• news-morning — last run 2026-08-09 09:00 ET (failed)" in text
    assert "STALE (no success within SLA):" in text
    assert "• sentinel — last success 3 days ago (SLA 30h)" in text
    assert "Investigate: uv run python -m signal_system <job>" in text


def test_repository_latest_run_helpers(db):
    _insert_run("daily-close", datetime(2026, 8, 7, 17, 15, tzinfo=_ET), "success")
    _insert_run("daily-close", datetime(2026, 8, 10, 17, 15, tzinfo=_ET), "failed")

    last_success = repository.get_latest_successful_run_datetime("daily-close")
    last_run = repository.get_latest_run("daily-close")

    assert last_success is not None
    assert last_success.tzinfo is not None
    assert last_success.date().isoformat() == "2026-08-07"
    assert last_run is not None
    assert last_run["status"] == "failed"
    assert last_run["started_at"].date().isoformat() == "2026-08-10"
    assert repository.get_latest_successful_run_datetime("sentinel") is None
    assert repository.get_latest_run("sentinel") is None
