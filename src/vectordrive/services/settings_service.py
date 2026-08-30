"""Settings & maintenance service (v0.2 G9).

Provides the backend for the Settings screen: log viewing, database
integrity checking, database backup, and safe maintenance actions (vacuum,
WAL checkpoint). Never deletes or modifies source documents.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from vectordrive.config import get_config_path, get_db_path, load_config
from vectordrive.services.atomic_io import timestamped_backup
from vectordrive.services.errors import DbUnavailable, VectorDriveError


LOG_FILENAME = "vectordrive.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3


def setup_file_logging(data_home: Path) -> Path:
    """Configure the root vectordrive logger to write to a RotatingFileHandler
    under data_home/logs/. Idempotent — safe to call multiple times.
    Returns the log file path.
    """
    from logging.handlers import RotatingFileHandler

    log_dir = data_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME

    root_logger = logging.getLogger("vectordrive")
    # Reconcile any existing RotatingFileHandler(s) already attached to the
    # vectordrive logger against the requested target before adding a new
    # one — otherwise repeated calls (e.g. tests that delete the log file,
    # or a data_home change at runtime) accumulate open handlers/file
    # descriptors, or return a handle whose backing file no longer exists.
    for h in list(root_logger.handlers):
        if not (isinstance(h, RotatingFileHandler) and hasattr(h, "baseFilename")):
            continue
        if Path(h.baseFilename).resolve() == log_path.resolve():
            if Path(h.baseFilename).exists():
                # Same target, file still present: idempotent no-op.
                return log_path
            # Same target but the file was deleted out from under the
            # handler: close/remove and fall through to recreate it.
            root_logger.removeHandler(h)
            h.close()
        else:
            # A prior VectorDrive rotating handler pointed at a different
            # target (e.g. data_home changed) — close/remove it so it
            # doesn't keep accumulating alongside the new one.
            root_logger.removeHandler(h)
            h.close()

    handler = RotatingFileHandler(
        log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    return log_path


def get_log_tail(data_home: Path, lines: int = 200) -> dict:
    """Read the last `lines` lines from the active log file."""
    log_path = data_home / "logs" / LOG_FILENAME
    if not log_path.exists():
        return {"path": str(log_path), "lines": [], "total_bytes": 0}
    content = log_path.read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return {
        "path": str(log_path),
        "lines": tail,
        "total_bytes": log_path.stat().st_size,
    }


def check_integrity(data_home: Path) -> dict:
    """Run PRAGMA integrity_check on the database."""
    db_path = get_db_path(data_home)
    if not db_path.exists():
        return {"ok": False, "message": "Database file not found.", "db_path": str(db_path)}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        return {"ok": False, "message": f"Cannot open database: {exc}", "db_path": str(db_path)}
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result_text = rows[0][0] if rows else "unknown"
        return {
            "ok": result_text == "ok",
            "message": result_text,
            "db_path": str(db_path),
        }
    except sqlite3.Error as exc:
        return {"ok": False, "message": f"Integrity check failed: {exc}", "db_path": str(db_path)}
    finally:
        conn.close()


def backup_database(data_home: Path) -> dict:
    """Create a timestamped copy of the database file in data_home/backups/."""
    db_path = get_db_path(data_home)
    if not db_path.exists():
        raise VectorDriveError("No database to back up.", code="db_unavailable")
    backup_dir = data_home / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_name = f"vectordrive-{ts}.db"
    dest = backup_dir / backup_name
    shutil.copy2(db_path, dest)
    # Also copy WAL if present (for a consistent hot backup)
    wal_path = db_path.with_name(db_path.name + "-wal")
    if wal_path.exists():
        shutil.copy2(wal_path, dest.with_name(backup_name + "-wal"))
    return {
        "backup_path": str(dest),
        "size_bytes": dest.stat().st_size,
    }


def list_database_backups(data_home: Path) -> list[dict]:
    """List available database backups."""
    backup_dir = data_home / "backups"
    if not backup_dir.exists():
        return []
    backups = sorted(backup_dir.glob("vectordrive-*.db"), reverse=True)
    return [
        {"path": str(p), "name": p.name, "size_bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
        for p in backups
    ]


def vacuum_database(data_home: Path) -> dict:
    """Run VACUUM on the database to reclaim space. Must not be called
    while an indexing job is running (caller is responsible for checking).
    """
    db_path = get_db_path(data_home)
    if not db_path.exists():
        raise VectorDriveError("No database to vacuum.", code="db_unavailable")
    size_before = db_path.stat().st_size
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("VACUUM")
    except sqlite3.Error as exc:
        raise VectorDriveError(f"Vacuum failed: {exc}", code="vacuum_failed") from exc
    finally:
        conn.close()
    size_after = db_path.stat().st_size
    return {
        "size_before": size_before,
        "size_after": size_after,
        "reclaimed": size_before - size_after,
    }


def wal_checkpoint(data_home: Path) -> dict:
    """Checkpoint the WAL into the main database file."""
    db_path = get_db_path(data_home)
    if not db_path.exists():
        raise VectorDriveError("No database.", code="db_unavailable")
    conn = sqlite3.connect(str(db_path))
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"busy": result[0], "log_frames": result[1], "checkpointed": result[2]}
    except sqlite3.Error as exc:
        raise VectorDriveError(f"Checkpoint failed: {exc}", code="checkpoint_failed") from exc
    finally:
        conn.close()


def get_data_home_info(data_home: Path) -> dict:
    """Summary of what's in VECTORDRIVE_HOME for the Settings screen."""
    db_path = get_db_path(data_home)
    config_path = get_config_path(data_home)
    log_path = data_home / "logs" / LOG_FILENAME
    return {
        "data_home": str(data_home),
        "db_exists": db_path.exists(),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "log_path": str(log_path),
        "log_exists": log_path.exists(),
        "log_size_bytes": log_path.stat().st_size if log_path.exists() else 0,
    }
