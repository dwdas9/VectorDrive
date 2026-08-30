"""MCP server probing — verifies VectorDrive's own MCP server actually
works, independent of whether Claude Desktop is configured to use it.
Mirrors docs/mcp-claude-desktop.md's own troubleshooting advice ("test the
server directly first, over the same stdio protocol Claude Desktop
uses") so the GUI can do exactly that before ever touching a real config.

Two tiers:
- quick_probe(): static checks only (command exists, is executable) —
  sub-millisecond, safe to call on every screen load, never spawns a
  process.
- deep_probe(): actually spawns the real `vectordrive mcp` subprocess
  over stdio using the same `mcp` client library that
  tests/integration/test_mcp_protocol.py uses against the production
  protocol path — calls list_tools() and, if present, index_status() —
  and reports latency. Bounded by an overall timeout so a hung/misbuilt
  server can't freeze the GUI forever.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class QuickProbeResult:
    ok: bool
    command: str
    command_exists: bool
    command_executable: bool
    message: str


def quick_probe(command: str, args: list[str] | None = None, env: dict | None = None) -> QuickProbeResult:
    """No subprocess, no I/O beyond a couple of stat() calls."""
    exists = os.path.exists(command)
    executable = exists and os.access(command, os.X_OK)
    if not exists:
        message = f"Command not found: {command}"
    elif not executable:
        message = f"Command exists but is not executable: {command}"
    else:
        message = "Command found and is executable."
    return QuickProbeResult(
        ok=exists and executable,
        command=command,
        command_exists=exists,
        command_executable=executable,
        message=message,
    )


@dataclass(frozen=True)
class DeepProbeResult:
    ok: bool
    tools: list[str]
    index_status: dict | None
    latency_s: float
    error: str | None


async def _run_deep_probe(command: str, args: list[str], env: dict, timeout_s: float) -> DeepProbeResult:
    # Imported lazily: the `mcp` package is only needed for this one deep
    # path, and importing it eagerly at module load would pull it into
    # every GUI screen for no reason.
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

    full_env = {**get_default_environment(), **env}
    params = StdioServerParameters(command=command, args=list(args), env=full_env)
    start = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    tool_names = sorted(t.name for t in tools_result.tools)

                    index_status: dict | None = None
                    if "index_status" in tool_names:
                        result = await session.call_tool("index_status", {})
                        if not result.is_error:
                            index_status = result.structured_content

                    return DeepProbeResult(
                        ok=True,
                        tools=tool_names,
                        index_status=index_status,
                        latency_s=round(time.monotonic() - start, 3),
                        error=None,
                    )
    except TimeoutError:
        return DeepProbeResult(
            ok=False, tools=[], index_status=None,
            latency_s=round(time.monotonic() - start, 3),
            error=f"Server did not respond within {timeout_s:.0f}s.",
        )
    except Exception as exc:  # noqa: BLE001 — any failure here is a probe *result*, not a crash
        return DeepProbeResult(
            ok=False, tools=[], index_status=None,
            latency_s=round(time.monotonic() - start, 3),
            error=f"{type(exc).__name__}: {exc}",
        )


def deep_probe(command: str, args: list[str], env: dict, *, timeout_s: float = 15.0) -> DeepProbeResult:
    """Synchronous wrapper — GuiApi methods, and the JobRunner bodies they
    submit, are plain sync callables.
    """
    return asyncio.run(_run_deep_probe(command, args, env, timeout_s))
