"""Durable checkpoints for the Markdown-generation and indexing phases.

This module records orchestration state only. It does not read, write, or
delete source documents or Markdown sidecars. Callers choose safe item
boundaries, then use this ledger to pause and resume between those units.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

from vectordrive.services.errors import IndexIncomplete

Phase = Literal["markdown", "index"]


class StateTransitionError(RuntimeError):
    """The requested run/item state transition is not currently valid."""


@dataclass(frozen=True)
class ProcessingItemSeed:
    item_key: str
    content_fingerprint: str
    config_fingerprint: str


@dataclass(frozen=True)
class ClaimedItem:
    id: int
    run_id: int
    ordinal: int
    item_key: str
    content_fingerprint: str
    config_fingerprint: str
    attempt_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def create_run(
    conn: sqlite3.Connection,
    *,
    phase: Phase,
    root_path: str,
    content_fingerprint: str,
    config_fingerprint: str,
    items: Iterable[ProcessingItemSeed],
) -> int:
    """Create a queued run and its immutable, deterministically ordered plan."""
    if phase not in ("markdown", "index"):
        raise ValueError("phase must be 'markdown' or 'index'")
    root_path = _required(root_path, "root_path")
    content_fingerprint = _required(content_fingerprint, "content_fingerprint")
    config_fingerprint = _required(config_fingerprint, "config_fingerprint")
    seeds = list(items)
    if not seeds:
        raise ValueError("items must not be empty")
    if len({seed.item_key for seed in seeds}) != len(seeds):
        raise ValueError("item keys must be unique within a run")

    now = _now()
    with conn:
        cursor = conn.execute(
            """INSERT INTO processing_runs
               (phase, root_path, status, content_fingerprint, config_fingerprint,
                created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?, ?, ?)""",
            (phase, root_path, content_fingerprint, config_fingerprint, now, now),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """INSERT INTO processing_run_items
               (run_id, ordinal, item_key, status, content_fingerprint,
                config_fingerprint, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    ordinal,
                    _required(seed.item_key, "item_key"),
                    _required(seed.content_fingerprint, "item content_fingerprint"),
                    _required(seed.config_fingerprint, "item config_fingerprint"),
                    now,
                    now,
                )
                for ordinal, seed in enumerate(seeds)
            ],
        )
    return run_id


def resume_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Explicitly start a queued run or resume a paused run."""
    now = _now()
    with conn:
        changed = conn.execute(
            """UPDATE processing_runs
               SET status = 'running', started_at = COALESCE(started_at, ?),
                   updated_at = ?, heartbeat_at = ?, error_message = NULL
               WHERE id = ? AND status IN ('queued', 'paused')""",
            (now, now, now, run_id),
        ).rowcount
    if changed != 1:
        raise StateTransitionError(f"run {run_id} is not queued or paused")


def claim_next_item(conn: sqlite3.Connection, run_id: int) -> ClaimedItem | None:
    """Claim the next resumable item, preferring interrupted work."""
    now = _now()
    with conn:
        run = conn.execute(
            "SELECT status FROM processing_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None or run["status"] != "running":
            return None
        row = conn.execute(
            """SELECT * FROM processing_run_items
               WHERE run_id = ? AND status IN ('paused', 'queued')
               ORDER BY CASE status WHEN 'paused' THEN 0 ELSE 1 END, ordinal
               LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        changed = conn.execute(
            """UPDATE processing_run_items
               SET status = 'running', attempt_count = attempt_count + 1,
                   started_at = COALESCE(started_at, ?), updated_at = ?,
                   heartbeat_at = ?, error_message = NULL
               WHERE id = ? AND status IN ('paused', 'queued')""",
            (now, now, now, row["id"]),
        ).rowcount
        if changed != 1:
            raise StateTransitionError("item was claimed concurrently")
        conn.execute(
            "UPDATE processing_runs SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
            (now, now, run_id),
        )
        attempt_count = row["attempt_count"] + 1
    return ClaimedItem(
        id=row["id"],
        run_id=run_id,
        ordinal=row["ordinal"],
        item_key=row["item_key"],
        content_fingerprint=row["content_fingerprint"],
        config_fingerprint=row["config_fingerprint"],
        attempt_count=attempt_count,
    )


def heartbeat(conn: sqlite3.Connection, run_id: int, *, item_id: int | None = None) -> None:
    """Persist liveness at a safe sub-item boundary."""
    now = _now()
    with conn:
        if conn.execute(
            """UPDATE processing_runs SET heartbeat_at = ?, updated_at = ?
               WHERE id = ? AND status = 'running'""",
            (now, now, run_id),
        ).rowcount != 1:
            raise StateTransitionError(f"run {run_id} is not running")
        if item_id is not None and conn.execute(
            """UPDATE processing_run_items SET heartbeat_at = ?, updated_at = ?
               WHERE id = ? AND run_id = ? AND status = 'running'""",
            (now, now, item_id, run_id),
        ).rowcount != 1:
            raise StateTransitionError(f"item {item_id} is not running in run {run_id}")


def pause_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Pause a run and its current item; completed work remains completed."""
    now = _now()
    with conn:
        changed = conn.execute(
            """UPDATE processing_runs
               SET status = 'paused', paused_at = ?, updated_at = ?
               WHERE id = ? AND status = 'running'""",
            (now, now, run_id),
        ).rowcount
        if changed != 1:
            raise StateTransitionError(f"run {run_id} is not running")
        conn.execute(
            """UPDATE processing_run_items
               SET status = 'paused', paused_at = ?, updated_at = ?
               WHERE run_id = ? AND status = 'running'""",
            (now, now, run_id),
        )


def complete_item(conn: sqlite3.Connection, run_id: int, item_id: int) -> None:
    now = _now()
    with conn:
        changed = conn.execute(
            """UPDATE processing_run_items
               SET status = 'completed', completed_at = ?, updated_at = ?, error_message = NULL
               WHERE id = ? AND run_id = ? AND status = 'running'""",
            (now, now, item_id, run_id),
        ).rowcount
        if changed != 1:
            raise StateTransitionError(f"item {item_id} is not running in run {run_id}")
        conn.execute(
            "UPDATE processing_runs SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
            (now, now, run_id),
        )


def fail_item(conn: sqlite3.Connection, run_id: int, item_id: int, error: str) -> None:
    now = _now()
    error = _required(error, "error")
    with conn:
        changed = conn.execute(
            """UPDATE processing_run_items
               SET status = 'failed', failed_at = ?, updated_at = ?, error_message = ?
               WHERE id = ? AND run_id = ? AND status = 'running'""",
            (now, now, error, item_id, run_id),
        ).rowcount
    if changed != 1:
        raise StateTransitionError(f"item {item_id} is not running in run {run_id}")


def complete_run(conn: sqlite3.Connection, run_id: int) -> None:
    now = _now()
    with conn:
        unfinished = conn.execute(
            "SELECT COUNT(*) FROM processing_run_items WHERE run_id = ? AND status != 'completed'",
            (run_id,),
        ).fetchone()[0]
        if unfinished:
            raise StateTransitionError(f"run {run_id} has {unfinished} unfinished items")
        changed = conn.execute(
            """UPDATE processing_runs
               SET status = 'completed', completed_at = ?, updated_at = ?, error_message = NULL
               WHERE id = ? AND status = 'running'""",
            (now, now, run_id),
        ).rowcount
    if changed != 1:
        raise StateTransitionError(f"run {run_id} is not running")


def fail_run(conn: sqlite3.Connection, run_id: int, error: str) -> None:
    """Record an unrecoverable orchestration failure separately from item errors."""
    now = _now()
    error = _required(error, "error")
    with conn:
        changed = conn.execute(
            """UPDATE processing_runs
               SET status = 'failed', failed_at = ?, updated_at = ?, error_message = ?
               WHERE id = ? AND status IN ('queued', 'running', 'paused')""",
            (now, now, error, run_id),
        ).rowcount
    if changed != 1:
        raise StateTransitionError(f"run {run_id} cannot be failed")


def recover_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Convert every orphaned running run/item to paused after restart."""
    now = _now()
    with conn:
        run_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM processing_runs WHERE status = 'running'"
            ).fetchall()
        ]
        if not run_ids:
            return 0
        placeholders = ",".join("?" for _ in run_ids)
        conn.execute(
            f"""UPDATE processing_run_items
                SET status = 'paused', paused_at = ?, updated_at = ?
                WHERE status = 'running' AND run_id IN ({placeholders})""",
            (now, now, *run_ids),
        )
        conn.execute(
            f"""UPDATE processing_runs
                SET status = 'paused', paused_at = ?, updated_at = ?
                WHERE status = 'running' AND id IN ({placeholders})""",
            (now, now, *run_ids),
        )
    return len(run_ids)


def list_resumable_runs(conn: sqlite3.Connection) -> list[dict]:
    """Return queued/paused run progress without document names or content."""
    rows = conn.execute(
        """SELECT r.id, r.phase, r.root_path, r.status, r.updated_at,
                  COUNT(i.id) AS total_items,
                  COALESCE(SUM(CASE WHEN i.status = 'completed' THEN 1 ELSE 0 END), 0)
                      AS completed_items
             FROM processing_runs AS r
             JOIN processing_run_items AS i ON i.run_id = r.id
            WHERE r.status IN ('queued', 'paused')
            GROUP BY r.id
            ORDER BY r.updated_at DESC, r.id DESC"""
    ).fetchall()
    return [
        {
            "run_id": row["id"],
            "phase": row["phase"],
            "root_path": row["root_path"],
            "status": row["status"],
            "completed_items": row["completed_items"],
            "total_items": row["total_items"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def require_completed_index(conn: sqlite3.Connection) -> None:
    """Fail closed while the database's newest Phase 2 publication is incomplete.

    A Phase 2 run mutates the relational index document by document and only
    publishes its vector index at completion.  Until then all retrieval modes,
    including FTS, would expose a mixed old/new corpus.
    """
    row = conn.execute(
        "SELECT status FROM processing_runs WHERE phase = 'index' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None and row["status"] != "completed":
        raise IndexIncomplete(str(row["status"]))
