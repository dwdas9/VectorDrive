"""Path-identity validation for read_document.

The whole path-traversal/symlink-escape defense is structural, not a
denylist: read_document() never calls open()/Path.read_text() on a
filesystem path at all (it reads pages.text from the database), and this
module's only job is to look up a `files` row by normalized identity. A
path that doesn't match a row already written by the indexer at index
time — because it was never indexed, or was disguised via traversal, or
is a symlink resolving somewhere never indexed — simply finds no row and
is rejected. There is no filesystem access here beyond Path.resolve()
(which never opens or reads file contents, only follows symlinks and
collapses ".."/"." components in the path string).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError


def resolve_indexed_file(conn: sqlite3.Connection, path: str) -> sqlite3.Row:
    """Return the `files` row for `path` iff it is a successfully indexed
    file (status = 'indexed'), identified the same way the indexer
    normalizes identity (resolved, absolute — pipeline/indexer.py's
    _normalize_path). Raises ToolError for anything else: empty input,
    an unparseable path, or a path with no matching indexed row.
    """
    if not path or not path.strip():
        raise ToolError("path must not be empty")

    try:
        normalized = str(Path(path).expanduser().resolve())
    except (OSError, ValueError) as exc:
        raise ToolError(f"invalid path: {exc}") from exc

    row = conn.execute(
        "SELECT * FROM files WHERE path = ? AND status = 'indexed'", (normalized,)
    ).fetchone()
    if row is None:
        raise ToolError(
            f"'{path}' is not a path VectorDrive has successfully indexed. "
            "Only paths returned by search_drive citations or `vectordrive status` "
            "can be read."
        )
    return row
