"""Platform-specific paths and OS actions, isolated behind one module.

`config.py:_default_data_home()` established the existing precedent of a
single `sys.platform == "darwin"` branch; this module is where every new
OS-specific path/action for v0.2 goes, so a future Windows adapter is a
matter of adding branches here, not hunting through gui/ and services/ for
scattered `sys.platform` checks.

Every subprocess call here uses a fixed argv list with an absolute binary
path and an explicit timeout — never a shell string, never `shell=True`,
never user-controlled text interpolated into a command.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


class UnsupportedPlatform(RuntimeError):
    """Raised by a platform-specific action on a platform it doesn't
    (yet) support, rather than silently no-op'ing.
    """


def claude_desktop_config_path() -> Path:
    """Path to Claude Desktop's own MCP config file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    raise UnsupportedPlatform(
        f"Claude Desktop config path is not yet known for platform {sys.platform!r} "
        "(only macOS is supported in v0.2)."
    )


def claude_desktop_app_paths() -> list[Path]:
    """Candidate install locations for Claude Desktop itself."""
    if sys.platform == "darwin":
        return [Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"]
    raise UnsupportedPlatform(
        f"Claude Desktop app paths are not yet known for platform {sys.platform!r}."
    )


def claude_desktop_is_running() -> bool | None:
    """Best-effort check via a fixed-argv `/bin/ps` scan (no `psutil`
    dependency). Returns None ("can't tell") rather than raising if the
    platform isn't supported or the check itself fails — callers must
    treat None as "unknown", never as "not running".
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return any(
        line.strip().endswith("/Claude") or line.strip() == "Claude"
        for line in result.stdout.splitlines()
    )


def open_path_in_default_app(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", str(path)], timeout=10, check=False)
        return
    raise UnsupportedPlatform(f"Cannot open paths in the default app on platform {sys.platform!r}.")


def reveal_path_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", "-R", str(path)], timeout=10, check=False)
        return
    raise UnsupportedPlatform(f"Cannot reveal paths in Finder on platform {sys.platform!r}.")


def get_memory_usage() -> dict | None:
    """Best-effort system-wide memory usage as {"used_bytes",
    "total_bytes"}, for the GUI's job-status card ("Memory: 18.2 / 36
    GB"). No `psutil` dependency — same convention as
    claude_desktop_is_running() above: a fixed-argv subprocess call with
    an explicit timeout.

    `used_bytes` is approximated as total minus genuinely-free pages
    (vm_stat's "free" + "speculative") — a coarse, honest "how much
    headroom is left" figure for an informational display, not an
    Activity-Monitor-precision accounting of wired/compressed/cached
    memory. Short timeouts (this is polled every ~2s while a job is
    active — see gui/web/job-status.js): never blocks the GUI bridge
    thread for more than a few seconds even if a command hangs. Returns
    None (never raises) on any failure or unsupported platform, so a
    resource-card display glitch can never surface as an app error.
    """
    if sys.platform != "darwin":
        return None
    try:
        vm = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=1.5)
        mem = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=1.5
        )
    except Exception:
        return None
    if vm.returncode != 0 or mem.returncode != 0:
        return None

    try:
        total_bytes = int(mem.stdout.strip())

        page_size = 16384
        lines = vm.stdout.splitlines()
        size_match = re.search(r"page size of (\d+) bytes", lines[0]) if lines else None
        if size_match:
            page_size = int(size_match.group(1))

        free_pages = 0
        for line in lines:
            if line.startswith("Pages free:") or line.startswith("Pages speculative:"):
                free_pages += int(line.split(":", 1)[1].strip().rstrip("."))

        used_bytes = max(0, total_bytes - free_pages * page_size)
        return {"used_bytes": used_bytes, "total_bytes": total_bytes}
    except Exception:
        return None


def default_backup_dir() -> Path:
    from vectordrive.config import get_data_home

    return get_data_home() / "backups"


def is_network_volume(path: Path) -> bool:
    """True if `path` lives under macOS's network-mount root, /Volumes,
    other than the boot volume. A conservative heuristic (not a `stat()`
    filesystem-type probe), good enough for a UI warning, not a hard block.
    """
    if sys.platform != "darwin":
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return str(resolved).startswith("/Volumes/")


def is_cloud_synced(path: Path) -> bool:
    """True if `path` looks like it lives inside a cloud-sync client's
    managed folder (iCloud Drive, and common third-party sync roots).
    Indexing such a folder is fine; only VECTORDRIVE_HOME itself must
    never be inside one (docs/storage-and-backup.md) — this heuristic is
    used both ways: as an informational note when adding a source folder,
    and as a hard warning when choosing the data home.
    """
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    markers = (
        "/Library/Mobile Documents/",  # iCloud Drive
        "/Library/CloudStorage/",      # iCloud Drive (new location) + Dropbox/OneDrive/Google Drive via File Provider
        "/Dropbox/",
        "/Google Drive/",
        "/OneDrive",
    )
    return any(marker in resolved for marker in markers)


def exclusive_lock(fd: int) -> None:
    """Take an exclusive, non-blocking advisory lock on file descriptor
    `fd`. Raises BlockingIOError if another process already holds it.
    """
    if sys.platform in ("darwin", "linux"):
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    raise UnsupportedPlatform(
        f"Exclusive file locking is not yet implemented for platform {sys.platform!r} "
        "(needs an msvcrt.locking-based adapter on Windows)."
    )


def release_lock(fd: int) -> None:
    if sys.platform in ("darwin", "linux"):
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    raise UnsupportedPlatform(f"Lock release is not yet implemented for platform {sys.platform!r}.")
