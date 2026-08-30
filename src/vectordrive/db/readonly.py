"""Strictly read-only SQLite access — usable by MCP, the GUI's read
screens (Overview, Search, Folders list), and any future read-only
consumer, without depending on the MCP SDK.

Relocated from mcp/readonly_db.py (v0.2 G1): `vectordrive.mcp.readonly_db`
now re-exports these names, translating vectordrive.services.errors.DbUnavailable
to mcp's own ToolError at the MCP boundary, so existing MCP behavior is
unchanged. GUI code should import directly from here instead, and never
touch the mcp package at all.

Two independent layers enforce read-only: the connection itself is opened
via SQLite's `mode=ro` URI (the OS/driver refuses any write, and refuses
to create a missing file — which is also how we detect a missing
database), and `PRAGMA query_only = ON` is set as a second,
application-level belt. Verified live: opening `mode=ro` against a
WAL-mode database (as vectordrive.db always is) reads correctly whether or
not the -wal/-shm sidecar files are present, and a `CREATE TABLE IF NOT
EXISTS`/INSERT/UPDATE/DELETE attempted on this connection raises
sqlite3.OperationalError rather than silently no-op'ing or succeeding.

Never calls connection.py's connect() — that function creates the DB and
its parent directories if missing, and applies schema.sql/_migrate() (DDL
writes). Nothing that opens a read-only connection may ever create a
database or migrate one; a missing or stale database is a clear error,
not something silently fixed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from vectordrive.search.vector_index.sqlite_vec_index import SqliteVecIndex
from vectordrive.services.errors import DbUnavailable


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` strictly read-only.

    Raises DbUnavailable (never a raw sqlite3/OS exception) with a clear,
    actionable message if the database file doesn't exist or can't be
    opened.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise DbUnavailable(
            f"No VectorDrive database found at {db_path}. "
            "Run `vectordrive index --path <folder>` first.",
            hint="Add and index a folder first.",
        )

    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        # check_same_thread=False: callers (MCP's SDK, the GUI's job
        # runner) may hand this connection between threads. Safe here
        # specifically because the connection is read-only (mode=ro +
        # query_only below) and each caller's SQLite access runs to
        # completion before the next begins — never concurrent access to
        # the same connection object from two threads at once.
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise DbUnavailable(f"Could not open the VectorDrive database at {db_path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def vec_table_exists(conn: sqlite3.Connection, table_name: str = "vec_chunks") -> bool:
    """True if the sqlite-vec virtual table already exists — read-only
    check, never CREATEs it. A read-only connection must never call
    SqliteVecIndex's normal constructor (which issues `CREATE VIRTUAL
    TABLE IF NOT EXISTS`); this is how callers decide whether a persisted
    vector index is there to reuse at all.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension on `conn` and prove it
    actually works with a real query (not just that the .load() call
    didn't raise). Loading an extension is a connection-level operation,
    not a database write, so it works on a read-only connection too.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        return False


class ReadOnlySqliteVecIndex(SqliteVecIndex):
    """SqliteVecIndex, minus the constructor's `CREATE VIRTUAL TABLE IF
    NOT EXISTS` (which is a write — forbidden on a read-only connection,
    and wrong in intent even where SQLite would treat it as a no-op:
    nothing read-only may ever be the thing that creates a vector index,
    only reads one `vectordrive index` already built). upsert()/delete()
    are inherited unchanged but must never be called against a read-only
    connection — only search() and list_chunk_ids() (read-only) are safe.
    """

    def _ensure_table(self) -> None:  # pragma: no cover - intentionally inert
        pass
