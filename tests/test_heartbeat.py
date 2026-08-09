"""Tests for per-job healthchecks heartbeat UUIDs.

heartbeat() must resolve its UUID per job: a job with HEALTHCHECKS_UUID_<JOB>
set pings its own check, every other job pings the shared HEALTHCHECKS_UUID.
The UUID is looked up at call time so tests use monkeypatch.setenv without a
config module reload.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from signal_system import config
from signal_system.monitoring import heartbeat


def _mock_post():
    return patch(
        "signal_system.monitoring.heartbeat.httpx.post",
        return_value=MagicMock(raise_for_status=MagicMock()),
    )


def test_shared_uuid_ping_sequence():
    """heartbeat() with no per-job env var pings /start and success on the shared UUID."""
    with _mock_post() as mock_post:
        with heartbeat.heartbeat():
            pass

    urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert urls == [
        "https://hc-ping.com/test-uuid-1234/start",
        "https://hc-ping.com/test-uuid-1234",
    ]


def test_per_job_env_var_used():
    """HEALTHCHECKS_UUID_SENTINEL overrides the shared UUID for job='sentinel'."""
    with patch.dict("os.environ", {"HEALTHCHECKS_UUID_SENTINEL": "per-job-abc"}):
        with _mock_post() as mock_post:
            with heartbeat.heartbeat(job="sentinel"):
                pass

    urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert urls == [
        "https://hc-ping.com/per-job-abc/start",
        "https://hc-ping.com/per-job-abc",
    ]


def test_fallback_when_per_job_unset_or_empty():
    """Unset/empty per-job vars fall back to the shared UUID."""
    for value in (None, ""):
        env = {}
        if value is not None:
            env["HEALTHCHECKS_UUID_DISCOVERY"] = value
        with patch.dict("os.environ", env):
            with _mock_post() as mock_post:
                with heartbeat.heartbeat(job="discovery"):
                    pass
            urls = [str(c.args[0]) for c in mock_post.call_args_list]
            assert urls == [
                "https://hc-ping.com/test-uuid-1234/start",
                "https://hc-ping.com/test-uuid-1234",
            ]


def test_fail_ping_uses_job_uuid_and_reraises():
    """On exception, /fail uses the job UUID and the exception propagates."""
    with patch.dict("os.environ", {"HEALTHCHECKS_UUID_OUTCOME_BACKFILL": "backfill-xyz"}):
        with _mock_post() as mock_post:
            with pytest.raises(ValueError, match="boom"):
                with heartbeat.heartbeat(job="outcome-backfill"):
                    raise ValueError("boom")

    urls = [str(c.args[0]) for c in mock_post.call_args_list]
    assert urls == [
        "https://hc-ping.com/backfill-xyz/start",
        "https://hc-ping.com/backfill-xyz/fail",
    ]


def test_network_failure_swallowed():
    """A failing httpx.post must not interrupt job execution."""
    with patch(
        "signal_system.monitoring.heartbeat.httpx.post",
        side_effect=RuntimeError("network down"),
    ):
        with heartbeat.heartbeat():
            pass  # must not raise


def test_slug_rule_dashes_to_underscores():
    """healthchecks_uuid_for() maps job names to HEALTHCHECKS_UUID_<SLUG>."""
    assert config.healthchecks_uuid_for("daily-close") == "test-uuid-1234"
    with patch.dict(
        "os.environ", {"HEALTHCHECKS_UUID_DAILY_CLOSE": "daily-close-check"}
    ):
        assert config.healthchecks_uuid_for("daily-close") == "daily-close-check"
        assert config.healthchecks_uuid_for("weekly-synthesis") == "test-uuid-1234"


def test_healthchecks_uuid_for_none_uses_shared():
    assert config.healthchecks_uuid_for(None) == "test-uuid-1234"
