"""`vectordrive mcp`: starts the read-only MCP server over stdio for
Claude Desktop. Contains no server/tool logic of its own — wires
vectordrive.mcp.server.run_mcp_stdio_server() the same thin way
index_cmd.py/search_cmd.py/status_cmd.py wire their own modules.
"""
from __future__ import annotations

from vectordrive.mcp.server import run_mcp_stdio_server


def run_mcp_command() -> int:
    run_mcp_stdio_server()
    return 0
