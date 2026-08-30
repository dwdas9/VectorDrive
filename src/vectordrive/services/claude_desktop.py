"""Claude Desktop MCP configuration management — the only genuinely new
domain in v0.2 (v0.1 never touched claude_desktop_config.json at all;
see docs/mcp-claude-desktop.md for the manual process this automates).

Hard safety rules, enforced structurally, not by convention:
- "Connected" is never inferred from the config file alone. See
  connection_state() below — it requires a passing deep test newer than
  the config file's own mtime.
- Every write goes through services.atomic_io (backup first, then an
  atomic replace) and a file_fingerprint_guard so a config edited by hand
  between preview and apply is never silently overwritten.
- Only the ["mcpServers"]["vectordrive"] key is ever touched; every other
  key, and every other server entry, is round-tripped byte-for-byte.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vectordrive.services.atomic_io import (
    ConcurrentModificationError,
    atomic_write_text,
    file_fingerprint_guard,
    timestamped_backup,
)
from vectordrive.services.errors import ClaudeConfigError
from vectordrive.services.platform_paths import (
    claude_desktop_app_paths,
    claude_desktop_config_path,
    claude_desktop_is_running,
)

SERVER_NAME = "vectordrive"

ConnectionState = Literal[
    "not_installed", "not_configured", "configured_untested", "configured_stale",
    "configured_failing", "verified",
]


@dataclass(frozen=True)
class ClaudeDesktopStatus:
    app_installed: bool
    app_path: str | None
    app_running: bool | None
    config_path: str
    config_exists: bool
    config_readable: bool
    config_parse_error: str | None
    entry_present: bool
    other_server_names: list[str]
    connection_state: ConnectionState
    last_verified_at: str | None
    restart_required: bool
    config_mtime_ns: int
    config_size: int


def _read_config_document(path: Path) -> tuple[dict | None, str | None]:
    """Returns (document, parse_error). document is None iff the file
    doesn't exist or fails to parse — the two are distinguished by
    parse_error being set only in the latter case.
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read file: {exc}"
    if not text.strip():
        return {}, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"


def resolve_executable() -> Path:
    """Absolute path to *this* environment's `vectordrive` console
    script. Claude Desktop has no PATH — a relative or PATH-dependent
    value would silently fail to launch.

    G14 finding: a packaged VectorDrive.app has no separate `vectordrive`
    CLI script next to its own executable — Contents/MacOS/ contains only
    the single frozen `VectorDrive` binary (confirmed by inspecting a
    real build) — so this lookup always raised ClaudeConfigError when
    "Connect" was driven from the packaged app. `sys.frozen` (set by
    PyInstaller's bootloader, the same flag gui/app.py's _web_dir()
    already checks) distinguishes the two cases: frozen means the app's
    own executable *is* the command to relaunch — with `--mcp`, not the
    dev CLI's `mcp` subcommand, see gui/app.py's __main__ dispatch —
    otherwise fall back to the sibling-script lookup for a normal venv
    install, unchanged.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    exe = Path(sys.executable).parent / "vectordrive"
    if not exe.exists():
        raise ClaudeConfigError(
            f"Could not find a 'vectordrive' executable next to the running Python "
            f"interpreter ({sys.executable}).",
            hint="Reinstall VectorDrive, or run this from the same environment it was installed in.",
        )
    return exe.resolve()


def desired_entry(data_home: Path) -> dict:
    args = ["--mcp"] if getattr(sys, "frozen", False) else ["mcp"]
    return {
        "command": str(resolve_executable()),
        "args": args,
        "env": {"VECTORDRIVE_HOME": str(Path(data_home).resolve())},
    }


def detect(
    *, data_home: Path, last_verified_at: str | None = None, config_path: Path | None = None
) -> ClaudeDesktopStatus:
    """`config_path` defaults to the real Claude Desktop location; tests
    always pass an explicit temp-fixture path so nothing here ever
    touches a real user's actual Claude Desktop configuration.
    """
    app_paths = [p for p in claude_desktop_app_paths() if p.exists()]
    app_installed = bool(app_paths)
    config_path = config_path or claude_desktop_config_path()
    document, parse_error = _read_config_document(config_path)

    config_exists = config_path.exists()
    config_readable = config_exists and parse_error is None
    entry_present = False
    other_servers: list[str] = []
    if document is not None:
        servers = document.get("mcpServers", {}) if isinstance(document, dict) else {}
        if isinstance(servers, dict):
            entry_present = SERVER_NAME in servers
            other_servers = [name for name in servers if name != SERVER_NAME]

    stat = config_path.stat() if config_exists else None
    config_mtime_ns = stat.st_mtime_ns if stat else 0
    config_size = stat.st_size if stat else 0

    if not app_installed:
        state: ConnectionState = "not_installed"
    elif not entry_present:
        state = "not_configured"
    elif last_verified_at is not None and config_mtime_ns and last_verified_at >= str(config_mtime_ns):
        state = "verified"
    else:
        state = "configured_untested"

    return ClaudeDesktopStatus(
        app_installed=app_installed,
        app_path=str(app_paths[0]) if app_paths else None,
        app_running=claude_desktop_is_running() if app_installed else None,
        config_path=str(config_path),
        config_exists=config_exists,
        config_readable=config_readable,
        config_parse_error=parse_error,
        entry_present=entry_present,
        other_server_names=other_servers,
        connection_state=state,
        last_verified_at=last_verified_at,
        restart_required=entry_present,  # any existing entry might be stale; refined by callers post-apply
        config_mtime_ns=config_mtime_ns,
        config_size=config_size,
    )


@dataclass(frozen=True)
class ConfigDiff:
    action: Literal["connect", "disconnect", "create"]
    before_text: str
    after_text: str
    unified_diff: str
    preserved_server_count: int
    is_noop: bool
    config_path: str
    fingerprint_mtime_ns: int
    fingerprint_size: int


def _dump(document: dict) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _unified_diff(before: str, after: str) -> str:
    import difflib

    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), lineterm=""))


def preview_connect(data_home: Path, *, config_path: Path | None = None) -> ConfigDiff:
    config_path = config_path or claude_desktop_config_path()
    document, parse_error = _read_config_document(config_path)
    if parse_error is not None:
        raise ClaudeConfigError(
            f"Claude Desktop's config file isn't valid JSON: {parse_error}",
            hint="Open it in a text editor to fix it, or restore from a backup, before connecting.",
        )
    before_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    document = document or {}
    servers = document.setdefault("mcpServers", {})
    other_count = len([n for n in servers if n != SERVER_NAME])
    was_present = SERVER_NAME in servers
    servers[SERVER_NAME] = desired_entry(data_home)
    after_text = _dump(document)

    stat = config_path.stat() if config_path.exists() else None
    return ConfigDiff(
        action="connect",
        before_text=before_text,
        after_text=after_text,
        unified_diff=_unified_diff(before_text, after_text),
        preserved_server_count=other_count,
        is_noop=(before_text == after_text) and was_present,
        config_path=str(config_path),
        fingerprint_mtime_ns=stat.st_mtime_ns if stat else 0,
        fingerprint_size=stat.st_size if stat else 0,
    )


def preview_disconnect(*, config_path: Path | None = None) -> ConfigDiff:
    config_path = config_path or claude_desktop_config_path()
    document, parse_error = _read_config_document(config_path)
    if parse_error is not None:
        raise ClaudeConfigError(f"Claude Desktop's config file isn't valid JSON: {parse_error}")
    before_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    document = document or {}
    servers = document.get("mcpServers", {})
    other_count = len([n for n in servers if n != SERVER_NAME])
    was_present = SERVER_NAME in servers
    if was_present:
        del servers[SERVER_NAME]
    after_text = _dump(document)

    stat = config_path.stat() if config_path.exists() else None
    return ConfigDiff(
        action="disconnect",
        before_text=before_text,
        after_text=after_text,
        unified_diff=_unified_diff(before_text, after_text),
        preserved_server_count=other_count,
        is_noop=not was_present,
        config_path=str(config_path),
        fingerprint_mtime_ns=stat.st_mtime_ns if stat else 0,
        fingerprint_size=stat.st_size if stat else 0,
    )


@dataclass(frozen=True)
class AppliedChange:
    backup_path: str | None
    config_path: str


def apply(diff: ConfigDiff, *, config_path: Path | None = None) -> AppliedChange:
    """Backs up the current file (if any), then atomically writes
    diff.after_text — refusing if the file changed since the diff was
    generated (file_fingerprint_guard).
    """
    path = config_path or Path(diff.config_path)
    try:
        with file_fingerprint_guard(path, expected_mtime_ns=diff.fingerprint_mtime_ns, expected_size=diff.fingerprint_size):
            backup_path = timestamped_backup(path)
            atomic_write_text(path, diff.after_text)
    except ConcurrentModificationError as exc:
        raise ClaudeConfigError(str(exc), hint="Re-open the preview and try again.") from exc
    return AppliedChange(backup_path=str(backup_path) if backup_path else None, config_path=str(path))


def list_backups(*, config_path: Path | None = None) -> list[str]:
    path = config_path or claude_desktop_config_path()
    return sorted(str(p) for p in path.parent.glob(f"{path.name}.vectordrive-backup.*"))


def restore_backup(backup_path: Path, *, config_path: Path | None = None) -> AppliedChange:
    path = config_path or claude_desktop_config_path()
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise ClaudeConfigError(f"Backup not found: {backup_path}")
    try:
        json.loads(backup_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeConfigError(f"That backup file isn't valid JSON: {exc}") from exc
    current_backup = timestamped_backup(path)
    atomic_write_text(path, backup_path.read_text(encoding="utf-8"))
    return AppliedChange(backup_path=str(current_backup) if current_backup else None, config_path=str(path))
