"""Tests for the preflight diagnostic command (Plan 02).

Each check function is a pure status classifier over injected boundaries;
external calls (Anthropic, Finnhub, thesis, holdings, DB) are monkeypatched so
no test touches the network or the live state database.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest
from finnhub.exceptions import FinnhubAPIException
import requests.models

from signal_system import preflight
from signal_system.data import finnhub_client, holdings, universe
from signal_system.data.holdings import Holding
from signal_system.state import repository

_ET = ZoneInfo("America/New_York")


def _stub_config(**overrides) -> types.SimpleNamespace:
    values = {
        "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        "ANTHROPIC_API_KEY": "test-key",
        "THESIS_PATH": "thesis.yaml",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _finnhub_exc(status_code: int = 403) -> FinnhubAPIException:
    resp = requests.models.Response()
    resp.status_code = status_code
    resp._content = b"{}"
    return FinnhubAPIException(resp)


# --- A: Secrets & config -----------------------------------------------------


def test_check_required_secrets_pass(monkeypatch):
    for name in preflight.REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "x")
    check = preflight.check_required_secrets()
    assert check.status == preflight.PASS


def test_check_required_secrets_fail_lists_missing(monkeypatch):
    for name in preflight.REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("ANTHROPIC_MODEL")
    check = preflight.check_required_secrets()
    assert check.status == preflight.FAIL
    assert "ANTHROPIC_MODEL" in check.message


def test_check_per_job_healthchecks_warns_when_unset():
    check = preflight.check_per_job_healthchecks()
    assert check.status == preflight.WARN
    assert "masking risk" in check.message


def test_check_per_job_healthchecks_pass_when_all_set(monkeypatch):
    for job in preflight.SCHEDULED_JOBS:
        monkeypatch.setenv(
            f"HEALTHCHECKS_UUID_{job.upper().replace('-', '_')}", "uuid"
        )
    check = preflight.check_per_job_healthchecks()
    assert check.status == preflight.PASS


def test_check_model_pin_pass(monkeypatch):
    monkeypatch.setattr(
        preflight, "_config_or_none", lambda: _stub_config(ANTHROPIC_MODEL="claude-sonnet-4-6")
    )
    check = preflight.check_model_pin()
    assert check.status == preflight.PASS
    assert "claude-sonnet-4-6" in check.message


def test_check_model_pin_float_alias_warns(monkeypatch):
    monkeypatch.setattr(
        preflight, "_config_or_none", lambda: _stub_config(ANTHROPIC_MODEL="claude-sonnet-4-6-latest")
    )
    check = preflight.check_model_pin()
    assert check.status == preflight.WARN


def test_check_model_pin_skips_when_config_unavailable(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: None)
    check = preflight.check_model_pin()
    assert check.status == preflight.WARN
    assert "skipped" in check.message


# --- B: Anthropic ------------------------------------------------------------


def test_check_anthropic_pass(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config())
    usage = types.SimpleNamespace(
        input_tokens=5, output_tokens=3, cache_read_input_tokens=2, cache_creation_input_tokens=1
    )
    monkeypatch.setattr(preflight, "_probe_anthropic", lambda model, key: usage)
    check = preflight.check_anthropic()
    assert check.status == preflight.PASS
    assert "in=5" in check.message


def test_check_anthropic_400_reported_not_raised(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config())
    response = httpx.Response(400, request=httpx.Request("POST", "http://example.com"))
    error = anthropic.BadRequestError(
        "temperature is deprecated for this model", response=response, body={}
    )

    def _raise(model, key):
        raise error

    monkeypatch.setattr(preflight, "_probe_anthropic", _raise)
    check = preflight.check_anthropic()
    assert check.status == preflight.FAIL
    assert "rejected params" in check.message
    assert "deprecated" in check.message


def test_check_anthropic_generic_error_is_fail(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config())

    def _raise(model, key):
        raise TimeoutError("network down")

    monkeypatch.setattr(preflight, "_probe_anthropic", _raise)
    check = preflight.check_anthropic()
    assert check.status == preflight.FAIL
    assert "network down" in check.message


# --- C: Finnhub --------------------------------------------------------------


def test_check_finnhub_core_pass(monkeypatch):
    monkeypatch.setattr(finnhub_client, "fetch_spy_close", lambda: 641.2)
    monkeypatch.setattr(finnhub_client, "fetch_quote", lambda t: {"c": 250.1, "h": 251, "l": 249, "dp": 1.0})
    monkeypatch.setattr(finnhub_client, "fetch_company_news", lambda *a, **k: [{"headline": "x"}] * 12)
    check = preflight.check_finnhub_core()
    assert check.status == preflight.PASS
    assert "spy_close=641.20" in check.message
    assert "company_news=12 items" in check.message


def test_check_finnhub_core_spy_fail(monkeypatch):
    monkeypatch.setattr(finnhub_client, "fetch_spy_close", lambda: (_ for _ in ()).throw(ValueError("bad")))
    check = preflight.check_finnhub_core()
    assert check.status == preflight.FAIL
    assert "fetch_spy_close failed" in check.message


def test_check_finnhub_core_quote_none_is_fail(monkeypatch):
    monkeypatch.setattr(finnhub_client, "fetch_spy_close", lambda: 641.2)
    monkeypatch.setattr(finnhub_client, "fetch_quote", lambda t: None)
    check = preflight.check_finnhub_core()
    assert check.status == preflight.FAIL


def test_check_finnhub_core_empty_news_is_fail(monkeypatch):
    monkeypatch.setattr(finnhub_client, "fetch_spy_close", lambda: 641.2)
    monkeypatch.setattr(finnhub_client, "fetch_quote", lambda t: {"c": 250.1})
    monkeypatch.setattr(finnhub_client, "fetch_company_news", lambda *a, **k: [])
    check = preflight.check_finnhub_core()
    assert check.status == preflight.FAIL
    assert "paid-gated" in check.message


def test_check_general_news_pass_and_warn(monkeypatch):
    monkeypatch.setattr(finnhub_client, "fetch_general_news", lambda *a, **k: [{"headline": "x"}])
    assert preflight.check_general_news().status == preflight.PASS

    monkeypatch.setattr(finnhub_client, "fetch_general_news", lambda *a, **k: [])
    check = preflight.check_general_news()
    assert check.status == preflight.WARN


def test_check_paid_gated_probes_all_ok(monkeypatch):
    class _FakeClient:
        def quote(self, symbol):
            return {"c": 1.0}

        def stock_insider_sentiment(self, *a, **k):
            return {"data": []}

    monkeypatch.setattr(finnhub_client, "_get_client", lambda: _FakeClient())
    check = preflight.check_paid_gated_probes()
    assert check.status == preflight.PASS


def test_check_paid_gated_probes_reports_gated(monkeypatch):
    class _FakeClient:
        def quote(self, symbol):
            if symbol == "^GSPC":
                return {"c": 1.0}
            raise _finnhub_exc(403)

        def stock_insider_sentiment(self, *a, **k):
            raise _finnhub_exc(404)

    monkeypatch.setattr(finnhub_client, "_get_client", lambda: _FakeClient())
    check = preflight.check_paid_gated_probes()
    assert check.status == preflight.WARN
    assert "^VIX" in check.message and "paid-gated" in check.message


# --- D: Thesis ---------------------------------------------------------------


def test_check_thesis_pass(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config(THESIS_PATH="t.yaml"))
    thesis = types.SimpleNamespace(review_due=datetime.now(_ET).date() + timedelta(days=60))
    monkeypatch.setattr(preflight, "load_thesis", lambda path: (thesis, "hash"))
    check = preflight.check_thesis()
    assert check.status == preflight.PASS
    assert "60 days" in check.message


def test_check_thesis_stale_is_fail(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config(THESIS_PATH="t.yaml"))
    monkeypatch.setattr(
        preflight,
        "load_thesis",
        lambda path: (_ for _ in ()).throw(
            preflight.ThesisStaleError("thesis.yaml review_due is 2026-01-01 (today: 2026-08-10)")
        ),
    )
    check = preflight.check_thesis()
    assert check.status == preflight.FAIL
    assert "review_due" in check.message


def test_check_thesis_missing_is_fail(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config(THESIS_PATH="missing.yaml"))
    monkeypatch.setattr(preflight, "load_thesis", lambda path: (_ for _ in ()).throw(FileNotFoundError()))
    check = preflight.check_thesis()
    assert check.status == preflight.FAIL
    assert "missing" in check.message


def test_check_thesis_due_soon_warns(monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config(THESIS_PATH="t.yaml"))
    thesis = types.SimpleNamespace(review_due=datetime.now(_ET).date() + timedelta(days=7))
    monkeypatch.setattr(preflight, "load_thesis", lambda path: (thesis, "hash"))
    check = preflight.check_thesis()
    assert check.status == preflight.WARN
    assert "review due soon" in check.message


# --- E: Coverage -------------------------------------------------------------


def test_check_holdings_pass_and_fail(monkeypatch):
    monkeypatch.setattr(holdings, "load_holdings", lambda *a, **k: [Holding("FCX", 40, 38.1, "schwab_main")])
    check = preflight.check_holdings()
    assert check.status == preflight.PASS
    assert "1 positions" in check.message

    monkeypatch.setattr(holdings, "load_holdings", lambda *a, **k: (_ for _ in ()).throw(holdings.EmptyHoldingsError("no rows")))
    check = preflight.check_holdings()
    assert check.status == preflight.FAIL


def test_check_drift_warns_and_passes(monkeypatch):
    rows = [Holding("FCX", 40, 38.1, "schwab_main"), Holding("NVDA", 10, 800, "roth_ira")]
    monkeypatch.setattr(holdings, "load_holdings", lambda *a, **k: rows)
    monkeypatch.setattr(holdings, "holdings_universe_drift", lambda holdings=None, core_holdings=None: ["FCX", "MU"])
    check = preflight.check_drift()
    assert check.status == preflight.WARN
    assert "2 held tickers not scanned" in check.message

    monkeypatch.setattr(holdings, "holdings_universe_drift", lambda holdings=None, core_holdings=None: [])
    assert preflight.check_drift().status == preflight.PASS


def test_check_universe_fail_on_missing_path(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "UNIVERSE_PATH", tmp_path / "nope.csv")
    check = preflight.check_universe()
    assert check.status == preflight.FAIL


def test_check_universe_pass(tmp_path, monkeypatch):
    uni = tmp_path / "universe.csv"
    uni.write_text("ticker,core_holding\nAAPL,true\n")
    monkeypatch.setattr(universe, "UNIVERSE_PATH", uni)
    monkeypatch.setattr(universe, "get_core_holdings", lambda: ["AAPL", "NVDA"])
    monkeypatch.setattr(universe, "get_todays_universe", lambda: ["AAPL", "NVDA", "MU"])
    check = preflight.check_universe()
    assert check.status == preflight.PASS


def test_check_universe_empty_core_is_fail(tmp_path, monkeypatch):
    uni = tmp_path / "universe.csv"
    uni.write_text("ticker,core_holding\nAAPL,true\n")
    monkeypatch.setattr(universe, "UNIVERSE_PATH", uni)
    monkeypatch.setattr(universe, "get_core_holdings", lambda: [])
    monkeypatch.setattr(universe, "get_todays_universe", lambda: ["MU"])
    check = preflight.check_universe()
    assert check.status == preflight.FAIL


def test_check_k1_denylist_pass_and_leak(tmp_path, monkeypatch):
    uni = tmp_path / "universe.csv"
    uni.write_text("ticker,core_holding\nAAPL,true\n")
    monkeypatch.setattr(universe, "UNIVERSE_PATH", uni)
    monkeypatch.setattr(universe, "get_all_tickers", lambda: ["AAPL", "MU"])
    assert preflight.check_k1_denylist().status == preflight.PASS

    monkeypatch.setattr(universe, "get_all_tickers", lambda: ["AAPL", "USO"])
    check = preflight.check_k1_denylist()
    assert check.status == preflight.FAIL
    assert "USO" in check.message


# --- F: State ----------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    repository.init_db()
    return tmp_path / "test.db"


def test_check_db_pass(db):
    check = preflight.check_db()
    assert check.status == preflight.PASS
    assert "WAL on" in check.message


def test_check_db_missing_is_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "missing.db")
    check = preflight.check_db()
    assert check.status == preflight.FAIL


def test_check_backup_freshness(db, monkeypatch):
    fresh = datetime.now(_ET) - timedelta(days=2)
    monkeypatch.setattr(repository, "get_latest_successful_run_datetime", lambda job: fresh)
    assert preflight.check_backup().status == preflight.PASS

    stale = datetime.now(_ET) - timedelta(days=10)
    monkeypatch.setattr(repository, "get_latest_successful_run_datetime", lambda job: stale)
    check = preflight.check_backup()
    assert check.status == preflight.WARN
    assert "10 days ago" in check.message

    monkeypatch.setattr(repository, "get_latest_successful_run_datetime", lambda job: None)
    assert preflight.check_backup().status == preflight.WARN


def test_check_jobs_liveness_flags_never_succeeded(db, monkeypatch):
    monkeypatch.setattr(repository, "get_latest_successful_run_datetime", lambda job: None)
    monkeypatch.setattr(repository, "get_latest_run", lambda job: None)
    check = preflight.check_jobs_liveness()
    assert check.status == preflight.WARN
    assert "never succeeded" in check.message


def test_check_jobs_liveness_pass_when_healthy(db, monkeypatch):
    ts = datetime.now(_ET) - timedelta(hours=5)
    monkeypatch.setattr(repository, "get_latest_successful_run_datetime", lambda job: ts)
    monkeypatch.setattr(repository, "get_latest_run", lambda job: {"started_at": ts, "status": "success"})
    assert preflight.check_jobs_liveness().status == preflight.PASS


# --- runner / exit codes -----------------------------------------------------


def _section(*fns) -> list:
    return [("T", "Test", list(fns))]


def test_run_preflight_all_pass_exit_0(capsys, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "SECTIONS",
        _section(lambda: preflight.Check(preflight.PASS, "a"), lambda: preflight.Check(preflight.PASS, "b")),
    )
    assert preflight.run_preflight() == preflight.EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT: PASS (0 fail, 0 warn)" in out


def test_run_preflight_warn_exit_1(capsys, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "SECTIONS",
        _section(lambda: preflight.Check(preflight.WARN, "w")),
    )
    assert preflight.run_preflight() == preflight.EXIT_WARN


def test_run_preflight_exit_code_worst_status(capsys, monkeypatch):
    monkeypatch.setattr(
        preflight,
        "SECTIONS",
        _section(
            lambda: preflight.Check(preflight.PASS, "ok"),
            lambda: preflight.Check(preflight.WARN, "w"),
            lambda: preflight.Check(preflight.FAIL, "f"),
        ),
    )
    assert preflight.run_preflight() == preflight.EXIT_FAIL
    out = capsys.readouterr().out
    assert "RESULT: FAIL (1 fail, 1 warn)" in out


def test_run_preflight_check_raise_becomes_fail(capsys, monkeypatch):
    def _boom():
        raise ValueError("boom")

    monkeypatch.setattr(preflight, "SECTIONS", _section(_boom))
    assert preflight.run_preflight() == preflight.EXIT_FAIL
    out = capsys.readouterr().out
    assert "unhandled error" in out
    assert "Traceback" not in out


def test_run_preflight_anthropic_400_exits_2(capsys, monkeypatch):
    monkeypatch.setattr(preflight, "_config_or_none", lambda: _stub_config())
    response = httpx.Response(400, request=httpx.Request("POST", "http://example.com"))
    error = anthropic.BadRequestError("temperature is deprecated for this model", response=response, body={})

    def _raise(model, key):
        raise error

    monkeypatch.setattr(preflight, "_probe_anthropic", _raise)
    monkeypatch.setattr(preflight, "SECTIONS", _section(preflight.check_anthropic))
    assert preflight.run_preflight() == preflight.EXIT_FAIL
    out = capsys.readouterr().out
    assert "rejected params" in out
    assert "Traceback" not in out


def test_preflight_readonly(db, monkeypatch):
    from signal_system.models import Signal, compute_alert_id

    sig = Signal(
        ticker="AAPL",
        score=0.8,
        severity="INFORMATIONAL",
        agent="news_classifier",
        timestamp=datetime.now(_ET),
        alert_id=compute_alert_id("AAPL", "2026-08-10", "r", "news_classifier"),
        title="AAPL: update",
    )
    repository.insert_signal(sig, routing_status="DELIVERED")
    repository.insert_run("backup")

    signals_before = repository.count_signals()
    conn = sqlite3.connect(db)
    runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()

    monkeypatch.setattr(
        preflight,
        "SECTIONS",
        _section(preflight.check_db, preflight.check_backup, preflight.check_jobs_liveness),
    )
    preflight.run_preflight()

    assert repository.count_signals() == signals_before
    conn = sqlite3.connect(db)
    runs_after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()
    assert runs_after == runs_before


# --- CLI wiring --------------------------------------------------------------


def test_cli_preflight_broken_env_no_traceback(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "signal_system", "preflight"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == preflight.EXIT_FAIL
    assert "SIGNAL SYSTEM PREFLIGHT" in result.stdout
    assert "Traceback" not in result.stderr
