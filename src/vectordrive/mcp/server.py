"""Builds the MCP server and runs it over stdio.

SDK note (checked live against the installed `mcp` package, v2.0.0, before
writing this — the plan's `mcp.server.fastmcp.FastMCP` does not exist in
this version; `python -c "import mcp.server.fastmcp"` raises
ModuleNotFoundError). This SDK's ergonomic server class is
`mcp.server.mcpserver.MCPServer` (also re-exported as `mcp.server.MCPServer`),
which is API-equivalent for our purposes: a `@server.tool()` decorator that
infers the JSON schema from the function signature and return-type
annotation, and `server.run("stdio")` for stdio transport.

Only three tools are registered, by explicit `name=`, exactly matching the
plan (requirement: "expose exactly these three MCP tools"). Any exception
a tool raises — ToolError or otherwise — is turned by the SDK into a
CallToolResult(is_error=True, content=[...]) rather than crashing the
server or leaking a raw traceback (verified in mcp/server/mcpserver/
server.py's _handle_call_tool); sqlite3.Error is also caught explicitly
here for a clearer "corrupt index state"-style message than the raw
driver exception would give.
"""
from __future__ import annotations

import sqlite3
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from vectordrive.config import get_db_path, load_config
from vectordrive.mcp import tools
from vectordrive.mcp.context import McpSearchContext, build_context

_SERVER_NAME = "vectordrive"
_SERVER_INSTRUCTIONS = (
    "Read-only search over the user's locally indexed documents. "
    "search_drive returns retrieved passage citations only, never the full "
    "document; read_document returns a bounded preview or a single page, "
    "never the whole file. This server cannot index, modify, or delete "
    "anything."
)


def _run_tool(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ToolError:
        raise
    except sqlite3.Error as exc:
        raise ToolError(f"VectorDrive index error: {exc}") from exc


def create_server(ctx: McpSearchContext) -> MCPServer:
    server: MCPServer = MCPServer(_SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)

    @server.tool(name="search_drive", description="Hybrid/FTS/vector search over the indexed documents.")
    def _search_drive(query: str, top_k: int = 10, mode: str = "hybrid") -> tools.SearchDriveResult:
        return _run_tool(tools.search_drive, ctx, query, top_k, mode)

    @server.tool(
        name="read_document",
        description="Read a bounded preview, or a single page, of an already-indexed document.",
    )
    def _read_document(path: str, page: int | None = None) -> tools.ReadDocumentResult:
        return _run_tool(tools.read_document, ctx, path, page)

    @server.tool(name="index_status", description="Summarize the latest indexing run and configuration.")
    def _index_status() -> tools.IndexStatusResult:
        return _run_tool(tools.index_status, ctx)

    return server


def run_mcp_stdio_server() -> None:
    """Entry point for `vectordrive mcp`. Never prints to stdout itself —
    stdout is reserved entirely for the MCP protocol's JSON-RPC framing;
    startup diagnostics go to stderr.
    """
    print(f"vectordrive mcp: starting stdio server (db: {get_db_path()})", file=sys.stderr)
    config = load_config()
    ctx = build_context(get_db_path(), config)
    if ctx.startup_error:
        print(f"vectordrive mcp: warning: {ctx.startup_error}", file=sys.stderr)
    server = create_server(ctx)
    server.run("stdio")
