"""MCP-facing read-only SQLite access.

v0.2 G1: the actual implementation moved to db/readonly.py so the GUI's
read screens (and any future consumer) can use it without depending on
the MCP SDK at all. This module now does exactly one thing the shared
implementation can't: translate vectordrive.services.errors.DbUnavailable
into mcp's own ToolError at the MCP boundary, so every existing MCP
behavior (error type, message text) is unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from vectordrive.db.readonly import vec_table_exists  # noqa: F401 - re-exported, no translation needed
from vectordrive.db.readonly import open_readonly_connection as _open_readonly_connection
from vectordrive.services.errors import DbUnavailable


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` strictly read-only.

    Raises ToolError (never a raw sqlite3/OS exception, and never the
    shared DbUnavailable — MCP tool handlers only know how to translate
    ToolError into a protocol-level error) with a clear, actionable
    message if the database file doesn't exist or can't be opened.
    """
    try:
        return _open_readonly_connection(db_path)
    except DbUnavailable as exc:
        raise ToolError(str(exc)) from exc
