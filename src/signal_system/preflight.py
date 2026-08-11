"""preflight.py — the "does this machine work right now" diagnostic.

A human-invoked battery of cheap, read-only checks that prints a
PASS/WARN/FAIL report and returns a CI-usable exit code (0 all pass, 1
warnings only, 2 any failure). It is never scheduled and makes no writes and
sends no alerts.

Design rules (Plan 02):
  - Never crashes: every check swallows its own exceptions and reports FAIL
    instead of raising; the runner is a last-resort safety net on top.
  - Config is imported lazily so a missing secret in .env produces a report
    row under [A] instead of an import-time traceback.
  - Read-only: no runs row, no signals write, no healthcheck ping, and the
    database is opened with SQLite's read-only URI flag.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from signal_system.data import holdings, universe
from signal_system.data.thesis_loader import ThesisStaleError, load_thesis
from signal_system.state import repository

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2

_ET = ZoneInfo("America/New_York")

REQUIRED_ENV_VARS = (
    "FINNHUB_API_KEY",
    "HEALTHCHECKS_UUID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
)

# Matches the per-job healthchecks env vars documented in .env.example.
SCHEDULED_JOBS = (
    "daily-close",
    "news-morning",
    "discovery",
    "advisor",
    "outcome-backfill",
    "review-report",
    "weekly-synthesis",
    "sentinel",
    "backup",
    "watchdog",
)

SKIP_MSG = "skipped — config unavailable (see [A])"

_config_module = None
_config_error: str | None = None


def _get_config():
    """Return the signal_system.config module or raise RuntimeError on failure."""
    global _config_module, _config_error
    if _config_module is None and _config_error is None:
        try:
            from signal_system import config as cfg

            _config_module = cfg
        except Exception as exc:
            _config_error = str(exc)
    if _config_error is not None:
        raise RuntimeError(_config_error)
    return _config_module


def _config_or_none():
    """Return config module, or None when secrets are missing (never raises)."""
    try:
        return _get_config()
    except RuntimeError:
        return None


def _finnhub_or_none():
    """Import finnhub_client lazily (it imports config); None on missing secrets."""
    try:
        import signal_system.data.finnhub_client as fh
    except RuntimeError:
        return None
    return fh


@dataclass(frozen=True, slots=True)
class Check:
    status: str
    message: str


def check_required_secrets() -> Check:
    missing = [
        name
        for name in REQUIRED_ENV_VARS
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        return Check(FAIL, f"missing: {', '.join(missing)}")
    return Check(PASS, "all required secrets present")


def check_per_job_healthchecks() -> Check:
    missing = [
        job
        for job in SCHEDULED_JOBS
        if not (
            os.environ.get(f"HEALTHCHECKS_UUID_{job.upper().replace('-', '_')}") or ""
        ).strip()
    ]
    if missing:
        return Check(
            WARN,
            f"per-job health checks unset: {', '.join(missing)} (masking risk)",
        )
    return Check(PASS, "per-job health checks set for every scheduled job")


def check_model_pin() -> Check:
    cfg = _config_or_none()
    if cfg is None:
        return Check(WARN, SKIP_MSG)
    model = cfg.ANTHROPIC_MODEL
    if "-latest" in model:
        return Check(WARN, f"model {model!r} is a floating alias (-latest) — pin an exact ID")
    return Check(PASS, f"model {model!r} pinned")


def _probe_anthropic(model: str, api_key: str):
    """One minimal messages.parse() call mirroring the classifier's call shape.

    Uses the same model, max_tokens, system cache_control, and output_format
    mechanism as the classifier, so a retired model, a deprecated parameter,
    or an unsupported schema is caught. Returns response.usage.
    """
    from anthropic import Anthropic
    from pydantic import BaseModel

    class _ProbeResult(BaseModel):
        ok: bool

    client = Anthropic(api_key=api_key)
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": "You are a connectivity probe. Always reply with JSON.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": 'Reply with {"ok": true}.'}],
        output_format=_ProbeResult,
    )
    return response.usage


def _format_usage(usage) -> str:
    parts = [f"in={usage.input_tokens}"]
    for attr, label in (
        ("output_tokens", "out"),
        ("cache_read_input_tokens", "cache_read"),
        ("cache_creation_input_tokens", "cache_creation"),
    ):
        value = getattr(usage, attr, None)
        if value is not None:
            parts.append(f"{label}={value}")
    return ", ".join(parts)


def check_anthropic() -> Check:
    cfg = _config_or_none()
    if cfg is None:
        return Check(WARN, SKIP_MSG)
    model = cfg.ANTHROPIC_MODEL
    try:
        import anthropic
    except ImportError as exc:
        return Check(FAIL, f"anthropic SDK unavailable: {exc}")
    try:
        usage = _probe_anthropic(model, cfg.ANTHROPIC_API_KEY)
    except anthropic.BadRequestError as exc:
        return Check(FAIL, f"model {model!r} rejected params: {exc}")
    except Exception as exc:
        return Check(FAIL, f"Anthropic probe failed: {exc}")
    return Check(PASS, f"model {model!r} accepted ({_format_usage(usage)})")


def check_finnhub_core() -> Check:
    fh = _finnhub_or_none()
    if fh is None:
        return Check(WARN, SKIP_MSG)
    today = datetime.now(_ET).date()
    parts: list[str] = []
    try:
        spy = fh.fetch_spy_close()
        parts.append(f"spy_close={spy:.2f}")
    except Exception as exc:
        return Check(FAIL, f"fetch_spy_close failed: {exc}")
    quote = fh.fetch_quote("AAPL")
    if quote is None or quote.get("c") is None:
        return Check(FAIL, "fetch_quote('AAPL') returned no data (paid-gated or bad response)")
    parts.append(f"quote(AAPL)={quote['c']}")
    news = fh.fetch_company_news("AAPL", today - timedelta(days=2), today)
    if not news:
        return Check(FAIL, "fetch_company_news('AAPL', -2d..today) returned [] (paid-gated?)")
    parts.append(f"company_news={len(news)} items")
    return Check(PASS, ", ".join(parts))


def check_general_news() -> Check:
    fh = _finnhub_or_none()
    if fh is None:
        return Check(WARN, SKIP_MSG)
    news = fh.fetch_general_news("general")
    if not news:
        return Check(WARN, "general_news returned [] (free-tier gated?)")
    return Check(PASS, f"general_news={len(news)} items")


PAID_PROBE_CALLS = (
    ("^GSPC", "quote"),
    ("^VIX", "quote"),
    ("insider_sentiment(AAPL)", "insider_sentiment"),
)


def check_paid_gated_probes() -> Check:
    """Probe CLAUDE.md's known-risk endpoints and report which are paid-gated.

    Informational: a gated endpoint is a WARN (expected on the free tier), not
    a failure — but scoring assumptions that depend on it stay honest.
    """
    fh = _finnhub_or_none()
    if fh is None:
        return Check(WARN, SKIP_MSG)
    client = fh._get_client()
    today = datetime.now(_ET).date()
    yesterday = today - timedelta(days=1)
    results: list[str] = []
    for name, kind in PAID_PROBE_CALLS:
        try:
            if kind == "quote":
                resp = client.quote(name)
                ok = bool(resp and resp.get("c"))
            else:
                resp = client.stock_insider_sentiment(
                    "AAPL", yesterday.isoformat(), today.isoformat()
                )
                ok = isinstance(resp, dict)
        except fh.FinnhubAPIException as exc:
            if exc.status_code in fh.PAID_TIER_STATUS_CODES:
                results.append(f"{name}: paid-gated (HTTP {exc.status_code})")
            else:
                results.append(f"{name}: HTTP {exc.status_code}")
            continue
        except Exception as exc:
            results.append(f"{name}: {exc}")
            continue
        results.append(f"{name}: ok" if ok else f"{name}: no data")
    if all(result.endswith(": ok") for result in results):
        return Check(PASS, "; ".join(results))
    return Check(WARN, "; ".join(results))


def check_thesis() -> Check:
    cfg = _config_or_none()
    if cfg is None:
        return Check(WARN, SKIP_MSG)
    path = cfg.THESIS_PATH
    try:
        thesis, _ = load_thesis(path)
    except ThesisStaleError as exc:
        return Check(FAIL, str(exc))
    except FileNotFoundError:
        return Check(FAIL, f"thesis.yaml missing at {path} — news-morning will refuse to run")
    except Exception as exc:
        return Check(FAIL, f"thesis load failed: {exc}")
    days_remaining = (thesis.review_due - datetime.now(_ET).date()).days
    if days_remaining < 14:
        return Check(
            WARN,
            f"review_due {thesis.review_due} ({days_remaining} days) — review due soon",
        )
    return Check(PASS, f"review_due {thesis.review_due} ({days_remaining} days)")


def check_holdings() -> Check:
    try:
        rows = holdings.load_holdings()
    except holdings.EmptyHoldingsError as exc:
        return Check(FAIL, str(exc))
    except Exception as exc:
        return Check(FAIL, f"holdings load failed: {exc}")
    return Check(PASS, f"{len(rows)} positions loaded")


def check_drift() -> Check:
    try:
        rows = holdings.load_holdings()
    except Exception:
        return Check(WARN, "holdings unavailable — cannot measure coverage drift")
    drift = holdings.holdings_universe_drift(holdings=rows)
    if not drift:
        return Check(PASS, "all held tickers are core-scanned")
    preview = ", ".join(drift[:10])
    if len(drift) > 10:
        preview += f"... (+{len(drift) - 10} more)"
    return Check(WARN, f"{len(drift)} held tickers not scanned: {preview}")


def check_universe() -> Check:
    if not universe.UNIVERSE_PATH.exists():
        return Check(FAIL, f"universe.csv missing at {universe.UNIVERSE_PATH}")
    core = universe.get_core_holdings()
    today_slice = universe.get_todays_universe()
    if not core:
        return Check(FAIL, "no core holdings in universe.csv — news-morning will scan nothing")
    if not today_slice:
        return Check(FAIL, "today's scan slice is empty")
    return Check(PASS, f"universe {len(today_slice)} today / {len(core)} core")


def check_k1_denylist() -> Check:
    if not universe.UNIVERSE_PATH.exists():
        return Check(WARN, "universe absent — cannot verify K-1 denylist")
    leaked = sorted(set(universe.get_all_tickers()) & set(universe.K1_DENYLIST))
    if leaked:
        return Check(FAIL, f"K-1 tickers leaked into universe: {', '.join(leaked)}")
    return Check(PASS, "K-1 denylist intact")


def check_db() -> Check:
    db_path = repository.DB_PATH
    if not db_path.exists():
        return Check(FAIL, f"signals.db missing at {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        return Check(FAIL, f"DB open failed: {exc}")
    if mode.lower() != "wal":
        return Check(FAIL, f"journal_mode={mode} (expected wal)")
    return Check(PASS, f"{count} signals, WAL on")


def check_backup() -> Check:
    if not repository.DB_PATH.exists():
        return Check(WARN, "signals.db missing — cannot check backup freshness")
    last = repository.get_latest_successful_run_datetime("backup")
    if last is None:
        return Check(WARN, "no successful backup run recorded")
    age_days = (datetime.now(_ET) - last).days
    if age_days >= 8:
        return Check(WARN, f"last backup {age_days} days ago (SLA 8)")
    return Check(PASS, f"backup {age_days} day(s) ago")


def check_jobs_liveness() -> Check:
    """Print last success (and flag never-succeeded / last-run-failed) per job.

    A lightweight, on-demand version of the Plan 01 watchdog view.
    """
    if not repository.DB_PATH.exists():
        return Check(WARN, "signals.db missing — cannot check job liveness")
    lines: list[str] = []
    problems = False
    for job in SCHEDULED_JOBS:
        last_success = repository.get_latest_successful_run_datetime(job)
        last_run = repository.get_latest_run(job)
        if last_success is None:
            problems = True
            lines.append(f"{job}: never succeeded")
        elif last_run and last_run["status"] == "failed":
            problems = True
            lines.append(
                f"{job}: last run FAILED {last_run['started_at']:%Y-%m-%d %H:%M ET} "
                f"(last success {last_success:%Y-%m-%d %H:%M ET})"
            )
        else:
            lines.append(f"{job}: {last_success:%Y-%m-%d %H:%M ET}")
    return Check(WARN if problems else PASS, "\n".join(lines))


SECTIONS: tuple[tuple[str, str, list], ...] = (
    (
        "A",
        "Secrets & config",
        [check_required_secrets, check_per_job_healthchecks, check_model_pin],
    ),
    ("B", "Anthropic", [check_anthropic]),
    (
        "C",
        "Finnhub",
        [check_finnhub_core, check_general_news, check_paid_gated_probes],
    ),
    ("D", "Thesis", [check_thesis]),
    (
        "E",
        "Coverage",
        [check_holdings, check_drift, check_universe, check_k1_denylist],
    ),
    ("F", "State", [check_db, check_backup, check_jobs_liveness]),
)


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_STATUS_COLOR = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
_RESET = "\033[0m"


def _fmt_status(status: str, color: bool) -> str:
    if not color:
        return status
    return f"{_STATUS_COLOR[status]}{status}{_RESET}"


def run_preflight(argv: list[str] | None = None) -> int:
    """Run every check, print the report, and return the worst exit code."""
    load_dotenv()
    color = _use_color()
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    print(f"SIGNAL SYSTEM PREFLIGHT — {datetime.now(_ET):%Y-%m-%d %H:%M ET}")
    for section, title, check_fns in SECTIONS:
        print(f"\n[{section}] {title}")
        for check_fn in check_fns:
            try:
                check = check_fn()
            except Exception as exc:
                check = Check(FAIL, f"unhandled error: {exc}")
            counts[check.status] += 1
            lines = check.message.splitlines() or [""]
            print(f"  {_fmt_status(check.status, color)}  {lines[0]}")
            for line in lines[1:]:
                print(f"             {line}")
    worst = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    exit_code = (
        EXIT_FAIL if counts[FAIL] else EXIT_WARN if counts[WARN] else EXIT_OK
    )
    print(
        f"\nRESULT: {worst} ({counts[FAIL]} fail, {counts[WARN]} warn)   exit={exit_code}"
    )
    return exit_code
