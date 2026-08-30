"""Root identity: the roots table rows behind config.indexed_folders.

A registered indexed root (config.indexed_folders entry) has a persistent
identity (roots.id) independent of its current physical path — this is
what lets Relink (Stage 4) keep the SAME identity after a folder moves or
is renamed, so a re-index after relinking compares files by path relative
to that root instead of treating every file as newly discovered.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def ensure_root(conn: sqlite3.Connection, path: str) -> int:
    """Get-or-create the roots row for an absolute path; returns roots.id."""
    row = conn.execute("SELECT id FROM roots WHERE path = ?", (path,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO roots (path, created_at) VALUES (?, ?)",
        (path, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_root_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM roots WHERE path = ?", (path,)).fetchone()


def delete_root(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM roots WHERE path = ?", (path,))
    conn.commit()


def backfill_files_under_root(conn: sqlite3.Connection, root_id: int, root_path: str) -> int:
    """Set root_id + relative_path on legacy rows under root_path. Idempotent."""
    pattern = root_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    rows = conn.execute(
        "SELECT id, path FROM files WHERE root_id IS NULL AND "
        "(path = ? OR path LIKE ? ESCAPE '\\')",
        (root_path, pattern),
    ).fetchall()
    for row in rows:
        rel = str(Path(row["path"]).relative_to(root_path)) if row["path"] != root_path else "."
        conn.execute(
            "UPDATE files SET root_id = ?, relative_path = ? WHERE id = ?",
            (root_id, rel, row["id"]),
        )
    conn.commit()
    return len(rows)
