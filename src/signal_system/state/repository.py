"""SQLite repository for signals and runs state management.

All SQLite access in signal-system goes through this module — no raw SQL elsewhere.
Every connection is opened via _connect() which applies PRAGMA busy_timeout = 30000
to prevent "database is locked" errors under concurrent Task Scheduler runs.
"""

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from signal_system.models import Signal

DB_PATH = Path(__file__).parents[3] / "state" / "signals.db"


@dataclass(frozen=True, slots=True)
class OutcomeBackfillCandidate:
    alert_id: str
    ticker: str
    timestamp: datetime
    outcome_price_30d: float | None
    outcome_price_90d: float | None


@dataclass(frozen=True, slots=True)
class AdviceBackfillCandidate:
    advice_id: str
    ticker: str
    timestamp: datetime
    outcome_price_30d: float | None
    outcome_price_90d: float | None


def _connect() -> sqlite3.Connection:
    """Open a SQLite connection with busy_timeout to handle concurrent writes."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")  # 30-second wait if DB is locked
    return conn


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, type_def: str) -> None:
    """Idempotent ALTER TABLE — only adds the column if it does not already exist.

    SQLite does NOT support 'ALTER TABLE ADD COLUMN IF NOT EXISTS'; this helper
    uses PRAGMA table_info() to check existence first.
    """
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")


def init_db() -> None:
    """Initialize the database: create state/ directory, enable WAL mode, and create/migrate tables.

    Safe to call multiple times on an existing database — idempotent.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect()
    try:
        cursor = conn.cursor()

        # Enable WAL mode for concurrent read/write safety
        cursor.execute("PRAGMA journal_mode=WAL;")

        # Core tables — CREATE IF NOT EXISTS preserves existing rows on upgrade
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                alert_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                agent TEXT NOT NULL,
                severity TEXT NOT NULL,
                ticker TEXT,
                title TEXT NOT NULL,
                body TEXT,
                suggested_action TEXT,
                score REAL,
                acted INTEGER,
                acted_at TEXT,
                user_note TEXT,
                outcome_price_30d REAL,
                outcome_price_90d REAL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                job TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL
            )
        """)

        # Idempotent column additions to signals (Phase 1 schema extensions)
        _ensure_column(cursor, "signals", "routing_status", "TEXT")
        _ensure_column(cursor, "signals", "signal_price_snapshot", "REAL")
        _ensure_column(cursor, "signals", "model_version", "TEXT")
        _ensure_column(cursor, "signals", "thesis_version_hash", "TEXT")

        # Idempotent column additions to runs (Phase 4 schema extensions)
        _ensure_column(cursor, "runs", "tickers_scanned", "INTEGER")
        _ensure_column(cursor, "runs", "tickers_signaled", "INTEGER")

        # Idempotent column additions to signals (Phase 5 schema extensions)
        _ensure_column(cursor, "signals", "demoted_from", "TEXT")
        _ensure_column(cursor, "signals", "suppression_reason", "TEXT")

        # Advisor phase additions
        _ensure_column(cursor, "signals", "direction", "TEXT")

        # Reconciliation phase additions
        _ensure_column(cursor, "signals", "pillar", "TEXT")

        # Scoring-regime stamp for IC interpretability (Phase 2)
        _ensure_column(cursor, "signals", "weight_version", "TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS advice (
                advice_id TEXT PRIMARY KEY,
                run_id TEXT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                account TEXT,
                held INTEGER NOT NULL DEFAULT 1,
                verdict TEXT NOT NULL,
                confidence REAL NOT NULL,
                mom_axis TEXT NOT NULL,
                news_axis TEXT NOT NULL,
                factors_json TEXT NOT NULL DEFAULT '{}',
                flags TEXT NOT NULL DEFAULT '',
                rationale TEXT,
                rationale_source TEXT,
                model_version TEXT,
                thesis_version_hash TEXT,
                signal_price_snapshot REAL,
                shadow_mode INTEGER NOT NULL DEFAULT 1,
                outcome_price_30d REAL,
                outcome_price_90d REAL,
                acted INTEGER,
                acted_at TEXT,
                user_note TEXT
            )
        """)

        # New tables (Phase 1)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wash_sale (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                account TEXT NOT NULL CHECK (account IN
                    ('schwab_main', 'schwab_secondary', 'roth_ira', 'hsa')),
                trade_date TEXT NOT NULL,
                quantity REAL,
                cost_basis REAL,
                notes TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL,
                model_version TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                timestamp TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_prices (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                close REAL NOT NULL,
                source TEXT,
                PRIMARY KEY (ticker, date)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ticker_meta (
                ticker TEXT PRIMARY KEY,
                sector TEXT,
                industry TEXT,
                source TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS synthesis_reports (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                week_start TEXT NOT NULL,
                corpus_size INTEGER NOT NULL,
                themes_json TEXT NOT NULL,
                candidates_json TEXT NOT NULL,
                model_version TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()


def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,
    suppression_reason: str | None = None,
) -> bool:
    """Insert a Signal into the database using INSERT OR IGNORE semantics.

    Returns:
        True if the signal was newly inserted.
        False if a signal with the same alert_id already existed (idempotent rerun).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                score, routing_status, signal_price_snapshot, model_version,
                thesis_version_hash, demoted_from, direction, pillar,
                suppression_reason, weight_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.alert_id,
            signal.timestamp.isoformat(),
            signal.agent,
            signal.severity,
            signal.ticker,
            signal.title,
            signal.body,
            signal.score,
            routing_status,
            signal.signal_price_snapshot,
            signal.model_version,
            signal.thesis_version_hash,
            demoted_from,
            signal.direction,
            signal.pillar,
            suppression_reason,
            signal.weight_version,
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def insert_run(job: str) -> str:
    """Insert a run record. Returns the generated run_id (UUID v4)."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    status = "running"

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO runs (run_id, job, started_at, ended_at, status)
            VALUES (?, ?, ?, ?, ?)
        """, (run_id, job, started_at, None, status))
        conn.commit()
    finally:
        conn.close()

    return run_id


def update_run(run_id: str, status: str) -> None:
    """Update a run record with ended_at timestamp and status."""
    ended_at = datetime.now(ZoneInfo("America/New_York")).isoformat()

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE runs
            SET status = ?, ended_at = ?
            WHERE run_id = ?
        """, (status, ended_at, run_id))
        conn.commit()
    finally:
        conn.close()


def update_run_counts(run_id: str, tickers_scanned: int, tickers_signaled: int) -> None:
    """Write tickers_scanned and tickers_signaled counts to the runs row.

    Call once per score_universe() invocation, before returning.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE runs SET tickers_scanned = ?, tickers_signaled = ? WHERE run_id = ?",
            (tickers_scanned, tickers_signaled, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_successful_run_date(job: str) -> date | None:
    """Return the newest ET calendar date for a successful run of *job*, or None."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT started_at
            FROM runs
            WHERE job = ? AND status = 'success'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (job,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0]).astimezone(ZoneInfo("America/New_York")).date()
    finally:
        conn.close()


def insert_llm_call(
    *,
    job: str,
    model_version: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> None:
    """Log one LLM API call's token telemetry to the llm_calls table.

    All parameters are keyword-only to prevent positional-arg drift if columns change.
    Callers must coerce None cache counts to 0 before calling (use 'or 0' pattern).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO llm_calls
               (job, model_version, input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                job,
                model_version,
                input_tokens,
                output_tokens,
                cache_read_input_tokens,
                cache_creation_input_tokens,
                datetime.now(ZoneInfo("America/New_York")).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def count_delivered_today() -> dict[str, int]:
    """Return today's signal counts keyed by severity (ET timezone).

    Counts both PENDING and DELIVERED routing_status to stay conservative
    across job re-runs — a PENDING row from a failed prior run still consumes
    budget.

    Uses ISO date-prefix matching on the timestamp column — all timestamps are
    stored as ET ISO strings by convention, so LIKE 'YYYY-MM-DD%' is correct.

    Returns:
        Dict mapping severity string to count of signals today.
        Missing severities are absent from the dict (treat as 0).
        Example: {"INFORMATIONAL": 2, "ACTION_REQUIRED": 1}
    """
    today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT severity, COUNT(*) FROM signals
            WHERE routing_status IN ('PENDING', 'DELIVERED')
              AND timestamp LIKE ? || '%'
            GROUP BY severity
        """, (today_iso,))
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()


def get_previous_score(ticker: str, agent: str, before: datetime) -> float | None:
    """Return the most recent score for (ticker, agent) from a *prior calendar day*.

    Looks back across any gap — Discovery's ~3-day rotation means the "prior
    scan" is often not literally yesterday. Excludes MONITORING rows (no
    comparable score, mirrors get_recent_signals()'s exclusion).

    Rows sharing *before*'s own ET calendar date are excluded, not just rows at
    a later timestamp. Discovery's alert_id is date-stable, so a same-day rerun
    (e.g. after a crash) leaves the first attempt's row in place while the rerun
    carries a strictly later timestamp — without the date exclusion the delta
    would compare a signal against its own earlier attempt rather than against a
    genuine prior scan.

    Uses ISO date-prefix comparison on the timestamp column — all timestamps are
    stored as ET ISO strings by convention, so substr(timestamp, 1, 10) is the
    ET calendar date (same idiom as count_delivered_today()).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT score FROM signals
            WHERE ticker = ? AND agent = ?
              AND timestamp < ?
              AND substr(timestamp, 1, 10) < substr(?, 1, 10)
              AND routing_status != 'MONITORING'
              AND score IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (ticker, agent, before.isoformat(), before.isoformat()),
        )
        row = cursor.fetchone()
        return float(row[0]) if row is not None else None
    finally:
        conn.close()


def list_outcome_backfill_candidates() -> list[OutcomeBackfillCandidate]:
    """Return routable signals (DELIVERED or SUPPRESSED) still missing outcome prices.

    Measures all routable signals, not just operator-reviewed ones, so the
    quarterly IC review can assess whether ignoring a signal was correct.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, ticker, timestamp, outcome_price_30d, outcome_price_90d
            FROM signals
            WHERE routing_status IN ('DELIVERED', 'SUPPRESSED')
              AND ticker IS NOT NULL
              AND ticker != ''
              AND (outcome_price_30d IS NULL OR outcome_price_90d IS NULL)
            ORDER BY timestamp ASC
            """
        )
        return [
            OutcomeBackfillCandidate(
                alert_id=row[0],
                ticker=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                outcome_price_30d=row[3],
                outcome_price_90d=row[4],
            )
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def update_signal_outcomes(
    alert_id: str,
    *,
    outcome_price_30d: float | None = None,
    outcome_price_90d: float | None = None,
) -> None:
    """Persist deferred outcome prices without overwriting existing values."""
    if outcome_price_30d is None and outcome_price_90d is None:
        return

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE signals
            SET outcome_price_30d = CASE
                    WHEN ? IS NOT NULL AND outcome_price_30d IS NULL THEN ?
                    ELSE outcome_price_30d
                END,
                outcome_price_90d = CASE
                    WHEN ? IS NOT NULL AND outcome_price_90d IS NULL THEN ?
                    ELSE outcome_price_90d
                END
            WHERE alert_id = ?
            """,
            (
                outcome_price_30d,
                outcome_price_30d,
                outcome_price_90d,
                outcome_price_90d,
                alert_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def insert_advice(row: dict) -> bool:
    """Insert one advice row using INSERT OR IGNORE. Returns True if newly inserted."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO advice (
                advice_id, run_id, timestamp, ticker, account, held,
                verdict, confidence, mom_axis, news_axis, factors_json, flags,
                rationale, rationale_source, model_version, thesis_version_hash,
                signal_price_snapshot, shadow_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["advice_id"],
            row.get("run_id"),
            row["timestamp"],
            row["ticker"],
            row.get("account"),
            int(bool(row.get("held", True))),
            row["verdict"],
            row["confidence"],
            row["mom_axis"],
            row["news_axis"],
            row.get("factors_json", "{}"),
            row.get("flags", ""),
            row.get("rationale"),
            row.get("rationale_source"),
            row.get("model_version"),
            row.get("thesis_version_hash"),
            row.get("signal_price_snapshot"),
            int(bool(row.get("shadow_mode", True))),
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def get_recent_signals(
    ticker: str,
    since: date,
    agent: str | None = None,
) -> list[tuple[float, float]]:
    """Return (direction_scalar, confidence) pairs for recent on-thesis signals.

    direction_scalar: +1.0 (positive), -1.0 (negative), 0.0 (neutral or unknown).
    Excludes signals with routing_status='MONITORING' (parse failures, off-thesis
    exhaust, reconciled losers, margin-guard downgrades).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        query = """
            SELECT score, direction
            FROM signals
            WHERE ticker = ?
              AND timestamp >= ?
              AND routing_status != 'MONITORING'
        """
        params: list = [ticker, since.isoformat()]
        if agent is not None:
            query += " AND agent = ?"
            params.append(agent)
        cursor.execute(query, params)
        rows = cursor.fetchall()
    finally:
        conn.close()

    _DIR = {"positive": 1.0, "negative": -1.0}
    results = []
    for score, direction in rows:
        if score is None:
            continue
        dir_scalar = _DIR.get(direction or "", 0.0)
        results.append((dir_scalar, float(score)))
    return results


def get_monitoring_signals_since(
    since: date,
    *,
    title_prefixes: tuple[str, ...] = ("[off_thesis]", "[volume_cap]"),
    limit: int = 500,
) -> list[dict]:
    """Return recent MONITORING signals whose title starts with a given prefix.

    The MONITORING corpus is the audit trail for off-thesis classifications,
    volume-cap overflow, and parse failures. The weekly synthesis job reads
    this to find new pillar candidates.

    `[` is not a SQLite LIKE wildcard, so prefix filters are safe as-is.
    Returns newest-first rows; each row is a sqlite3.Row-like dict with the
    full signals columns plus routing_status.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        query = f"""
            SELECT alert_id, timestamp, agent, severity, ticker, title, body,
                   score, routing_status, direction, pillar
            FROM signals
            WHERE routing_status = 'MONITORING'
              AND timestamp >= ?
              AND ({" OR ".join(f"title LIKE ? || '%'" for _ in title_prefixes)})
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params: list = [since.isoformat()]
        params.extend(title_prefixes)
        params.append(limit)
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_pillar_routing_counts_since(since: date) -> dict[str, dict[str, int]]:
    """Return {pillar: {DELIVERED: n, SUPPRESSED: n}} for routed signals since *since*.

    Trusted context for the weekly synthesis job — the operator's per-pillar
    delivery record is real ground truth, distinct from the untrusted MONITORING
    corpus the LLM must not treat as instructions.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT pillar, routing_status, COUNT(*)
            FROM signals
            WHERE pillar IS NOT NULL
              AND routing_status IN ('DELIVERED', 'SUPPRESSED')
              AND timestamp >= ?
            GROUP BY pillar, routing_status
            """,
            (since.isoformat(),),
        )
        result: dict[str, dict[str, int]] = {}
        for pillar, status, count in cursor.fetchall():
            result.setdefault(pillar, {"DELIVERED": 0, "SUPPRESSED": 0})[status] = count
        return result
    finally:
        conn.close()


def get_agent_signals_since(agent: str, since: date) -> list[dict]:
    """Return recent signals for *agent* since *since* (newest first)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, body,
                   score, routing_status, direction, pillar
            FROM signals
            WHERE agent = ?
              AND timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (agent, since.isoformat()),
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def insert_synthesis_report(
    *,
    run_id: str,
    week_start: str,
    corpus_size: int,
    themes_json: str,
    candidates_json: str,
    model_version: str,
) -> None:
    """Record one weekly-synthesis run (INSERT OR REPLACE for idempotent reruns)."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO synthesis_reports (
                run_id, timestamp, week_start, corpus_size,
                themes_json, candidates_json, model_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                datetime.now(ZoneInfo("America/New_York")).isoformat(),
                week_start,
                corpus_size,
                themes_json,
                candidates_json,
                model_version,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_benchmark_price(*, ticker: str, date_iso: str, close: float, source: str) -> None:
    """Insert or replace a benchmark close for (ticker, date).

    INSERT OR REPLACE lets the yfinance history backfill overwrite quote-sourced
    rows when the backfill runs after daily-close has written the same date.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO benchmark_prices (ticker, date, close, source)
            VALUES (?, ?, ?, ?)
            """,
            (ticker, date_iso, close, source),
        )
        conn.commit()
    finally:
        conn.close()


def get_benchmark_close_on_or_after(ticker: str, date_iso: str) -> float | None:
    """Return the benchmark close for the first stored trading day >= *date_iso*.

    Mirrors the outcome-backfill convention (first trading day at or after the
    target) so excess-return math is consistent with signal outcome prices.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT close FROM benchmark_prices
            WHERE ticker = ? AND date >= ?
            ORDER BY date ASC
            LIMIT 1
            """,
            (ticker, date_iso),
        )
        row = cursor.fetchone()
        return float(row[0]) if row is not None else None
    finally:
        conn.close()


def get_earliest_signal_date() -> date | None:
    """Return the earliest signal timestamp's ET date, or None on an empty table."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(timestamp) FROM signals")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return (
            datetime.fromisoformat(row[0])
            .astimezone(ZoneInfo("America/New_York"))
            .date()
        )
    finally:
        conn.close()


def upsert_ticker_meta(
    *, ticker: str, sector: str | None, industry: str | None, source: str
) -> None:
    """Insert or replace a ticker's sector/industry enrichment row."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO ticker_meta (ticker, sector, industry, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticker, sector, industry, source, datetime.now(ZoneInfo("America/New_York")).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_ticker_meta(ticker: str) -> dict | None:
    """Return the cached sector/industry row for *ticker*, or None."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ticker, sector, industry, source, updated_at FROM ticker_meta WHERE ticker = ?",
            (ticker,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "ticker": row[0],
            "sector": row[1],
            "industry": row[2],
            "source": row[3],
            "updated_at": row[4],
        }
    finally:
        conn.close()


def find_signals_by_alert_prefix(prefix: str) -> list[dict]:
    """Return DELIVERED signals whose alert_id starts with *prefix*.

    Raises ValueError for non-hex or out-of-range prefixes (CLI ambiguity guard
    depends on this). Returns all matching rows so the caller can detect
    ambiguity (>1) or no-match (0).
    """
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", prefix):
        raise ValueError(
            f"alert_id prefix must be 4-64 hex chars, got {prefix!r}"
        )
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, acted, acted_at
            FROM signals
            WHERE alert_id LIKE ? || '%'
            ORDER BY timestamp DESC
            """,
            (prefix,),
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_signal_acted(
    alert_id: str, *, acted: bool, user_note: str | None = None
) -> None:
    """Record the operator's acted/passed decision on a signal.

    Stamps acted_at with the current ET time. acted=True → 1, acted=False → 0.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE signals
            SET acted = ?, acted_at = ?, user_note = COALESCE(?, user_note)
            WHERE alert_id = ?
            """,
            (
                int(acted),
                datetime.now(ZoneInfo("America/New_York")).isoformat(),
                user_note,
                alert_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent_delivered_signals(
    days: int = 14, *, now: datetime | None = None
) -> list[dict]:
    """Return DELIVERED signals from the last *days* (ET), newest first."""
    effective_now = now or datetime.now(ZoneInfo("America/New_York"))
    cutoff = (effective_now.astimezone(ZoneInfo("America/New_York")) - timedelta(days=days)).isoformat()
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, score,
                   acted, acted_at, routing_status
            FROM signals
            WHERE routing_status = 'DELIVERED'
              AND timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (cutoff,),
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def count_unacted_delivered_older_than(
    days: int = 7, *, now: datetime | None = None
) -> int:
    """Count DELIVERED signals older than *days* (ET) with acted IS NULL."""
    effective_now = now or datetime.now(ZoneInfo("America/New_York"))
    cutoff = (effective_now.astimezone(ZoneInfo("America/New_York")) - timedelta(days=days)).isoformat()
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE routing_status = 'DELIVERED'
              AND acted IS NULL
              AND timestamp < ?
            """,
            (cutoff,),
        )
        row = cursor.fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def list_signals_for_review() -> list[dict]:
    """Return routable signals with price snapshots for the review report.

    Agents discovery_agent/news_classifier, routing DELIVERED/SUPPRESSED,
    signal_price_snapshot NOT NULL (excludes legacy DAILY_CLOSE rows by
    construction since they carry no snapshot). Excludes MONITORING corpus.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, score,
                   routing_status, signal_price_snapshot, outcome_price_30d,
                   outcome_price_90d, direction, acted, weight_version
            FROM signals
            WHERE agent IN ('discovery_agent', 'news_classifier')
              AND routing_status IN ('DELIVERED', 'SUPPRESSED')
              AND signal_price_snapshot IS NOT NULL
              AND signal_price_snapshot > 0
            ORDER BY timestamp ASC
            """
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_llm_call_summary() -> list[dict]:
    """Aggregate llm_calls per job: call count and token sums."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT job,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens
            FROM llm_calls
            GROUP BY job
            ORDER BY job
            """
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_routable_discovery_signals(
    since: date,
    *,
    excluded_tickers: set[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent routable (DELIVERED or SUPPRESSED) discovery_agent signals by score desc.

    Includes SUPPRESSED so the daily delivery budget can't starve analysis —
    the advisor's new-buy funnel should see every top scorer, not just the few
    the router delivered that day.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ticker, score, timestamp
            FROM signals
            WHERE agent = 'discovery_agent'
              AND routing_status IN ('DELIVERED', 'SUPPRESSED')
              AND timestamp >= ?
            ORDER BY score DESC, timestamp DESC
            LIMIT ?
        """, (since.isoformat(), limit))
        rows = cursor.fetchall()
    finally:
        conn.close()

    excluded = excluded_tickers or set()
    return [
        {"ticker": row[0], "score": row[1]}
        for row in rows
        if row[0] and row[0] not in excluded
    ]


def list_advice_backfill_candidates() -> list[AdviceBackfillCandidate]:
    """Return advice rows still missing at least one deferred outcome price.

    Measures all advice rows, not just operator-reviewed ones, so the
    quarterly review can assess every verdict's outcome.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT advice_id, ticker, timestamp, outcome_price_30d, outcome_price_90d
            FROM advice
            WHERE ticker IS NOT NULL
              AND ticker != ''
              AND (outcome_price_30d IS NULL OR outcome_price_90d IS NULL)
            ORDER BY timestamp ASC
        """)
        return [
            AdviceBackfillCandidate(
                advice_id=row[0],
                ticker=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                outcome_price_30d=row[3],
                outcome_price_90d=row[4],
            )
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def update_advice_outcomes(
    advice_id: str,
    *,
    outcome_price_30d: float | None = None,
    outcome_price_90d: float | None = None,
) -> None:
    """Persist deferred outcome prices for an advice row without overwriting existing values."""
    if outcome_price_30d is None and outcome_price_90d is None:
        return

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE advice
            SET outcome_price_30d = CASE
                    WHEN ? IS NOT NULL AND outcome_price_30d IS NULL THEN ?
                    ELSE outcome_price_30d
                END,
                outcome_price_90d = CASE
                    WHEN ? IS NOT NULL AND outcome_price_90d IS NULL THEN ?
                    ELSE outcome_price_90d
                END
            WHERE advice_id = ?
            """,
            (
                outcome_price_30d, outcome_price_30d,
                outcome_price_90d, outcome_price_90d,
                advice_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_routing_status(alert_id: str, routing_status: str) -> None:
    """Flip the routing_status of an existing signal row.

    Used to transition PENDING → DELIVERED after a successful Telegram send.
    Only modifies rows where the alert_id exists — no error on missing rows.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET routing_status = ? WHERE alert_id = ?",
            (routing_status, alert_id),
        )
        conn.commit()
    finally:
        conn.close()
