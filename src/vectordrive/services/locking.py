"""The concurrent-index-writer guard, shared by the CLI and the GUI.

Two `index_folder()` calls against the same database at once would
interleave writes with no coordination — SQLite's WAL mode tolerates
concurrent connections, but nothing stops two indexers stepping on each
other's `files` row updates. This module is the single mutual-exclusion
point: one file, `VECTORDRIVE_HOME/index.lock`, held with an OS advisory
lock (fcntl.flock via services.platform_paths, non-blocking).

Using `flock` rather than a stale-PID-file heuristic means the lock is
released automatically by the OS the moment its holder process dies
(crash, kill -9, force-quit) — there is no "is the PID from the lock file
still alive?" logic to get wrong.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Literal

from vectordrive.services import platform_paths
from vectordrive.services.errors import IndexLocked

LOCK_FILENAME = "index.lock"


@dataclass(frozen=True)
class LockHolder:
    pid: int
    source: Literal["cli", "gui"]
    started_at: str
    root: str | None


def _lock_path(data_home: Path) -> Path:
    return Path(data_home) / LOCK_FILENAME


@contextmanager
def index_lock(
    data_home: Path, *, source: Literal["cli", "gui"], root: str | None = None
) -> Iterator[LockHolder]:
    """Acquire the exclusive index-writer lock for the duration of the
    `with` block. Raises IndexLocked (never blocks) if another process
    already holds it — see read_lock_holder() for who.
    """
    data_home = Path(data_home)
    data_home.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(data_home)

    holder = LockHolder(pid=os.getpid(), source=source, started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), root=root)

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            platform_paths.exclusive_lock(fd)
        except BlockingIOError:
            existing = read_lock_holder(data_home)
            raise IndexLocked(
                "Another indexing run is already in progress"
                + (f" (started by the {existing.source}, {existing.started_at})" if existing else "")
                + ".",
                holder=existing,
            ) from None

        # Write holder metadata AFTER acquiring the lock, so a reader
        # never observes a lock file whose contents don't match its
        # actual holder (a race that a stale-PID design would be prone
        # to; flock's own atomicity makes this window safe here).
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(asdict(holder)).encode("utf-8"))
        os.fsync(fd)

        yield holder
    finally:
        try:
            platform_paths.release_lock(fd)
        except Exception:  # noqa: BLE001 - best-effort; os.close still runs
            pass
        os.close(fd)


def read_lock_holder(data_home: Path) -> LockHolder | None:
    """Non-blocking peek at who (if anyone) currently holds the index
    lock, for the GUI to display ("Indexing already running, started by
    the command line at 09:12"). Returns None if the lock file doesn't
    exist, is empty, or isn't currently held (i.e. flock would succeed).

    This never itself holds the lock — it takes a shared, immediately-
    released probe purely to distinguish "held by someone" from "not
    held", then reads the metadata written by whoever holds it.
    """
    lock_path = _lock_path(Path(data_home))
    if not lock_path.exists():
        return None

    fd = os.open(lock_path, os.O_RDONLY)
    try:
        try:
            platform_paths.exclusive_lock(fd)
        except BlockingIOError:
            pass  # held by someone else — fall through to read metadata
        else:
            # We just acquired it ourselves, meaning nobody else holds it.
            platform_paths.release_lock(fd)
            return None

        try:
            data = os.read(fd, 65536)
        except OSError:
            return None
        if not data:
            return None
        try:
            parsed = json.loads(data.decode("utf-8"))
            return LockHolder(**parsed)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
    finally:
        os.close(fd)
