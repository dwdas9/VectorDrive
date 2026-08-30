"""Atomic, crash-safe file writes and backups.

Used by config.save_config() (fixing a latent v0.1 bug — the original
save_config() truncates and writes in place, so a crash mid-write or a
concurrent writer can corrupt config.toml), by Claude Desktop config
writes (G8), and by gui_state.json (G3).
"""
from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write `data` to `path` atomically: write to a sibling tmp file in
    the same directory (so os.replace() is a same-filesystem rename, never
    a cross-filesystem copy), fsync it, then rename over the destination.
    A reader can never observe a partially-written file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def timestamped_backup(path: Path, *, suffix: str = ".vectordrive-backup", keep: int = 10) -> Path | None:
    """Copy `path` to `path.<suffix>.<timestamp>` and prune old backups
    beyond `keep`. Returns the new backup's path, or None if `path`
    doesn't exist (nothing to back up — not an error, e.g. a first-time
    Claude Desktop config write with no prior file).
    """
    path = Path(path)
    if not path.exists():
        return None
    # Second-resolution strftime alone collides when two backups happen
    # within the same second (e.g. a user double-clicking "Back up"), which
    # would silently overwrite the first backup with the second — append a
    # sub-second, monotonically-increasing suffix so every call gets a
    # distinct filename.
    ts = time.strftime("%Y%m%d-%H%M%S")
    micros = time.time_ns() // 1000 % 1_000_000
    backup_path = path.with_name(f"{path.name}{suffix}.{ts}-{micros:06d}")
    shutil.copy2(path, backup_path)

    existing = sorted(
        path.parent.glob(f"{path.name}{suffix}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in existing[keep:]:
        stale.unlink(missing_ok=True)

    return backup_path


class ConcurrentModificationError(RuntimeError):
    """Raised by file_fingerprint_guard when the watched file changed
    underneath us between the fingerprint being taken and the write being
    attempted (e.g. Claude Desktop's config edited by hand mid-preview).
    """


@contextmanager
def file_fingerprint_guard(path: Path, *, expected_mtime_ns: int, expected_size: int) -> Iterator[None]:
    """Raise ConcurrentModificationError if `path`'s mtime/size no longer
    match what was recorded when a preview/diff was generated, rather than
    silently overwriting a change made in between.
    """
    path = Path(path)
    if path.exists():
        stat = path.stat()
        if stat.st_mtime_ns != expected_mtime_ns or stat.st_size != expected_size:
            raise ConcurrentModificationError(
                f"{path} changed on disk since this change was previewed. "
                "Re-open the preview and try again."
            )
    elif expected_mtime_ns != 0 or expected_size != 0:
        raise ConcurrentModificationError(
            f"{path} no longer exists — it was deleted since this change was previewed."
        )
    yield
