"""SQLite repository for signals and runs state management.

All SQLite access in signal-system goes through this module — no raw SQL elsewhere.
Every connection is opened via _connect() which applies PRAGMA busy_timeout = 30000
to prevent "database is locked" errors under concurrent Task Scheduler runs.
"""

import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from signal_system.models import Signal

DB_PATH = Path(__file__).parents[3] / "state" / "signals.db"


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

        conn.commit()
    finally:
        conn.close()


def insert_signal(
    signal: Signal,
    routing_status: str | None = None,
    demoted_from: str | None = None,
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
                thesis_version_hash, demoted_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            demoted_from,               # Phase 5 addition (D-11)
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
    """Return today's DELIVERED signal counts keyed by severity (ET timezone).

    Uses ISO date-prefix matching on the timestamp column — all timestamps are
    stored as ET ISO strings by convention, so LIKE 'YYYY-MM-DD%' is correct.

    Returns:
        Dict mapping severity string to count of DELIVERED signals today.
        Missing severities are absent from the dict (treat as 0).
        Example: {"INFORMATIONAL": 2, "ACTION_REQUIRED": 1}
    """
    today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT severity, COUNT(*) FROM signals
            WHERE routing_status = 'DELIVERED'
              AND timestamp LIKE ? || '%'
            GROUP BY severity
        """, (today_iso,))
        return {row[0]: row[1] for row in cursor.fetchall()}
    finally:
        conn.close()
