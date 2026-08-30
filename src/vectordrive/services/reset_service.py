"""Safely discard VectorDrive-owned derived state and create a fresh DB.

The reset boundary is deliberately an allow-list.  It never walks registered
roots and never searches for files by extension, so source documents and all
adjacent ``*.vectordrive.md`` sidecars (including ``final`` sidecars) are
outside its reach.  ``config.toml`` is application state, but is durable user
configuration rather than a derived artifact and is intentionally preserved.
"""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from vectordrive.config import DB_FILENAME, get_config_path, load_config

HOME_IDENTITY_FILE = ".vectordrive-home"
from vectordrive.db.connection import connect
from vectordrive.services.locking import index_lock

# Exact, VectorDrive-owned children of VECTORDRIVE_HOME.  Adding a target is a
# deliberate code change; reset must never glob or recursively scan data_home.
DERIVED_FILES = (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm")
DERIVED_DIRECTORIES = ("cache", "logs", "backups", "review_handoffs")
RECREATED_DIRECTORIES = ("cache", "logs")


@dataclass(frozen=True)
class ResetResult:
    database_existed: bool
    removed: tuple[str, ...]


def _validated_home(data_home: Path) -> Path:
    home = Path(data_home).expanduser().resolve(strict=False)
    if home == Path(home.anchor):
        raise ValueError("Refusing to use a filesystem root as VECTORDRIVE_HOME")
    marker = home / HOME_IDENTITY_FILE
    config_path = home / "config.toml"
    env_home = os.environ.get("VECTORDRIVE_HOME")
    explicitly_selected = env_home and Path(env_home).expanduser().resolve(strict=False) == home
    if not marker.exists() and not config_path.exists() and not explicitly_selected:
        raise ValueError("not a recognized VectorDrive data home")
    if config_path.exists():
        try:
            config = load_config(config_path)
        except Exception as exc:
            raise ValueError("not a recognized VectorDrive data home") from exc
        for registered in config.indexed_folders:
            root = Path(registered).expanduser().resolve(strict=False)
            if home == root or home.is_relative_to(root) or root.is_relative_to(home):
                raise ValueError("data home overlaps registered source folder")
    if not marker.exists():
        marker.write_text("VectorDrive data home v1\n", encoding="utf-8")
    return home


def reset_derived_state(data_home: Path) -> ResetResult:
    """Reset only allow-listed derived artifacts beneath ``data_home``.

    The shared index lock makes the operation fail fast while a CLI or GUI
    processing job is active.  Existing targets are first atomically renamed
    into a private quarantine directory.  If fresh schema creation fails they
    are moved back, leaving the previous state intact.
    """
    home = _validated_home(data_home)
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / DB_FILENAME
    with index_lock(home, source="cli", root=None):
        database_existed = db_path.exists()
        quarantine = home / f".reset-in-progress-{uuid.uuid4().hex}"
        moved: list[tuple[Path, Path]] = []
        removed: list[str] = []
        quarantine.mkdir(mode=0o700)
        recreation_started = False
        try:
            for name in (*DERIVED_FILES, *DERIVED_DIRECTORIES):
                target = home / name
                if not target.exists() and not target.is_symlink():
                    continue
                quarantined = quarantine / name
                os.replace(target, quarantined)
                moved.append((target, quarantined))
                removed.append(name)

            recreation_started = True
            db = connect(db_path)
            db.conn.close()
            for name in RECREATED_DIRECTORIES:
                (home / name).mkdir(mode=0o700, exist_ok=True)
        except Exception:
            if recreation_started:
                # Remove only files/directories freshly created by this reset,
                # then atomically restore every old target from quarantine.
                for name in (*DERIVED_FILES, *RECREATED_DIRECTORIES):
                    target = home / name
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    elif target.exists() or target.is_symlink():
                        target.unlink()
            for target, quarantined in reversed(moved):
                os.replace(quarantined, target)
            shutil.rmtree(quarantine)
            raise

        # Permanently dispose the quarantined derived artifacts only after
        # the fresh database and required empty directories exist. Symlinks
        # inside quarantine are unlinked, never followed.
        shutil.rmtree(quarantine)

    return ResetResult(database_existed=database_existed, removed=tuple(removed))
