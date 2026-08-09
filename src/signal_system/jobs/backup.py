"""backup job — weekly WAL-safe snapshot of the signal database.

Standard job shape: runs row, heartbeat, print + Telegram delivery. Backs up
state/signals.db via repository.backup_database() (VACUUM INTO), prunes old
backups, and fails loudly if the backup is empty while the live DB has rows.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from signal_system import config
from signal_system.delivery.telegram_sender import send_message
from signal_system.monitoring.heartbeat import heartbeat
from signal_system.state import repository

logger = logging.getLogger(__name__)

_BACKUP_RE = re.compile(r"^signals-\d{8}\.db$")
_KEEP_NEWEST = 8


def _resolve_backup_dir() -> Path:
    """Resolve config.BACKUP_DIR to an absolute path.

    Relative paths are anchored at the repo root (repository.DB_PATH.parents[1])
    because Task Scheduler's working directory is not trustworthy on Windows.
    """
    raw = config.BACKUP_DIR
    path = Path(raw)
    if path.is_absolute():
        return path
    return repository.DB_PATH.parents[1] / path


def _prune(dest_dir: Path) -> list[str]:
    """Delete all but the newest *_KEEP_NEWEST* signals-YYYYMMDD.db files.

    Only files matching ``^signals-\\d{8}\\.db$`` are ever touched — foreign
    files (notes.txt, the live DB itself) are never deleted.
    """
    candidates = sorted(
        p for p in dest_dir.glob("signals-*.db") if _BACKUP_RE.match(p.name)
    )
    for stale in candidates[: max(0, len(candidates) - _KEEP_NEWEST)]:
        stale.unlink(missing_ok=True)
    return [p.name for p in candidates[max(0, len(candidates) - _KEEP_NEWEST):]]


def run() -> None:
    run_id = repository.insert_run("backup")
    try:
        with heartbeat(job="backup"):
            dest_dir = _resolve_backup_dir()
            path, backup_count = repository.backup_database(dest_dir)
            live_count = repository.count_signals()
            if backup_count == 0 and live_count > 0:
                raise RuntimeError(
                    "backup produced 0 rows while the live database has "
                    f"{live_count} — refusing to trust a corrupt snapshot"
                )
            kept = _prune(dest_dir)
            text = (
                f"DB Backup — {path.name}\n"
                f"Backed up {backup_count} signals to {path}\n"
                f"Kept backups: {', '.join(kept) if kept else 'none'}"
            )
            logger.info("backup:\n%s", text)
            print(text)
            send_message(text)
            repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
