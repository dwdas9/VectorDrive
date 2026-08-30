"""Review panel "Remove from VectorDrive": exclude a source file so future
Markdown generation and indexing skip it, and purge everything VectorDrive
already derived from it — without ever touching the source document.

The exclusion is stored in `config.toml` (durable — it survives
`vectordrive reset` and a Full Rebuild) as the source of truth, and
mirrored into the derived database's `file_exclusions` table so it is
queryable inside the same transaction that purges a file's rows. Identity
is root-relative first (root identity + relative path, rewritten by
folders_service.relink_folder on a relink), with a normalized absolute
path kept only as a fallback.

Nothing here reads, writes, moves, or deletes the original source file.
The single filesystem write is the removal of the adjacent, VectorDrive-
generated `*.vectordrive.md` sidecar, and only when its name and recorded
path both confirm it is that generated artifact.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vectordrive.config import Config, ExcludedFile, path_is_inside, update_config
from vectordrive.pipeline.indexer import _reown_shared_extracted_docs
from vectordrive.pipeline.markdown_artifact import is_vectordrive_markdown_name, sidecar_path_for
from vectordrive.services.errors import VectorDriveError

logger = logging.getLogger("vectordrive.exclusions")


@dataclass(frozen=True)
class ExclusionResult:
    file_id: int
    root_path: str
    relative_path: str
    abs_path: str
    sidecar_removed: bool
    purged: bool

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "root_path": self.root_path,
            "relative_path": self.relative_path,
            "abs_path": self.abs_path,
            "sidecar_removed": self.sidecar_removed,
            "purged": self.purged,
            # Stated back to the UI so the source-preservation promise is
            # visible in the success payload, not just the confirm dialog.
            "source_preserved": True,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_resolve(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def resolve_excluded_abs_paths(config: Config) -> set[str]:
    """Every currently-excluded file, as a set of normalized absolute path
    strings, for a scan to filter against. Resolves root-relative identity
    against the *current* registered folder list (so a relinked root still
    matches), and always also includes the stored absolute fallback.
    """
    registered: dict[str, Path] = {}
    for folder in config.indexed_folders:
        resolved = _safe_resolve(folder)
        registered[resolved] = Path(resolved)

    out: set[str] = set()
    for ex in config.excluded_files:
        if ex.root_path and ex.relative_path:
            root_resolved = _safe_resolve(ex.root_path)
            if root_resolved in registered:
                out.add(_safe_resolve(registered[root_resolved] / ex.relative_path))
        if ex.abs_path:
            out.add(_safe_resolve(ex.abs_path))
    return out


def rebuild_exclusions_table(conn: sqlite3.Connection, config: Config) -> None:
    """Rebuild the derived `file_exclusions` table from config.toml (the
    source of truth). Safe to call any time; the table is disposable.
    """
    now = _now_iso()
    with conn:
        conn.execute("DELETE FROM file_exclusions")
        for ex in config.excluded_files:
            conn.execute(
                "INSERT OR REPLACE INTO file_exclusions "
                "(root_path, relative_path, abs_path, reason, excluded_at) VALUES (?, ?, ?, ?, ?)",
                (ex.root_path, ex.relative_path, ex.abs_path, "config_sync", now),
            )


def _resolve_root_relationship(
    conn: sqlite3.Connection, file_row: sqlite3.Row, abs_path: str
) -> tuple[str, str]:
    """(root_path, relative_path) for the excluded file, verified against
    the roots table. Raises if a recorded root identity is inconsistent
    with the file's path (never trust the stored strings blindly).
    """
    root_id = file_row["root_id"]
    if root_id is not None:
        root = conn.execute("SELECT path FROM roots WHERE id = ?", (root_id,)).fetchone()
        if root is not None:
            root_path = _safe_resolve(root["path"])
            if not path_is_inside(Path(abs_path), Path(root_path)):
                raise VectorDriveError(
                    "That file's recorded folder no longer contains it.",
                    code="root_mismatch",
                    hint="Rescan or relink the folder, then try again.",
                )
            relative_path = file_row["relative_path"]
            if not relative_path:
                try:
                    relative_path = str(Path(abs_path).relative_to(root_path))
                except ValueError:
                    relative_path = ""
            return root_path, (relative_path or "")
    # Legacy / bare-CLI file with no registered root: fall back to whatever
    # relative_path was recorded (may be empty) and rely on abs_path.
    return "", (file_row["relative_path"] or "")


def _persist_exclusion_to_config(config_path: Path | None, excluded: ExcludedFile) -> Config:
    def _mutate(cfg: Config) -> None:
        kept = [
            e
            for e in cfg.excluded_files
            if e.abs_path != excluded.abs_path
            and not (e.root_path == excluded.root_path and e.relative_path == excluded.relative_path)
        ]
        kept.append(excluded)
        cfg.excluded_files = kept

    return update_config(_mutate, config_path)


def _remove_generated_sidecar(recorded_markdown_path: str | None, source_path: Path) -> bool:
    """Delete the one adjacent VectorDrive-generated sidecar, if present.
    Never follows a symlink, never deletes anything that is not a plain
    file whose name carries the `*.vectordrive.md` suffix, and never the
    source document.
    """
    candidate = sidecar_path_for(source_path)
    if recorded_markdown_path:
        recorded = Path(recorded_markdown_path)
        if _safe_resolve(recorded) != _safe_resolve(candidate):
            # Database fields are derived state, not deletion authority. A
            # stale or corrupted markdown_path must never broaden this
            # operation beyond the one canonical sidecar adjacent to the
            # source document.
            logger.warning("event=sidecar_path_mismatch action=canonical_only")
    if not is_vectordrive_markdown_name(candidate):
        return False
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return False
        if candidate.resolve() == source_path.resolve():
            return False  # paranoia: never the source
        candidate.unlink()
        return True
    except OSError as exc:
        logger.warning("event=sidecar_remove_failed reason=%s", exc.strerror or str(exc))
        return False


def _remove_vectors_for_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    """Remove sqlite-vec rows before their owning chunks are cascaded.

    ``vec_chunks`` is intentionally outside the relational FK graph, so a
    file DELETE cannot clean it automatically.  The table name is fixed by
    VectorDrive (never caller input), and absence is the normal FTS-only
    case.
    """
    if not chunk_ids:
        return
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec_chunks'"
    ).fetchone()
    if table_exists is None:
        return
    placeholders = ",".join("?" for _ in chunk_ids)
    conn.execute(f"DELETE FROM vec_chunks WHERE chunk_id IN ({placeholders})", chunk_ids)


def remove_file_from_vectordrive(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    config_path: Path | None,
    data_home: Path | None = None,
) -> ExclusionResult:
    """Exclude the file identified by `file_id` (a database row id, never a
    caller-supplied path) and purge every derived row for it.

    Order: durable config exclusion first, then a single-transaction DB
    purge (cascades to extracted_docs/pages/chunks/FTS/quality-reviews/
    handoffs), then removal of the generated sidecar only. The source
    document is never read or written.
    """
    file_id = int(file_id)
    row = conn.execute(
        "SELECT id, path, root_id, relative_path, markdown_path FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        raise VectorDriveError(
            "That file is not in VectorDrive's catalogue.", code="file_not_found"
        )

    # Authorization: the Review panel can only remove a file that is
    # actually in the review queue (has at least one page_quality_reviews
    # row). Mirrors mcp/path_safety.py — resolve identity from the DB, act
    # only on what the DB already vouches for.
    if conn.execute(
        "SELECT 1 FROM page_quality_reviews WHERE file_id = ? LIMIT 1", (file_id,)
    ).fetchone() is None:
        raise VectorDriveError(
            "That file has no review-queue entry, so it can't be removed from here.",
            code="not_in_review_queue",
        )

    abs_path = _safe_resolve(row["path"])
    root_path, relative_path = _resolve_root_relationship(conn, row, abs_path)
    excluded = ExcludedFile(
        root_path=root_path, relative_path=relative_path, abs_path=abs_path
    )

    # 1. Durable exclusion (config.toml). Written first so the file stays
    #    excluded even if the purge below is interrupted; a retry re-runs
    #    the purge harmlessly.
    _persist_exclusion_to_config(config_path, excluded)

    # 2. Transactional purge of all derived rows + the DB-side mirror.
    try:
        chunk_ids = [
            int(chunk["id"])
            for chunk in conn.execute(
                "SELECT id FROM chunks WHERE file_id = ?", (file_id,)
            ).fetchall()
        ]
        _remove_vectors_for_chunks(conn, chunk_ids)
        _reown_shared_extracted_docs(conn, [file_id])
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.execute(
            "INSERT OR REPLACE INTO file_exclusions "
            "(root_path, relative_path, abs_path, reason, excluded_at) VALUES (?, ?, ?, ?, ?)",
            (excluded.root_path, excluded.relative_path, excluded.abs_path,
             "review_panel_remove", _now_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 3. Remove ONLY the generated sidecar (never the source document).
    sidecar_removed = _remove_generated_sidecar(row["markdown_path"], Path(row["path"]))

    logger.info(
        "event=file_removed_from_vectordrive file_id=%s root_relative=%s sidecar_removed=%s",
        file_id,
        f"{Path(root_path).name}/{relative_path}" if root_path else "(no root)",
        sidecar_removed,
    )
    return ExclusionResult(
        file_id=file_id,
        root_path=root_path,
        relative_path=relative_path,
        abs_path=abs_path,
        sidecar_removed=sidecar_removed,
        purged=True,
    )
