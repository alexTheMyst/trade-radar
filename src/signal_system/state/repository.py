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

from signal_system.models import NEWS_CLASSIFIER_AGENT, ROUTING_REGIME, Signal

DB_PATH = Path(__file__).parents[3] / "state" / "signals.db"

@dataclass(frozen=True, slots=True)
class OutcomeBackfillCandidate:
    alert_id: str
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


def _backfill_monitoring_reason(cursor: sqlite3.Cursor) -> int:
    """Derive monitoring_reason for rows written before the column existed.

    Without this, adding the column leaves every historical MONITORING row NULL,
    and two things break:

      1. weekly-synthesis filters on `monitoring_reason IN (...)`, so the entire
         pre-migration corpus — the "new-idea raw material" — becomes invisible.
      2. count_monitoring_by_reason() buckets NULL under '', which no digest
         breakdown contains, so any job rerunning on a date that already holds
         legacy rows fails digest validation and refuses to send.

    The old taxonomy was carried in the title prefix, plus demoted_from for
    reconciled losers. Unprefixed MONITORING rows are exactly the pillar-matched
    below-threshold ones this plan adds a bucket for, so they are the default.

    Idempotent: only touches rows still NULL. Returns rows updated.
    """
    cursor.execute(
        """
        UPDATE signals
           SET monitoring_reason = CASE
                 WHEN title LIKE '[off_thesis]%'    THEN 'off_thesis'
                 WHEN title LIKE '[volume_cap]%'    THEN 'volume_cap'
                 WHEN title LIKE '[parse_failure]%' THEN 'parse_failure'
                 WHEN title LIKE '[cooldown]%'      THEN 'cooldown'
                 WHEN title LIKE '[repeat]%'        THEN 'repeat'
                 WHEN demoted_from = 'reconciled'   THEN 'reconciled'
                 ELSE 'below_threshold'
               END
         WHERE routing_status = 'MONITORING'
           AND monitoring_reason IS NULL
        """
    )
    return cursor.rowcount


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

        # Direction attribution (news / sentinel)
        _ensure_column(cursor, "signals", "direction", "TEXT")

        # Reconciliation phase additions
        _ensure_column(cursor, "signals", "pillar", "TEXT")

        # Scoring-regime stamp for IC interpretability (Phase 2)
        _ensure_column(cursor, "signals", "weight_version", "TEXT")

        # MONITORING-reason classification (Plan 08) — the column is the source
        # of truth for digest counting and the weekly-synthesis corpus, replacing
        # title-prefix string matching.
        _ensure_column(cursor, "signals", "monitoring_reason", "TEXT")
        _backfill_monitoring_reason(cursor)

        # Routing-regime stamp (Plan 06, Option D). NULL = legacy first-come
        # routing; never backfilled.
        _ensure_column(cursor, "signals", "routing_regime", "TEXT")

        # Sentinel trigger magnitudes as JSON (Plan 12a2). NULL for every other
        # agent; drives the reserved INFO slot and unclamped ranking.
        _ensure_column(cursor, "signals", "trigger_json", "TEXT")

        # DEPRECATED (Plan 04, executed via Plan 12e): the Advisor was removed;
        # historical shadow rows are retained read-only for the record.
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
        # DEPRECATED (Plan 05, 2026-10): the manual wash-sale ledger is retired —
        # Schwab is the system of record. The table is left in place for schema
        # stability and the weekly backup; no code writes to it.
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

        # Wash-sale ledger: `side` (buy/sell) and per-share trade price.
        # cost_basis is reused as the per-share trade price — the table's
        # original column name is retained to keep the schema migration simple;
        # it is documented as price-per-share, not total cost.
        _ensure_column(cursor, "wash_sale", "side", "TEXT")

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

        # Telegram action-logging (Plan 12e). One row per button message sent
        # for an alert; kind='review_prompt' rows are Friday re-prompts.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_messages (
                chat_id     TEXT NOT NULL,
                message_id  INTEGER,
                alert_id    TEXT NOT NULL,
                kind        TEXT NOT NULL,
                status      TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                answered_at TEXT,
                PRIMARY KEY (chat_id, alert_id, kind, sent_at)
            )
        """)

        # Small key/value store for the Telegram getUpdates offset.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
    monitoring_reason: str | None = None,
    routing_regime: str | None = ROUTING_REGIME,
) -> bool:
    """Insert a Signal into the database using INSERT OR IGNORE semantics.

    Every insert is stamped with ROUTING_REGIME by default — including
    MONITORING paths, which do not travel through the router. Pass
    ``routing_regime=None`` only for a deliberate legacy replay.

    Returns:
        True if the signal was newly inserted.
        False if a signal with the same alert_id already existed (idempotent rerun).
    """
    effective_reason = (
        monitoring_reason if monitoring_reason is not None else signal.monitoring_reason
    )
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                score, routing_status, signal_price_snapshot, model_version,
                thesis_version_hash, demoted_from, direction, pillar,
                suppression_reason, weight_version, monitoring_reason,
                routing_regime, trigger_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            effective_reason,
            routing_regime,
            signal.trigger_json,
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


def get_latest_successful_run_datetime(job: str) -> datetime | None:
    """Return the ET-aware datetime of the most recent status='success' run, or None."""
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
        return (
            datetime.fromisoformat(row[0]).astimezone(ZoneInfo("America/New_York"))
        )
    finally:
        conn.close()


def get_latest_run(job: str) -> dict | None:
    """Return the most recent run row for *job*, or None.

    Returns ``{'started_at': datetime (ET-aware), 'status': str}``. Used by the
    watchdog to flag a job whose most recent run failed even when an older
    success keeps the age-based SLA green.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT started_at, status
            FROM runs
            WHERE job = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (job,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "started_at": datetime.fromisoformat(row[0]).astimezone(
                ZoneInfo("America/New_York")
            ),
            "status": row[1],
        }
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


def count_delivered_today(agent: str | None = None) -> dict[str, int]:
    """Return today's signal counts keyed by severity (ET timezone).

    Counts both PENDING and DELIVERED routing_status to stay conservative
    across job re-runs — a PENDING row from a failed prior run still consumes
    budget. When *agent* is given, only that agent's rows are counted, which is
    what per-agent ACTION_REQUIRED lanes need (Plan 12a).

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
        query = """
            SELECT severity, COUNT(*) FROM signals
            WHERE routing_status IN ('PENDING', 'DELIVERED')
              AND timestamp LIKE ? || '%'
        """
        params: list = [today_iso]
        if agent is not None:
            query += " AND agent = ?"
            params.append(agent)
        query += " GROUP BY severity"
        cursor.execute(query, params)
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
    monitoring_reasons: tuple[str, ...] = ("off_thesis", "volume_cap"),
    limit: int = 500,
) -> list[dict]:
    """Return recent MONITORING signals whose monitoring_reason is in *monitoring_reasons*.

    The MONITORING corpus is the audit trail for off-thesis classifications,
    volume-cap overflow, and parse failures. The weekly synthesis job reads
    this to find new pillar candidates. monitoring_reason is the column stamped
    at insert time — the source of truth, not title-prefix string matching.
    Returns newest-first rows; each row is a dict with the full signals columns
    plus routing_status and monitoring_reason.
    """
    if not monitoring_reasons:
        return []
    conn = _connect()
    try:
        cursor = conn.cursor()
        query = f"""
            SELECT alert_id, timestamp, agent, severity, ticker, title, body,
                   score, routing_status, direction, pillar, monitoring_reason
            FROM signals
            WHERE routing_status = 'MONITORING'
              AND timestamp >= ?
              AND monitoring_reason IN ({", ".join("?" for _ in monitoring_reasons)})
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params: list = [since.isoformat()]
        params.extend(monitoring_reasons)
        params.append(limit)
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def count_signals_by_routing_status(since: datetime, agent: str) -> dict[str, int]:
    """Count an agent's signals written at or after *since*, keyed by routing_status.

    PENDING and DELIVERED are merged under DELIVERED: the digest reports a run's
    to-be-delivered signals, which sit in PENDING at validation time and are only
    flipped to DELIVERED after a successful send. Scoped by *agent* so a job's
    digest is validated against its own rows — not the whole day's cross-job
    traffic (discovery/sentinel run after news-morning on the same ET date).

    *since* is the run's start instant, NOT the calendar date. Scoping to the run
    is what makes a same-day rerun survivable: the earlier attempt's rows fall
    outside the window, so the rerun's zero inserts match its zero-count digest
    instead of colliding with rows it did not write. Day-scoping would fail every
    rerun after a run that persisted before dying, which is the common case —
    persistence happens before the digest is sent.

    Callers pass an ET-aware datetime; timestamps are stored ISO-8601 in
    America/New_York, so lexicographic comparison is ordering-correct.

    Returns only the statuses present (treat a missing key as 0).
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT routing_status, COUNT(*)
            FROM signals
            WHERE agent = ?
              AND timestamp >= ?
            GROUP BY routing_status
            """,
            (agent, since.isoformat()),
        )
        counts: dict[str, int] = {}
        for status, count in cursor.fetchall():
            key = "DELIVERED" if status in ("PENDING", "DELIVERED") else status
            counts[key] = counts.get(key, 0) + count
        return counts
    finally:
        conn.close()


def count_monitoring_by_reason(since: datetime, agent: str) -> dict[str, int]:
    """Count an agent's MONITORING rows at or after *since*, by monitoring_reason.

    Run-scoped for the same reason as count_signals_by_routing_status — see that
    docstring. Rows with a NULL monitoring_reason group under the empty-string
    key so an unreasoned row is visible to digest validation instead of silently
    vanishing; init_db backfills historical rows so '' should never appear.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(monitoring_reason, ''), COUNT(*)
            FROM signals
            WHERE agent = ?
              AND routing_status = 'MONITORING'
              AND timestamp >= ?
            GROUP BY monitoring_reason
            """,
            (agent, since.isoformat()),
        )
        return {reason: count for reason, count in cursor.fetchall()}
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
              AND routing_status = 'DELIVERED'
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

    Agents discovery_agent/news_classifier/exit_sentinel, routing
    DELIVERED/SUPPRESSED, signal_price_snapshot NOT NULL (excludes legacy
    DAILY_CLOSE rows by construction since they carry no snapshot). Excludes
    MONITORING corpus.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, score,
                   routing_status, signal_price_snapshot, outcome_price_30d,
                   outcome_price_90d, direction, acted, weight_version, user_note
            FROM signals
            WHERE agent IN ('discovery_agent', 'news_classifier', 'exit_sentinel')
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


def count_signals() -> int:
    """Return the total number of rows in the signals table."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signals")
        row = cursor.fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def backup_database(dest_dir: Path) -> tuple[Path, int]:
    """Back up the live database to a dated signals-YYYYMMDD.db file.

    Uses ``VACUUM INTO`` rather than a plain file copy: in WAL mode an active
    -wal file can hold committed pages a raw copy would miss, and copying the
    -wal alongside the main file can hand back an inconsistent snapshot.
    VACUUM INTO reads a consistent logical snapshot including WAL content.

    The destination file is named with the ET date (``signals-YYYYMMDD.db``).
    VACUUM INTO refuses to overwrite, so any same-day file is unlinked first
    (a same-day rerun replaces yesterday's backup — the weekly schedule makes
    collision between genuinely different days impossible).

    Returns (path, row_count) where row_count is the sanity-checked COUNT(*)
    from the backup file itself — the caller should compare it against
    count_signals() on the live database.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    date_stamp = datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y%m%d")
    target = dest_dir / f"signals-{date_stamp}.db"
    target.unlink(missing_ok=True)

    conn = _connect()
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()

    backup_conn = sqlite3.connect(target)
    try:
        row = backup_conn.execute("SELECT COUNT(*) FROM signals").fetchone()
        count = int(row[0]) if row is not None else 0
    finally:
        backup_conn.close()

    return target, count


def get_signal_meta_by_alert_ids(alert_ids: list[str]) -> list[dict]:
    """Return routing-relevant metadata for the given alert_ids.

    Returns ``{alert_id, severity, demoted_from, routing_status, timestamp}``
    for each existing alert_id, excluding MONITORING rows (they bypass the
    router and must not suppress a fresh trigger). Empty input returns [].
    """
    if not alert_ids:
        return []
    placeholders = ", ".join("?" for _ in alert_ids)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT alert_id, severity, demoted_from, routing_status, timestamp
            FROM signals
            WHERE alert_id IN ({placeholders})
              AND routing_status != 'MONITORING'
            """,
            alert_ids,
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


# --- Deferred INFO dispatch (Plan 06, Option D) ------------------------------

_CANDIDATE_COLUMNS = """
    alert_id, timestamp, agent, severity, ticker, title, body, score,
    model_version, thesis_version_hash, signal_price_snapshot, direction,
    pillar, weight_version, monitoring_reason, demoted_from, trigger_json
"""


def _row_to_candidate(row: tuple) -> tuple[Signal, str | None]:
    signal = Signal(
        ticker=row[4],
        score=row[7],
        severity=row[3],
        agent=row[2],
        timestamp=datetime.fromisoformat(row[1]),
        alert_id=row[0],
        title=row[5],
        body=row[6],
        model_version=row[8],
        thesis_version_hash=row[9],
        signal_price_snapshot=row[10],
        direction=row[11],
        pillar=row[12],
        weight_version=row[13],
        monitoring_reason=row[14],
        trigger_json=row[16],
    )
    return signal, row[15]


def list_unconfirmed_since(cutoff: datetime) -> list[tuple[Signal, str | None]]:
    """Return (signal, demoted_from) for PENDING rows stamped at/after *cutoff* (ET-aware).

    A PENDING row is a delivered signal whose Telegram send was never
    confirmed. INFORMATIONAL rows are dispatch winners (staging never delivers
    INFO); ACTION_REQUIRED rows are lane winners from news-morning or sentinel.
    No job's rerun re-sends its own PENDING rows (INSERT OR IGNORE drops them
    from the to-deliver list), so the telegram-poll sweep re-sends them.
    """
    return _list_pending("timestamp >= ?", cutoff)


def list_unconfirmed_before(cutoff: datetime) -> list[tuple[Signal, str | None]]:
    """Return (signal, demoted_from) for PENDING rows stamped before *cutoff* (ET-aware).

    These are past the re-send window: re-sending a days-old alert as if it
    were fresh would mislead, so the sweep closes them out instead.
    """
    return _list_pending("timestamp < ?", cutoff)


def _list_pending(
    timestamp_clause: str, cutoff: datetime
) -> list[tuple[Signal, str | None]]:
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM signals
            WHERE routing_status = 'PENDING'
              AND {timestamp_clause}
            ORDER BY timestamp ASC, alert_id ASC
            """,
            (cutoff.isoformat(),),
        )
        return [_row_to_candidate(row) for row in cursor.fetchall()]
    finally:
        conn.close()


SEND_FAILED_REASON = "send_failed"


def mark_unconfirmed_send_failed(alert_ids: list[str]) -> int:
    """Close out PENDING rows that never reached the operator.

    Sets routing_status='SUPPRESSED', suppression_reason='send_failed' (the
    stored demoted_from is kept). Only rows still PENDING are touched, so a row
    confirmed in the meantime is never overwritten. Returns rows updated.
    """
    if not alert_ids:
        return 0
    placeholders = ", ".join("?" for _ in alert_ids)
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE signals
               SET routing_status = 'SUPPRESSED', suppression_reason = ?
             WHERE routing_status = 'PENDING'
               AND alert_id IN ({placeholders})
            """,
            [SEND_FAILED_REASON, *alert_ids],
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def confirm_unconfirmed_delivered(alert_id: str) -> bool:
    """Flip one PENDING row to DELIVERED after a successful re-send.

    Guarded on PENDING so the sweep can never resurrect a row that was closed
    out or already confirmed. Returns True when the row was flipped.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE signals SET routing_status = 'DELIVERED'
             WHERE alert_id = ? AND routing_status = 'PENDING'
            """,
            (alert_id,),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def list_candidates_for_date(date_iso: str) -> list[tuple[Signal, str | None]]:
    """Return (signal, demoted_from) for every CANDIDATE row stamped on *date_iso* (ET).

    Uses the same ISO date-prefix convention as count_delivered_today().
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM signals
            WHERE routing_status = 'CANDIDATE'
              AND timestamp LIKE ? || '%'
            ORDER BY timestamp ASC, alert_id ASC
            """,
            (date_iso,),
        )
        return [_row_to_candidate(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def list_candidates_before_date(date_iso: str) -> list[tuple[Signal, str | None]]:
    """Return CANDIDATE rows stamped before *date_iso* (ET): dispatch never ran for them."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM signals
            WHERE routing_status = 'CANDIDATE'
              AND timestamp < ?
            ORDER BY timestamp ASC, alert_id ASC
            """,
            (date_iso,),
        )
        return [_row_to_candidate(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def mark_candidates_routed(
    outcomes: list[tuple[str, str, str | None, str | None]],
) -> int:
    """Transition CANDIDATE rows to their dispatch outcome in ONE transaction.

    *outcomes* are (alert_id, routing_status, demoted_from, suppression_reason).
    Only rows still in CANDIDATE are touched, so a concurrent or repeated
    dispatch cannot re-route an already-routed row. Returns the number of rows
    updated; the caller treats a shortfall as a failure.
    """
    if not outcomes:
        return 0
    conn = _connect()
    try:
        cursor = conn.cursor()
        updated = 0
        for alert_id, routing_status, demoted_from, suppression_reason in outcomes:
            cursor.execute(
                """
                UPDATE signals
                   SET routing_status = ?, demoted_from = ?, suppression_reason = ?
                 WHERE alert_id = ? AND routing_status = 'CANDIDATE'
                """,
                (routing_status, demoted_from, suppression_reason, alert_id),
            )
            updated += cursor.rowcount
        if updated != len(outcomes):
            conn.rollback()
            return updated
        conn.commit()
        return updated
    finally:
        conn.close()


def count_routing_status_for_alert_ids(alert_ids: list[str]) -> dict[str, int]:
    """Read back routing_status counts for exactly these rows (PENDING merged into DELIVERED).

    Scoped by alert_id rather than by time so a dispatch rerun validates its own
    batch, never an earlier attempt's rows.
    """
    if not alert_ids:
        return {}
    conn = _connect()
    try:
        cursor = conn.cursor()
        counts: dict[str, int] = {}
        # Chunk to stay under SQLite's host-parameter limit.
        for start in range(0, len(alert_ids), 500):
            chunk = alert_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT routing_status, COUNT(*)
                FROM signals
                WHERE alert_id IN ({placeholders})
                GROUP BY routing_status
                """,
                chunk,
            )
            for status, count in cursor.fetchall():
                key = "DELIVERED" if status in ("PENDING", "DELIVERED") else status
                counts[key] = counts.get(key, 0) + count
        return counts
    finally:
        conn.close()


def get_oldest_candidate_timestamp() -> datetime | None:
    """Return the timestamp of the oldest CANDIDATE row, or None if there are none."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MIN(timestamp), COUNT(*) FROM signals WHERE routing_status = 'CANDIDATE'"
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])
    finally:
        conn.close()


def count_candidates_older_than(cutoff: datetime) -> int:
    """Count CANDIDATE rows stamped before *cutoff* (an ET-aware datetime)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM signals WHERE routing_status = 'CANDIDATE' AND timestamp < ?",
            (cutoff.isoformat(),),
        )
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def get_oldest_pending_timestamp() -> datetime | None:
    """Return the timestamp of the oldest PENDING row, or None if there are none."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MIN(timestamp), COUNT(*) FROM signals WHERE routing_status = 'PENDING'"
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])
    finally:
        conn.close()


def count_pending_older_than(cutoff: datetime) -> int:
    """Count PENDING rows stamped before *cutoff* (an ET-aware datetime).

    A PENDING row is a delivered signal whose Telegram send was never confirmed
    (the job died after persist, before confirm_delivered_signals). Older than a
    few hours it is stuck: the alert was never seen and must be surfaced.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM signals WHERE routing_status = 'PENDING' AND timestamp < ?",
            (cutoff.isoformat(),),
        )
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def list_recent_delivered_news(
    ticker: str,
    since: date,
    *,
    agent: str = NEWS_CLASSIFIER_AGENT,
) -> list[dict]:
    """Return recent delivered news rows for *ticker* since *since*.

    Used by the cross-day repeat-story filter (Plan 12c3). Scoped to the news
    agent so a sentinel title can never look like a news repeat. The caller
    filters out this run's own alert_ids — a failed-send rerun must not match
    its earlier PENDING copy and suppress itself as a repeat.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, title FROM signals
            WHERE ticker = ?
              AND agent = ?
              AND routing_status IN ('DELIVERED', 'PENDING')
              AND timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (ticker, agent, since.isoformat()),
        )
        return [{"alert_id": row[0], "title": row[1]} for row in cursor.fetchall()]
    finally:
        conn.close()


def list_suppressed_for_date(
    date_iso: str, agent: str
) -> list[tuple[Signal, str | None, str | None]]:
    """Return (signal, demoted_from, suppression_reason) for *agent*'s SUPPRESSED rows.

    Scoped to the ET date. Dispatch uses this to render the complete "exits not
    delivered" section — including rows suppressed by an earlier run of the same
    day, so a failed-send rerun still produces a truthful digest.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {_CANDIDATE_COLUMNS}, suppression_reason
            FROM signals
            WHERE routing_status = 'SUPPRESSED'
              AND agent = ?
              AND timestamp LIKE ? || '%'
            ORDER BY timestamp ASC, alert_id ASC
            """,
            (agent, date_iso),
        )
        return [
            (*_row_to_candidate(row[:17]), row[17])
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def count_routing_status_for_agent_on_date(
    date_iso: str, agent: str, routing_status: str
) -> int:
    """Count *agent* rows with *routing_status* stamped on *date_iso* (ET)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE agent = ?
              AND routing_status = ?
              AND timestamp LIKE ? || '%'
            """,
            (agent, routing_status, date_iso),
        )
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def count_suppressed_for_date(date_iso: str, agent: str) -> int:
    """Count SUPPRESSED rows for *agent* on *date_iso* (ET)."""
    return count_routing_status_for_agent_on_date(date_iso, agent, "SUPPRESSED")


def count_shadow_signals_for_date(date_iso: str) -> int:
    """Count discovery rows persisted as suppression_reason='shadow' today."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE agent = 'discovery_agent'
              AND routing_status = 'SUPPRESSED'
              AND suppression_reason = 'shadow'
              AND timestamp LIKE ? || '%'
            """,
            (date_iso,),
        )
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


# --- Telegram action logging (Plan 12e) --------------------------------------


def insert_telegram_message(
    *,
    chat_id: str,
    message_id: int | None,
    alert_id: str,
    kind: str,
    status: str,
) -> None:
    """Record one button/review message sent for an alert."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO telegram_messages
                (chat_id, message_id, alert_id, kind, status, sent_at, answered_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                chat_id,
                message_id,
                alert_id,
                kind,
                status,
                datetime.now(ZoneInfo("America/New_York")).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_telegram_message_by_id(chat_id: str, message_id: int) -> dict | None:
    """Return the newest telegram_messages row for (chat_id, message_id)."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chat_id, message_id, alert_id, kind, status, sent_at
            FROM telegram_messages
            WHERE chat_id = ? AND message_id = ?
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (chat_id, message_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))
    finally:
        conn.close()


def mark_telegram_message_answered(
    *, chat_id: str, alert_id: str, kind: str = "alert"
) -> None:
    """Stamp answered_at on the newest message row for (chat_id, alert_id, kind)."""
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE telegram_messages
               SET answered_at = ?
             WHERE chat_id = ?
               AND alert_id = ?
               AND kind = ?
               AND sent_at = (
                   SELECT MAX(sent_at) FROM telegram_messages
                   WHERE chat_id = ? AND alert_id = ? AND kind = ?
               )
            """,
            (
                datetime.now(ZoneInfo("America/New_York")).isoformat(),
                chat_id,
                alert_id,
                kind,
                chat_id,
                alert_id,
                kind,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_telegram_messages_since(
    since: datetime, *, status: str | None = None
) -> list[dict]:
    """Return telegram_messages rows sent at/after *since*, newest first."""
    query = """
        SELECT chat_id, message_id, alert_id, kind, status, sent_at, answered_at
        FROM telegram_messages
        WHERE sent_at >= ?
    """
    params: list = [since.isoformat()]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY sent_at DESC"
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_app_state(key: str) -> str | None:
    """Return the stored value for *key*, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_app_state(key: str, value: str) -> None:
    """Insert or replace one app_state key/value pair."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def update_signal_user_note(alert_id: str, note: str) -> None:
    """Set user_note on a DELIVERED signal without touching acted/acted_at."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE signals SET user_note = ? WHERE alert_id = ? AND routing_status = 'DELIVERED'",
            (note, alert_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_unacted_delivered_since(
    since: datetime, *, limit: int = 10
) -> list[dict]:
    """Return DELIVERED, acted-IS-NULL signals since *since*, newest first."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT alert_id, timestamp, agent, severity, ticker, title, score
            FROM signals
            WHERE routing_status = 'DELIVERED'
              AND acted IS NULL
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (since.isoformat(), limit),
        )
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


BULK_CLOSE_NOTE_PREFIX = "bulk-closed:"


def bulk_close_delivered_before(date_iso: str, note: str) -> int:
    """Stamp a bulk-close note on unreviewed DELIVERED rows older than *date_iso*.

    Per Plan 10 §10d.1: acted/acted_at stay NULL — this documents "never
    reviewed", not a retrospective decision. Existing notes (e.g. a Telegram
    reply-to-note) are never overwritten, and the note is always normalized to
    the ``bulk-closed:`` prefix so review-report can exclude it regardless of
    the operator's wording. Returns rows actually stamped.
    """
    normalized = (
        note if note.startswith(BULK_CLOSE_NOTE_PREFIX) else f"{BULK_CLOSE_NOTE_PREFIX} {note}"
    )
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE signals
               SET user_note = ?
             WHERE routing_status = 'DELIVERED'
               AND acted IS NULL
               AND (user_note IS NULL OR TRIM(user_note) = '')
               AND timestamp < ?
            """,
            (normalized, date_iso),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
