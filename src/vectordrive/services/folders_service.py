"""Makes Config.indexed_folders live: the GUI's Folders catalogue.

v0.1 declared this field and round-tripped it through config.toml, but
nothing read or wrote it — G2's run_index() auto-registration and this
module together make it the real source of truth for "which folders is
VectorDrive managing", shared by the CLI and GUI alike.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vectordrive.config import (
    Config,
    get_config_path,
    get_data_home,
    load_config,
    path_is_inside,
    update_config,
)
from vectordrive.pipeline.indexer import _reown_shared_extracted_docs
from vectordrive.services.errors import InvalidFolder
from vectordrive.services.platform_paths import is_cloud_synced, is_network_volume
from vectordrive.services.roots_service import backfill_files_under_root, delete_root, ensure_root, get_root_by_path


@dataclass(frozen=True)
class FolderRow:
    path: str
    exists: bool
    indexed: int
    failed: int
    unsupported: int
    last_indexed_at: str | None
    total_bytes: int | None
    is_cloud_synced: bool
    # G12 finding: a subset of `indexed` files whose OCR failed or
    # produced no searchable text (files.ocr_warning IS NOT NULL) — see
    # pipeline/indexer.py. Counted separately, not subtracted from
    # `indexed`, so the two numbers stay independently meaningful:
    # `indexed` is still "the pipeline completed", `warnings` is "but N
    # of those may not actually be searchable".
    warnings: int
    # Durable history, independent of current counts. A completed processing
    # run is authoritative; first_indexed_at covers indexes created before
    # the processing-run ledger existed.
    has_index_history: bool


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_folders(conn: sqlite3.Connection, config: Config) -> list[FolderRow]:
    """Per-folder stats, derived from the DB at read time (never
    persisted alongside the path) — so config and DB can never disagree.
    """
    rows: list[FolderRow] = []
    for folder_str in config.indexed_folders:
        folder = Path(folder_str)
        pattern = _like_escape(folder_str) + "/%"
        db_rows = conn.execute(
            "SELECT status, COUNT(*) AS n, SUM(size) AS total_size, MAX(last_indexed_at) AS last_at "
            "FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\' GROUP BY status",
            (folder_str, pattern),
        ).fetchall()
        counts = {"indexed": 0, "failed": 0, "unsupported": 0}
        total_bytes = 0
        last_indexed_at = None
        for r in db_rows:
            counts[r["status"]] = counts.get(r["status"], 0) + r["n"]
            if r["total_size"]:
                total_bytes += r["total_size"]
            if r["last_at"] and (last_indexed_at is None or r["last_at"] > last_indexed_at):
                last_indexed_at = r["last_at"]
        warning_count = conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE (path = ? OR path LIKE ? ESCAPE '\\') "
            "AND status = 'indexed' AND ocr_warning IS NOT NULL",
            (folder_str, pattern),
        ).fetchone()["n"]
        has_completed_run = conn.execute(
            "SELECT 1 FROM processing_runs "
            "WHERE phase = 'index' AND root_path = ? AND status = 'completed' LIMIT 1",
            (folder_str,),
        ).fetchone()
        has_legacy_index = conn.execute(
            "SELECT 1 FROM files WHERE (path = ? OR path LIKE ? ESCAPE '\\') "
            "AND (first_indexed_at IS NOT NULL OR last_indexed_at IS NOT NULL) LIMIT 1",
            (folder_str, pattern),
        ).fetchone()
        rows.append(
            FolderRow(
                path=folder_str,
                exists=folder.exists(),
                indexed=counts.get("indexed", 0),
                failed=counts.get("failed", 0),
                unsupported=counts.get("unsupported", 0),
                last_indexed_at=last_indexed_at,
                total_bytes=total_bytes or None,
                is_cloud_synced=is_cloud_synced(folder),
                warnings=warning_count,
                has_index_history=bool(has_completed_run or has_legacy_index),
            )
        )
    return rows


def list_file_warnings(conn: sqlite3.Connection, folder_str: str) -> list[dict]:
    """Files under `folder_str` with status='indexed' but a non-NULL
    ocr_warning (see pipeline/indexer.py) — the "useful error message"
    behind list_folders()'s aggregate `warnings` count, for the GUI to
    show on request rather than crowding the default folder card.
    """
    pattern = _like_escape(folder_str) + "/%"
    rows = conn.execute(
        "SELECT path, ocr_warning FROM files WHERE (path = ? OR path LIKE ? ESCAPE '\\') "
        "AND status = 'indexed' AND ocr_warning IS NOT NULL ORDER BY path",
        (folder_str, pattern),
    ).fetchall()
    return [{"path": r["path"], "warning": r["ocr_warning"]} for r in rows]


def validate_folder(path: Path, config: Config, data_home: Path) -> list[str]:
    """Structural/policy checks (fast, no full tree walk — that's
    services.index_service.preview_folder's job). Raises InvalidFolder
    for anything that must hard-block adding the folder; returns a list
    of non-fatal warning strings for anything the user should just know
    about (network volume, cloud-synced).
    """
    path = Path(path)
    warnings: list[str] = []

    if not path.exists():
        raise InvalidFolder(f"{path} does not exist.", hint="Choose a different folder.")
    if not path.is_dir():
        raise InvalidFolder(f"{path} is not a folder.", hint="Choose a folder, not a file.")
    if not os.access(path, os.R_OK):
        raise InvalidFolder(f"{path} is not readable.", hint="Check the folder's permissions.")

    resolved = path.resolve()
    home = Path.home()
    if resolved == Path("/") or resolved == home:
        raise InvalidFolder(
            f"{resolved} is your entire home folder or the root of the disk.",
            hint="Choose a more specific subfolder instead.",
        )

    data_home_resolved = Path(data_home).resolve()
    if path_is_inside(resolved, data_home_resolved):
        raise InvalidFolder(
            f"{resolved} is inside VectorDrive's own data folder.",
            hint="Choose a folder outside VectorDrive's data directory.",
        )

    for other_str in config.indexed_folders:
        other = Path(other_str).resolve()
        if resolved == other:
            raise InvalidFolder(f"{resolved} is already in your folder list.")
        if path_is_inside(resolved, other):
            raise InvalidFolder(
                f"{resolved} is inside the already-added folder {other}.",
                hint="Remove the parent folder first, or choose a different one.",
            )
        if path_is_inside(other, resolved):
            raise InvalidFolder(
                f"{resolved} contains the already-added folder {other}.",
                hint="Remove that folder first, or choose a more specific one.",
            )

    if is_network_volume(resolved):
        warnings.append("This folder is on a network volume — indexing may be slow, and files must stay reachable.")
    if is_cloud_synced(resolved):
        warnings.append(
            "This folder is inside a cloud-sync folder. Indexing it is fine; VectorDrive's database stays outside it."
        )

    return warnings


def add_folder(path: Path, *, config_path: Path | None = None) -> Config:
    """Idempotently add `path` to config.indexed_folders under the atomic
    read-modify-write lock. Callers should run validate_folder() first —
    this function does not re-validate, it only persists.
    """
    root_str = str(Path(path).resolve())

    def _mutate(cfg: Config) -> None:
        if root_str not in cfg.indexed_folders:
            cfg.indexed_folders.append(root_str)

    return update_config(_mutate, config_path)


def remove_folder(
    conn: sqlite3.Connection, path: Path, *, purge_index: bool, config_path: Path | None = None
) -> tuple[Config, int]:
    """Remove `path` from config.indexed_folders. Never touches the
    filesystem — only catalogue/index metadata.

    G13 finding: this used to call reset_files_under_root() (--rebuild's
    unconditional "delete everything under root" helper) for purge_index,
    AND deregistered the folder from config *before* attempting any DB
    delete. Two bugs followed from that: (1) a file also covered by
    another still-registered folder was deleted too — no overlap
    awareness; (2) a failure partway through the DB delete left the
    folder deregistered (gone from the Folders screen) while its rows —
    and therefore its FTS/vector search hits — silently survived, the
    exact "looks removed but isn't" failure mode this fixes. Fixed here
    with dedicated overlap-aware deletion (a file is only purged if it is
    *exclusively* under `root` — not also under any folder remaining in
    indexed_folders after this removal), wrapped so a delete failure
    rolls back and re-raises *before* config.toml is ever touched — a
    failed purge leaves the folder still registered with its index
    intact, never the reverse.

    Deletion cascades (schema.sql FKs, PRAGMA foreign_keys=ON) to
    extracted_docs/pages/chunks/chunk_runs, and chunks' AFTER DELETE
    trigger keeps fts_chunks in sync — so purged files stop appearing in
    FTS results immediately, atomically, as part of this same DELETE.
    sqlite-vec's vec_chunks has no FK/trigger tie to chunks (a separate
    virtual table), so it is *not* cleaned up here — gui/api.py's
    remove_folder() does that immediately afterward by calling the same
    non-readonly vector-index refresh already used by search, so vector/
    hybrid search agree with FTS right after removal, not just after the
    next full index run.
    """
    root = Path(path).resolve()
    root_str = str(root)

    current_config = load_config(config_path or get_config_path())
    remaining_folders = [p for p in current_config.indexed_folders if p != root_str]

    purged_count = 0
    if purge_index:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        exclusive_ids = [
            row["id"]
            for row in rows
            if path_is_inside(Path(row["path"]), root)
            and not any(path_is_inside(Path(row["path"]), Path(other)) for other in remaining_folders)
        ]
        if exclusive_ids:
            placeholders = ",".join("?" for _ in exclusive_ids)
            try:
                _reown_shared_extracted_docs(conn, exclusive_ids)
                conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", exclusive_ids)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        purged_count = len(exclusive_ids)

    # Deregister only after any DB purge above has committed — an
    # exception from the try block above returns control to the caller
    # before this line is ever reached, so a failed purge never leaves
    # config.toml out of sync with what's actually in the database.
    def _mutate(cfg: Config) -> None:
        cfg.indexed_folders = [p for p in cfg.indexed_folders if p != root_str]

    new_config = update_config(_mutate, config_path)

    # Stage 3: an unregistered root keeps no identity row — deregistering
    # (above) always drops the roots.id, whether or not this removal also
    # purged the index. If the folder is re-added later, ensure_root()
    # simply creates a fresh identity; there is no relink-across-removal.
    delete_root(conn, root_str)

    return new_config, purged_count


@dataclass(frozen=True)
class RelinkValidation:
    sampled: int  # relative paths sampled from the DB (<= 200)
    found: int  # how many exist under the candidate root
    overlap_ratio: float  # found / sampled, 1.0 when sampled == 0
    ok: bool  # overlap_ratio >= 0.5 or sampled == 0
    message: str  # human summary for the GUI warning


def _relink_structural_checks(new_path: Path, old: str, config: Config, data_home: Path) -> Path:
    """Structural validity of `new_path` as a relink target — the same
    individual checks as validate_folder(), but NOT a call to
    validate_folder() itself: that function hard-rejects a path already
    present in config.indexed_folders, which describes `old` exactly
    (the folder being relinked stays registered until the relink
    commits). `old` is skipped when checking overlap with OTHER
    registered folders. Returns the resolved new path on success; raises
    InvalidFolder on any hard failure.
    """
    new_path = Path(new_path)
    if not new_path.exists():
        raise InvalidFolder(f"{new_path} does not exist.", hint="Choose a different folder.")
    if not new_path.is_dir():
        raise InvalidFolder(f"{new_path} is not a folder.", hint="Choose a folder, not a file.")
    if not os.access(new_path, os.R_OK):
        raise InvalidFolder(f"{new_path} is not readable.", hint="Check the folder's permissions.")

    resolved = new_path.resolve()
    home = Path.home()
    if resolved == Path("/") or resolved == home:
        raise InvalidFolder(
            f"{resolved} is your entire home folder or the root of the disk.",
            hint="Choose a more specific subfolder instead.",
        )

    data_home_resolved = Path(data_home).resolve()
    if path_is_inside(resolved, data_home_resolved):
        raise InvalidFolder(
            f"{resolved} is inside VectorDrive's own data folder.",
            hint="Choose a folder outside VectorDrive's data directory.",
        )

    for other_str in config.indexed_folders:
        if other_str == old:
            continue
        other = Path(other_str).resolve()
        if resolved == other:
            raise InvalidFolder(f"{resolved} is already in your folder list.")
        if path_is_inside(resolved, other):
            raise InvalidFolder(
                f"{resolved} is inside the already-added folder {other}.",
                hint="Remove the parent folder first, or choose a different one.",
            )
        if path_is_inside(other, resolved):
            raise InvalidFolder(
                f"{resolved} contains the already-added folder {other}.",
                hint="Remove that folder first, or choose a more specific one.",
            )

    return resolved


def validate_relink(
    conn: sqlite3.Connection,
    old_path: str,
    new_path: Path,
    config: Config,
    *,
    data_home: Path | None = None,
) -> RelinkValidation:
    """How much of the DB's expected content is actually present at
    `new_path`, before committing to a relink — the GUI's "N of M indexed
    files were found here" warning. Never mutates anything (read-only:
    safe to call speculatively as the user types/browses).
    """
    home = Path(data_home) if data_home is not None else get_data_home()
    old = str(Path(old_path).resolve())
    resolved_new = _relink_structural_checks(Path(new_path), old, config, home)

    root = get_root_by_path(conn, old)
    rels: list[str] = []
    if root is not None:
        rels = [
            r["relative_path"]
            for r in conn.execute(
                "SELECT relative_path FROM files WHERE root_id = ? AND relative_path IS NOT NULL "
                "ORDER BY relative_path LIMIT 200",
                (root["id"],),
            ).fetchall()
        ]
    if not rels:
        # Legacy fallback: a root not yet backfilled (never indexed since
        # Stage 3 landed, or never indexed at all) has no relative_path
        # rows to sample from — derive them from files.path's old/%
        # prefix instead, same LIMIT/order.
        pattern = _like_escape(old) + "/%"
        legacy_rows = conn.execute(
            "SELECT path FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\' ORDER BY path LIMIT 200",
            (old, pattern),
        ).fetchall()
        prefix = old + "/"
        rels = [r["path"][len(prefix):] for r in legacy_rows if r["path"].startswith(prefix)]

    sampled = len(rels)
    found = sum(1 for r in rels if (resolved_new / r).exists())
    ratio = 1.0 if sampled == 0 else found / sampled
    ok = sampled == 0 or ratio >= 0.5
    message = f"{found} of {sampled} indexed files were found in this folder."
    return RelinkValidation(sampled=sampled, found=found, overlap_ratio=ratio, ok=ok, message=message)


def relink_folder(
    conn: sqlite3.Connection,
    old_path: str,
    new_path: Path,
    *,
    config_path: Path | None = None,
    data_home: Path | None = None,
) -> Config:
    """Point a registered root at a new physical location while keeping
    its SAME identity (roots.id) — the "Relink Folder" GUI action for a
    root the GUI shows as Missing. A subsequent re-index compares files
    by (root_id, relative_path) rather than absolute path, so unchanged
    files/structure are recognized as unchanged — not thousands of
    deletes+adds (the critical product rule this stage exists for).

    One transaction: roots.path (+ last_relinked_at) and every affected
    files.path are updated together, so a failure partway through (e.g. a
    files.path UNIQUE collision — a stray row already at the destination)
    rolls back completely, leaving the root still pointed at `old_path`.
    config.toml is only touched after that transaction commits, mirroring
    remove_folder()'s "DB before config" ordering.
    """
    home = Path(data_home) if data_home is not None else get_data_home()
    current_config = load_config(config_path or get_config_path(home))
    old = str(Path(old_path).resolve())

    resolved_new = _relink_structural_checks(Path(new_path), old, current_config, home)
    new = str(resolved_new)

    # ensure_root/backfill work against DB strings only, not the
    # filesystem — safe to call while old_path's directory is missing
    # (the exact situation a relink is performed from). A root never
    # explicitly registered before (legacy DB, or never indexed since
    # Stage 3 landed) gets its identity row created here.
    root_id = ensure_root(conn, old)
    backfill_files_under_root(conn, root_id, old)

    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT id, relative_path FROM files WHERE root_id = ?", (root_id,)
    ).fetchall()
    try:
        conn.execute(
            "UPDATE roots SET path = ?, last_relinked_at = ? WHERE id = ?", (new, now, root_id)
        )
        for row in rows:
            rel = row["relative_path"]
            new_abs = new if rel == "." else f"{new}/{rel}"
            conn.execute("UPDATE files SET path = ? WHERE id = ?", (new_abs, row["id"]))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise InvalidFolder(
            f"{new_path} already has indexed entries that conflict with this folder's index.",
            hint="Choose a different folder, or remove the conflicting folder's index first.",
        ) from exc
    except Exception:
        conn.rollback()
        raise

    def _mutate(cfg: Config) -> None:
        cfg.indexed_folders = [new if p == old else p for p in cfg.indexed_folders]
        # Keep Review-panel exclusions stable across the relink: their
        # identity is root-relative, so only the root anchor moves.
        if cfg.excluded_files:
            from vectordrive.config import ExcludedFile

            rewritten: list[ExcludedFile] = []
            for ex in cfg.excluded_files:
                if ex.root_path == old:
                    rel = ex.relative_path
                    rewritten.append(
                        ExcludedFile(
                            root_path=new,
                            relative_path=rel,
                            abs_path=(str(Path(new) / rel) if rel else new),
                        )
                    )
                else:
                    rewritten.append(ex)
            cfg.excluded_files = rewritten

    return update_config(_mutate, config_path)


def adopt_existing_folders(conn: sqlite3.Connection) -> list[str]:
    """v0.1 -> v0.2 backfill: find candidate root folders for files that
    are already indexed but not yet in config.indexed_folders (true for
    every pre-existing v0.1 database, since indexed_folders was
    vestigial before G2). Returns candidates for the GUI's one-time
    "adopt existing folders?" confirmation card — nothing is written
    here; the GUI calls add_folder() per confirmed path.

    Simplified from the plan's full clustering design: computes one
    overall common ancestor via os.path.commonpath(); if that's too
    broad to be a sensible "folder" (home directory or disk root,
    implying the indexed files come from multiple unrelated locations),
    falls back to each file's immediate parent directory instead of
    silently proposing to scope the whole home folder.
    """
    paths = [r["path"] for r in conn.execute("SELECT DISTINCT path FROM files").fetchall()]
    if not paths:
        return []
    try:
        common = os.path.commonpath(paths)
    except ValueError:
        return []
    common_path = Path(common)
    if common_path in (Path("/"), Path.home()):
        return sorted({str(Path(p).parent) for p in paths})
    return [str(common_path)]
