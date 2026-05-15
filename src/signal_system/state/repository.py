"""SQLite repository for signals and runs state management."""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("state/signals.db")


def init_db() -> None:
    """Initialize the database: create state/ directory, enable WAL mode, and create tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # Enable WAL mode
        cursor.execute("PRAGMA journal_mode=WAL;")

        # Create signals table
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

        # Create runs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                job TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()


def insert_signal(
    agent: str,
    ticker: str | None,
    title: str,
    body: str | None = None,
    score: float | None = None,
    severity: str = "INFORMATIONAL",
    suggested_action: str | None = None,
) -> str:
    """
    Insert a signal into the database.

    Returns the generated alert_id (UUID v4).
    """
    alert_id = str(uuid.uuid4())
    timestamp = datetime.now(ZoneInfo("America/New_York")).isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO signals (
                alert_id, timestamp, agent, severity, ticker, title, body,
                suggested_action, score, acted, acted_at, user_note,
                outcome_price_30d, outcome_price_90d
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert_id, timestamp, agent, severity, ticker, title, body,
            suggested_action, score, None, None, None, None, None
        ))

        conn.commit()
    finally:
        conn.close()

    return alert_id


def insert_run(job: str) -> str:
    """
    Insert a run record.

    Returns the generated run_id (UUID v4).
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    status = "running"

    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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
