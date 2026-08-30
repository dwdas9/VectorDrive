"""Folder-indexing orchestrator: walk a folder, run each supported file
through hashing -> extraction -> OCR -> chunking -> embeddings -> search
index, skip-unchanged, record status, isolate per-file errors, and clean
up stale index entries when a file's content changes.

M10.5 hardens unchanged-file detection (requirements 4-6): file identity
is a normalized (resolved, absolute) path, and the default fast path
checks size + nanosecond mtime BEFORE ever reading/hashing the file's
content — only a metadata change triggers a real content_hash comparison,
and only an actual hash change triggers reprocessing. `verify=True`
(the CLI's --verify) skips straight to the hash comparison every time,
catching a content change disguised behind identical size+mtime, while
still only reprocessing files whose hash actually changed.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from vectordrive.config import path_is_inside

logger = logging.getLogger(__name__)
from vectordrive.pipeline.chunk.base import ChunkerConfig
from vectordrive.pipeline.chunk.cache import get_or_create_chunks
from vectordrive.pipeline.embed.base import DEFAULT_EMBED_BATCH_SIZE, EmbeddingConfig, EmbeddingProvider
from vectordrive.pipeline.embed.embed_chunks import embed_chunks
from vectordrive.pipeline.extract.cache import extract_with_cache
from vectordrive.pipeline.extract.registry import get_extractor_for
from vectordrive.pipeline.canonicalize import build_canonical_document
from vectordrive.pipeline.hashing import sha256_file
from vectordrive.pipeline.ocr.base import OcrEngine
from vectordrive.pipeline.ocr.page_ocr import ocr_needs_ocr_pages
from vectordrive.pipeline.review.assess import assess_and_persist_document
from vectordrive.pipeline.ocr.tiered import DEFAULT_CONFIDENCE_THRESHOLD
from vectordrive.pipeline.structure.analyze import analyze_document_structure
from vectordrive.search.index_builder import build_vector_index
from vectordrive.search.vector_index.base import VectorIndex

_SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".eml": "eml",
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".jpg": "jpg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".pptx": "pptx",
}

# Generic fallback for any extension not in the explicit map above: read a
# small prefix and check whether it looks like text (no null bytes, valid
# UTF-8, mostly printable characters) — the same idea the Unix `file`
# command uses. Catches .json/.yaml/.csv/.xml/.html/.log/.ini/.toml/.rst
# and any future text-ish format without a per-extension entry, while a
# genuine binary (image, audio, video, archive, compiled binary) still
# correctly falls through to "unsupported" exactly as before this existed.
_SNIFF_SAMPLE_SIZE = 8192
_MIN_PRINTABLE_RATIO = 0.95


def _looks_like_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            sample = f.read(_SNIFF_SAMPLE_SIZE)
    except OSError:
        return False
    if not sample:
        return True  # empty file: trivially text, nothing to contradict that
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in sample if b >= 0x20 or b in (0x09, 0x0A, 0x0D))
    return (printable / len(sample)) >= _MIN_PRINTABLE_RATIO


@dataclass
class IndexRunSummary:
    run_id: int
    indexed: int
    skipped: int
    unsupported: int
    failed: int
    cancelled: bool = False  # v0.2 G2: True if should_cancel() ended the run early
    # M-indexing-fix: directories os.walk could not list (permission denied,
    # a transient sync/mount error, ...) -- NOT folded into indexed/skipped/
    # unsupported/failed because we never learn how many files were inside
    # an unlistable directory. A nonzero value means the counts above are a
    # genuine undercount of what's really in the folder, not a complete
    # picture -- see index_folder()'s docstring.
    walk_errors: int = 0


# v0.2 G2: the GUI (G6) needs live progress; on_event is called with a small
# dict per notable moment (see index_folder's docstring below for the
# kinds). should_cancel is polled once per file boundary, never mid-file —
# a half-processed file must never be committed as "indexed".
EventCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: Path) -> str:
    """Canonical identity for a file across runs and invocation styles
    (requirement 4) — resolved (symlinks followed) and absolute, so
    `--path docs` and `--path /abs/.../docs` from the same cwd, or the
    same folder indexed from two different working directories, are
    recognized as the same files rather than creating duplicate rows.
    """
    return str(Path(path).resolve())


WalkErrorCallback = Callable[[Path, OSError], None]


def _iter_files(
    root: Path,
    *,
    should_cancel: CancelCallback | None = None,
    on_error: WalkErrorCallback | None = None,
):
    """Yield files in a stable, memory-bounded walk.

    Sorting ``root.rglob('*')`` used to materialize the entire tree before
    yielding the first path. Besides the memory spike, that meant a cancel
    request could not be observed while a large folder was being discovered.
    ``os.walk`` lets us preserve deterministic per-directory ordering while
    polling cancellation between entries and directories.

    M-indexing-fix: ``os.walk``'s default ``onerror=None`` silently drops an
    entire subtree the instant it can't be listed (permission denied, a
    transient cloud-sync/mount error) -- no exception, no log, nothing
    downstream can ever tell those files existed. `on_error`, when given,
    is called for every such directory AND for every individual file whose
    `stat()` fails (the same silent-drop failure mode `Path.is_file()`
    would otherwise swallow internally) -- so a caller can count/log what
    was skipped instead of it vanishing. Hidden files (dotfiles) are still
    excluded unconditionally, deliberately: that's real OS/app metadata
    (`.DS_Store`, etc.), not a scan failure.
    """

    def _onerror(exc: OSError) -> None:
        if on_error is not None:
            on_error(Path(exc.filename) if exc.filename else root, exc)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        if should_cancel is not None and should_cancel():
            return
        dirnames.sort()
        for filename in sorted(filenames):
            if should_cancel is not None and should_cancel():
                return
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            try:
                is_regular_file = path.is_file()
            except OSError as exc:
                if on_error is not None:
                    on_error(path, exc)
                continue
            if is_regular_file:
                yield path


def _classify(path: Path) -> str | None:
    file_type = _SUPPORTED_EXTENSIONS.get(path.suffix.lower())
    if file_type is not None:
        return file_type
    if _looks_like_text(path):
        return "text"
    return None


def _get_file_row(conn: sqlite3.Connection, normalized_path: str):
    return conn.execute("SELECT * FROM files WHERE path = ?", (normalized_path,)).fetchone()


def _relative_path_under(normalized_path: str, root: Path | None, root_id: int | None) -> str | None:
    """Stage 3: relative_path is only meaningful once a file is attributed
    to a registered root (root_id is not None) — bare pipeline callers
    (direct index_folder() calls, no root registration) pass root_id=None
    and this returns None, leaving files.relative_path untouched/NULL,
    preserving pre-Stage-3 behavior exactly.
    """
    if root_id is None or root is None:
        return None
    return str(Path(normalized_path).relative_to(root))


def _record_unsupported(
    conn: sqlite3.Connection, path: Path, *, root: Path | None = None, root_id: int | None = None
) -> None:
    normalized_path = _normalize_path(path)
    now = _now()
    relative_path = _relative_path_under(normalized_path, root, root_id)
    existing = _get_file_row(conn, normalized_path)
    if existing is not None:
        if root_id is not None:
            conn.execute(
                "UPDATE files SET status = 'unsupported', last_indexed_at = ?, "
                "root_id = ?, relative_path = ? WHERE id = ?",
                (now, root_id, relative_path, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE files SET status = 'unsupported', last_indexed_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
    else:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
            "first_indexed_at, last_indexed_at, root_id, relative_path) VALUES (?, 0, 0.0, 0, '', "
            "'unknown', 'unsupported', ?, ?, ?, ?)",
            (normalized_path, now, now, root_id, relative_path),
        )
    conn.commit()


def _record_failure(
    conn: sqlite3.Connection,
    path: Path,
    file_type: str | None,
    message: str,
    *,
    root: Path | None = None,
    root_id: int | None = None,
) -> None:
    normalized_path = _normalize_path(path)
    now = _now()
    relative_path = _relative_path_under(normalized_path, root, root_id)
    existing = _get_file_row(conn, normalized_path)
    if existing is not None:
        if root_id is not None:
            conn.execute(
                "UPDATE files SET status = 'failed', error_message = ?, last_indexed_at = ?, "
                "root_id = ?, relative_path = ? WHERE id = ?",
                (message, now, root_id, relative_path, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE files SET status = 'failed', error_message = ?, last_indexed_at = ? WHERE id = ?",
                (message, now, existing["id"]),
            )
    else:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
            "error_message, first_indexed_at, last_indexed_at, root_id, relative_path) VALUES "
            "(?, 0, 0.0, 0, '', ?, 'failed', ?, ?, ?, ?, ?)",
            (normalized_path, file_type or "unknown", message, now, now, root_id, relative_path),
        )
    conn.commit()


def _process_one_file(
    conn: sqlite3.Connection,
    path: Path,
    file_type: str,
    *,
    run_id: int | None = None,
    verify: bool,
    tier2_engine: OcrEngine,
    tier3_engine: OcrEngine | None,
    tier4_engine: OcrEngine | None,
    ocr_lang: str,
    ocr_confidence_threshold: float,
    chunk_config: ChunkerConfig,
    embed_provider: EmbeddingProvider,
    embed_config: EmbeddingConfig,
    embed_batch_size: int,
    root: Path | None = None,
    root_id: int | None = None,
    structure_engine=None,
    structure_analyze_native: bool = True,
) -> str:
    """Returns 'indexed' or 'skipped'. Raises on any unhandled failure —
    the caller is responsible for catching it and recording 'failed'
    (error isolation lives at the call site, not here).
    """
    stat = path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns
    normalized_path = _normalize_path(path)
    now = _now()
    relative_path = _relative_path_under(normalized_path, root, root_id)

    existing = _get_file_row(conn, normalized_path)

    # Requirement 5 (default path): if size and mtime_ns both match what
    # was recorded last time, skip WITHOUT reading or hashing the file's
    # content at all. --verify (requirement 6) bypasses this shortcut
    # deliberately, so a content change disguised behind identical
    # metadata is still caught below.
    if (
        not verify
        and existing is not None
        and existing["status"] == "indexed"
        and existing["size"] == size
        and existing["mtime_ns"] == mtime_ns
    ):
        if root_id is not None:
            conn.execute(
                "UPDATE files SET root_id = ?, relative_path = ? WHERE id = ?",
                (root_id, relative_path, existing["id"]),
            )
            conn.commit()
        logger.info(
            "event=file_skip_unchanged run_id=%s path=%s reason=metadata_fast_path",
            run_id, normalized_path,
        )
        return "skipped"

    hash_start = time.perf_counter()
    content_hash = sha256_file(path)
    logger.info(
        "event=stage_complete stage=hash run_id=%s path=%s duration_ms=%d",
        run_id, normalized_path, (time.perf_counter() - hash_start) * 1000,
    )

    if existing is not None and existing["status"] == "indexed" and existing["content_hash"] == content_hash:
        # Metadata looked different (or --verify double-checked anyway),
        # but the content hash proves it's genuinely the same content —
        # e.g. touched, copied, or re-synced. Update the stored metadata
        # so the fast path matches next time, but do not repeat
        # extraction/OCR/chunking/embedding (requirement 5).
        if root_id is not None:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, last_indexed_at = ?, "
                "root_id = ?, relative_path = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, now, root_id, relative_path, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, last_indexed_at = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, now, existing["id"]),
            )
        conn.commit()
        logger.info(
            "event=file_skip_unchanged run_id=%s path=%s reason=content_hash_match",
            run_id, normalized_path,
        )
        return "skipped"

    if existing is None:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
            "error_message, first_indexed_at, last_indexed_at, root_id, relative_path) VALUES "
            "(?, ?, ?, ?, ?, ?, 'failed', NULL, ?, ?, ?, ?)",
            (
                normalized_path, size, stat.st_mtime, mtime_ns, content_hash, file_type, now, now,
                root_id, relative_path,
            ),
        )
        file_id = _get_file_row(conn, normalized_path)["id"]
    else:
        file_id = existing["id"]
        # Content (or file type) changed since last time: any extracted_docs
        # row under a *different* content_hash is now stale — deleting it
        # cascades to its pages/chunks/chunk_runs (schema.sql FKs), which in
        # turn cascades to fts_chunks via the M2 triggers. The vector index
        # side of staleness is handled once, for the whole run, by the
        # final build_vector_index() call in index_folder().
        conn.execute(
            "DELETE FROM extracted_docs WHERE file_id = ? AND content_hash != ?",
            (file_id, content_hash),
        )
        if root_id is not None:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, file_type = ?, "
                "status = 'failed', error_message = NULL, last_indexed_at = ?, root_id = ?, "
                "relative_path = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, content_hash, file_type, now, root_id, relative_path, file_id),
            )
        else:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, file_type = ?, "
                "status = 'failed', error_message = NULL, last_indexed_at = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, content_hash, file_type, now, file_id),
            )
    conn.commit()

    extractor = get_extractor_for(file_type)
    stage_start = time.perf_counter()
    extract_with_cache(conn, file_id, content_hash, path, extractor)
    logger.info(
        "event=stage_complete stage=extract run_id=%s path=%s file_type=%s duration_ms=%d",
        run_id, normalized_path, file_type, (time.perf_counter() - stage_start) * 1000,
    )
    # M14: extracted_docs is a content-addressed cache (schema.sql's
    # UNIQUE(content_hash, extractor_name, extractor_version) — same
    # philosophy as the M7 embeddings cache) shared across ANY files with
    # identical content, not scoped to file_id. A second file with
    # identical content to one already indexed (an exact duplicate copy,
    # or two unrelated empty files) hits the cache under a row owned by
    # the OTHER file's id — looking this row up by (file_id, content_hash)
    # together, as before, matches nothing and crashes. The lookup key
    # must mirror extract/cache.py's own get_cached_extraction() key
    # exactly: content_hash + extractor identity, never file_id.
    extracted_doc_id = conn.execute(
        "SELECT id FROM extracted_docs WHERE content_hash = ? AND extractor_name = ? AND extractor_version = ?",
        (content_hash, extractor.NAME, extractor.VERSION),
    ).fetchone()["id"]

    stage_start = time.perf_counter()
    ocr_outcomes = ocr_needs_ocr_pages(
        conn, extracted_doc_id, path, file_type,
        tier2_engine=tier2_engine, tier3_engine=tier3_engine, tier4_engine=tier4_engine,
        lang=ocr_lang, confidence_threshold=ocr_confidence_threshold,
    )
    ocr_succeeded = sum(1 for o in ocr_outcomes if o.success)
    ocr_failed = len(ocr_outcomes) - ocr_succeeded
    logger.info(
        "event=stage_complete stage=ocr run_id=%s path=%s pages_total=%d pages_succeeded=%d "
        "pages_failed=%d duration_ms=%d",
        run_id, normalized_path, len(ocr_outcomes), ocr_succeeded, ocr_failed,
        (time.perf_counter() - stage_start) * 1000,
    )

    if structure_engine is not None and file_type in ("pdf", "jpg", "jpeg", "png"):
        stage_start = time.perf_counter()
        analyze_document_structure(
            conn, extracted_doc_id, path, file_type,
            engine=structure_engine, analyze_native_pages=structure_analyze_native,
        )
        logger.info(
            "event=stage_complete stage=structure run_id=%s path=%s duration_ms=%d",
            run_id, normalized_path, (time.perf_counter() - stage_start) * 1000,
        )

    stage_start = time.perf_counter()
    canonical_document = build_canonical_document(conn, file_id, extracted_doc_id)
    mode_counts = {"native": 0, "ocr": 0, "merged": 0, "failed": 0, "skipped": 0}
    total_chars = 0
    for page in canonical_document.pages:
        mode_counts[page.mode] = mode_counts.get(page.mode, 0) + 1
        total_chars += len(page.text)
    logger.info(
        "event=stage_complete stage=canonicalize run_id=%s path=%s pages_total=%d "
        "pages_native=%d pages_ocr=%d pages_merged=%d pages_failed=%d pages_skipped=%d "
        "total_chars=%d duration_ms=%d",
        run_id, normalized_path, len(canonical_document.pages),
        mode_counts["native"], mode_counts["ocr"], mode_counts["merged"],
        mode_counts["failed"], mode_counts["skipped"], total_chars,
        (time.perf_counter() - stage_start) * 1000,
    )

    stage_start = time.perf_counter()
    quality_assessment = assess_and_persist_document(
        conn, file_id=file_id, extracted_doc_id=extracted_doc_id, source_relative_path=relative_path,
    )
    logger.info(
        "event=stage_complete stage=quality_review run_id=%s path=%s decision=%s "
        "pages_flagged=%d duration_ms=%d",
        run_id, normalized_path, quality_assessment.decision,
        len(quality_assessment.flagged_page_numbers), (time.perf_counter() - stage_start) * 1000,
    )

    stage_start = time.perf_counter()
    chunks, _ = get_or_create_chunks(
        conn, file_id, extracted_doc_id, chunk_config, canonical_document=canonical_document
    )
    logger.info(
        "event=stage_complete stage=chunk run_id=%s path=%s chunk_count=%d duration_ms=%d",
        run_id, normalized_path, len(chunks), (time.perf_counter() - stage_start) * 1000,
    )

    stage_start = time.perf_counter()
    embed_chunks(conn, chunks, embed_provider, embed_config, batch_size=embed_batch_size)
    logger.info(
        "event=stage_complete stage=embed run_id=%s path=%s chunk_count=%d duration_ms=%d",
        run_id, normalized_path, len(chunks), (time.perf_counter() - stage_start) * 1000,
    )

    # G12 finding: this used to unconditionally mark 'indexed' regardless
    # of ocr_needs_ocr_pages()'s outcomes (its return value was discarded
    # entirely) or how many chunks resulted — a file whose only page
    # failed OCR (e.g. the OCR engine couldn't even load) still ended up
    # plain "indexed" with zero chunks, identical in status to a genuine
    # success. status stays 'indexed' (preserves the skip-unchanged fast
    # path above and every existing database's CHECK constraint — no
    # schema-breaking status value), but ocr_warning now records *why*
    # this file may not actually be searchable, for the GUI/CLI to
    # surface instead of an unqualified "Indexed".
    ocr_warning = None
    failed_pages = [o for o in ocr_outcomes if not o.success]
    if failed_pages and not chunks:
        reasons = sorted({o.error_message or "unknown OCR error" for o in failed_pages})
        ocr_warning = f"OCR failed on {len(failed_pages)} page(s): {'; '.join(reasons)}"
    elif failed_pages:
        ocr_warning = (
            f"OCR failed on {len(failed_pages)} of {len(ocr_outcomes)} page(s); "
            "some text may be missing."
        )
    elif not chunks:
        ocr_warning = "No searchable text was extracted from this file."

    conn.execute(
        "UPDATE files SET status = 'indexed', ocr_warning = ?, last_indexed_at = ? WHERE id = ?",
        (ocr_warning, _now(), file_id),
    )
    conn.commit()
    return "indexed"


def _reown_shared_extracted_docs(conn: sqlite3.Connection, stale_file_ids: list[int]) -> None:
    """Content-addressed extracted_docs rows are shared across files with
    identical content but owned (FK file_id, ON DELETE CASCADE) by whichever
    file indexed first. Before deleting stale files, re-own any of their
    extracted_docs rows — and the chunks rows physically tied to them — to
    a surviving file with the same content_hash. Otherwise the cascade
    would destroy the survivor's pages/chunks/FTS.

    DEVIATION FROM PLAN (recorded in VECTORDRIVE_IMPLEMENTATION_PLAN.md
    §4/Stage 2): the plan's original SQL located a "surviving owner" via
    an existing chunks row with a non-stale file_id. That never exists in
    practice — get_or_create_chunks() only ever writes chunks rows for
    whichever file first computed them (a cache hit for a second file
    with identical content returns the cached rows without inserting its
    own); a duplicate/moved file's chunks rows always carry the *first*
    file's id. The join is done via files.content_hash instead (the same
    value extracted_docs.content_hash already stores). And since
    `chunks.file_id` is its own independent `REFERENCES files(id) ON
    DELETE CASCADE` column — not merely inherited from extracted_docs —
    reassigning extracted_docs.file_id alone left chunks.file_id still
    pointing at the stale file, so the direct chunks->files cascade still
    fired. chunks.file_id must be re-pointed too.
    """
    if not stale_file_ids:
        return
    placeholders = ",".join("?" for _ in stale_file_ids)
    conn.execute(
        f"""
        UPDATE extracted_docs SET file_id = (
            SELECT files.id FROM files
            WHERE files.content_hash = extracted_docs.content_hash
              AND files.id NOT IN ({placeholders})
            LIMIT 1)
        WHERE file_id IN ({placeholders})
          AND EXISTS (SELECT 1 FROM files
                      WHERE files.content_hash = extracted_docs.content_hash
                        AND files.id NOT IN ({placeholders}))
        """,
        [*stale_file_ids, *stale_file_ids, *stale_file_ids],
    )
    conn.execute(
        f"""
        UPDATE chunks SET file_id = (
            SELECT extracted_docs.file_id FROM extracted_docs
            WHERE extracted_docs.id = chunks.extracted_doc_id)
        WHERE file_id IN ({placeholders})
          AND extracted_doc_id IN (
              SELECT id FROM extracted_docs WHERE file_id NOT IN ({placeholders}))
        """,
        [*stale_file_ids, *stale_file_ids],
    )


def _reconcile_deleted_files(
    conn: sqlite3.Connection, root: Path, discovered_paths: set[str], indexed_folders: list[str]
) -> int:
    """Remove files from the index that no longer exist on disk.

    After indexing a folder, this identifies files in the database under
    `root` that were NOT discovered during the current walk (deleted since
    the last index run) and removes them — but only if they are exclusively
    under `root` and not also covered by another folder in `indexed_folders`.

    Deletion cascades (via FK constraints) to extracted_docs, pages, chunks,
    chunk_runs, and fts_chunks (via AFTER DELETE trigger). The vector index
    (vec_chunks) is rebuilt separately afterward by build_vector_index().

    Returns the count of deleted file records.
    """
    root = Path(root)
    if not root.exists():
        return 0  # never reconcile against an inaccessible root (BUG A defense in depth)
    root_str = str(root.resolve())

    # Find all files in the DB under this root
    # Use LIKE with proper escaping to match both the root itself and its descendants
    def _like_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    pattern = _like_escape(root_str) + "/%"
    db_rows = conn.execute(
        "SELECT id, path FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'", (root_str, pattern)
    ).fetchall()

    # Identify stale files: in DB but not discovered during this walk
    stale_ids = [row["id"] for row in db_rows if row["path"] not in discovered_paths]

    if not stale_ids:
        return 0

    # Overlap check: only delete files that are NOT covered by any OTHER indexed folder
    # (same logic as folders_service.remove_folder but adapted for individual file reconciliation).
    # Resolve paths before comparing to handle symlinks (e.g., macOS /var → /private/var).
    remaining_folders = [f for f in indexed_folders if str(Path(f).resolve()) != root_str]

    exclusive_ids = [
        file_id
        for file_id in stale_ids
        for row in db_rows
        if row["id"] == file_id
        and not any(path_is_inside(Path(row["path"]), Path(other)) for other in remaining_folders)
    ]

    if not exclusive_ids:
        return 0

    # Delete stale files (cascades to all related tables)
    placeholders = ",".join("?" for _ in exclusive_ids)
    try:
        _reown_shared_extracted_docs(conn, exclusive_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", exclusive_ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return len(exclusive_ids)


def index_folder(
    conn: sqlite3.Connection,
    root: Path,
    *,
    verify: bool = False,
    tier2_engine: OcrEngine,
    tier3_engine: OcrEngine | None = None,
    tier4_engine: OcrEngine | None = None,
    ocr_lang: str = "en",
    ocr_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    chunk_config: ChunkerConfig,
    embed_provider: EmbeddingProvider,
    embed_config: EmbeddingConfig,
    embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    vector_index: VectorIndex,
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    indexed_folders: list[str] | None = None,
    root_id: int | None = None,
    structure_engine=None,
    structure_analyze_native: bool = True,
    excluded_paths: set[str] | None = None,
) -> IndexRunSummary:
    """Run one indexing pass over every supported file under `root`.

    v0.2 G2 additions (both optional, default to v0.1's exact behavior):

    `on_event(event)` is called with a small dict at each notable moment:
      - {"kind": "run_started", "run_id": int, "total": int}
      - {"kind": "file_started", "path": str, "index": int, "total": int}
      - {"kind": "file_done", "path": str, "index": int, "total": int,
         "outcome": "indexed"|"skipped"|"unsupported"|"failed", "error": str|None}
      - {"kind": "run_finished" | "run_cancelled", "summary": {...}}
    The GUI's job runner (G6) wraps these into its own sequenced event
    envelope; index_folder itself knows nothing about jobs, threads, or
    the GUI.

    `should_cancel()` is polled once per file, between files only — never
    mid-file, so a half-processed file is never committed as "indexed".
    On cancellation the run still finalizes its index_runs row (counts
    as-of-cancellation, finished_at set) so the Overview screen never
    shows a permanently "in progress" run; IndexRunSummary.cancelled is
    True and the file loop simply stops early.

    `indexed_folders` enables deletion reconciliation: files in the DB under
    `root` that are no longer on disk are removed from the index (with
    overlap awareness — a file covered by multiple folders is only deleted
    when no remaining folder covers it). When None (the default, preserving
    backward compatibility), no reconciliation occurs.

    `root_id` (Stage 3) is the roots.id this run belongs to — when provided,
    every files row this run touches also gets root_id + relative_path
    (path relative to `root`) set, which is what lets a later Relink
    (Stage 4) re-index by relative-path comparison instead of treating
    every file as newly discovered. None (the default) preserves pre-
    Stage-3 behavior exactly: no root attribution, relative_path stays
    NULL. Bare pipeline callers (direct index_folder() with no root
    registration, e.g. integration tests) simply omit it.

    `structure_engine` (Stage 9) is None by default — no structure
    analysis runs, byte-identical to pre-Stage-9 behavior, and zero
    structure_results rows are written. Passing a PPStructureEngine (or
    fake, in tests) runs it per file for pdf/jpg/jpeg/png files, after
    OCR resolves page text and before chunking, so a structure-aware
    chunker (Stage 10) can use it. `structure_analyze_native` gates
    whether pages whose text came from native extraction (not OCR) are
    also analyzed — irrelevant when structure_engine is None.

    `excluded_paths` (Review panel "Remove from VectorDrive") is a set of
    normalized absolute path strings dropped from discovery before any
    work; the file is not re-indexed until it is un-excluded in
    config.toml. None (default) means no exclusions — unchanged behavior.
    """
    root = Path(root)
    run_start = time.perf_counter()
    started_at = _now()
    cur = conn.execute("INSERT INTO index_runs (started_at) VALUES (?)", (started_at,))
    run_id = cur.lastrowid
    conn.commit()

    # M-indexing-fix: a directory os.walk can't list (permission denied, a
    # transient cloud-sync/mount error) used to vanish silently -- no
    # exception, no count, nothing downstream could ever tell. Every such
    # directory (and every individual file whose stat() fails) is recorded
    # here instead, merged into the same errors_json this run already
    # writes for per-file failures below.
    walk_errors: list[dict[str, str]] = []

    def _on_walk_error(path: Path, exc: OSError) -> None:
        message = f"{type(exc).__name__}: {exc}"
        walk_errors.append({"path": str(path), "error": message, "kind": "walk_error"})
        logger.warning("indexing: could not scan %s: %s", path, message)

    # Materialized once so on_event can report a total from the first event.
    # The walk itself is cancellation-aware, so closing during discovery no
    # longer waits for a large tree to be completely enumerated and sorted.
    discover_start = time.perf_counter()
    files = list(_iter_files(root, should_cancel=should_cancel, on_error=_on_walk_error))
    if excluded_paths:
        # Review panel "Remove from VectorDrive": a file the user excluded
        # is dropped before any work — direct OCR->index never re-adds it.
        before = len(files)
        files = [p for p in files if _normalize_path(p) not in excluded_paths]
        if len(files) != before:
            logger.info(
                "event=excluded_files_skipped run_id=%s count=%d", run_id, before - len(files)
            )
    total = len(files)
    logger.info(
        "event=stage_complete stage=discover run_id=%s total=%d duration_ms=%d",
        run_id, total, (time.perf_counter() - discover_start) * 1000,
    )
    logger.info("event=run_start run_id=%s root=%s total=%d", run_id, root, total)
    if on_event is not None:
        on_event({"kind": "run_started", "run_id": run_id, "total": total})

    counts = {"indexed": 0, "skipped": 0, "unsupported": 0, "failed": 0}
    errors: list[dict[str, str]] = []
    cancelled = should_cancel is not None and should_cancel()

    for index, path in enumerate(files):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

        file_type = _classify(path)
        file_start = time.perf_counter()
        logger.info(
            "event=file_start run_id=%s path=%s file_type=%s", run_id, path, file_type or "unsupported"
        )
        if on_event is not None:
            on_event({"kind": "file_started", "path": str(path), "index": index, "total": total})

        if file_type is None:
            _record_unsupported(conn, path, root=root, root_id=root_id)
            counts["unsupported"] += 1
            logger.info(
                "event=file_done run_id=%s path=%s outcome=unsupported duration_ms=%d",
                run_id, path, (time.perf_counter() - file_start) * 1000,
            )
            if on_event is not None:
                on_event({
                    "kind": "file_done", "path": str(path), "index": index, "total": total,
                    "outcome": "unsupported", "error": None,
                })
            continue

        try:
            outcome = _process_one_file(
                conn, path, file_type,
                run_id=run_id,
                verify=verify,
                tier2_engine=tier2_engine, tier3_engine=tier3_engine, tier4_engine=tier4_engine,
                ocr_lang=ocr_lang, ocr_confidence_threshold=ocr_confidence_threshold,
                chunk_config=chunk_config, embed_provider=embed_provider, embed_config=embed_config,
                embed_batch_size=embed_batch_size,
                root=root, root_id=root_id,
                structure_engine=structure_engine, structure_analyze_native=structure_analyze_native,
            )
        except Exception as exc:  # noqa: BLE001 — deliberate: one bad file must never stop the run
            message = f"{type(exc).__name__}: {exc}"
            _record_failure(conn, path, file_type, message, root=root, root_id=root_id)
            counts["failed"] += 1
            errors.append({"path": str(path), "error": message})
            logger.error(
                "event=file_done run_id=%s path=%s outcome=failed duration_ms=%d error=%s",
                run_id, path, (time.perf_counter() - file_start) * 1000, message,
                exc_info=True,
            )
            if on_event is not None:
                on_event({
                    "kind": "file_done", "path": str(path), "index": index, "total": total,
                    "outcome": "failed", "error": message,
                })
            continue

        counts[outcome] += 1
        logger.info(
            "event=file_done run_id=%s path=%s outcome=%s duration_ms=%d",
            run_id, path, outcome, (time.perf_counter() - file_start) * 1000,
        )
        if on_event is not None:
            on_event({
                "kind": "file_done", "path": str(path), "index": index, "total": total,
                "outcome": outcome, "error": None,
            })

    # Deletion reconciliation: remove files from the index that no longer
    # exist on disk (deleted since the last run). Only runs when
    # indexed_folders is provided (typically from the GUI or service layer);
    # the CLI's direct index_folder() call omits it, preserving v0.1 behavior.
    if indexed_folders is not None:
        discovered_paths = {_normalize_path(path) for path in files}
        _reconcile_deleted_files(conn, root, discovered_paths, indexed_folders)

    # One vector-index rebuild for the whole run: reads only chunks+embeddings
    # (never re-extracts/re-OCRs/re-chunks/re-embeds) and removes any
    # chunk_ids no longer present — this is what makes stale vector entries
    # go away after a file changes, in addition to the FTS/pages/chunks
    # cleanup that already happened per-file above. Still runs on a
    # cancelled run, so whatever files WERE processed are searchable.
    config_hash = embed_config.config_hash()
    # A cancellation during discovery has not changed any index data, so
    # rebuilding a potentially large vector index would only delay shutdown.
    # Once at least one file was handled, retain the existing partial-run
    # behavior and make that work searchable before finalizing the run.
    if not cancelled or any(counts.values()):
        build_vector_index(
            conn, vector_index, embed_provider.NAME, embed_config.model, embed_config.dimensions, config_hash
        )

    # M-indexing-fix: enforce files = indexed + skipped + unsupported +
    # failed for every file _iter_files actually yielded. This holds by
    # construction today (every path in `files` goes through exactly one
    # counted branch above) -- this check exists so a future change that
    # adds a new silent `continue` doesn't reopen that gap without anyone
    # noticing. walk_errors is deliberately NOT part of this equation: it
    # counts directories that couldn't be listed at all, so we have no way
    # to know how many files were inside them -- see IndexRunSummary.
    accounted = counts["indexed"] + counts["skipped"] + counts["unsupported"] + counts["failed"]
    if accounted != total and not cancelled:
        logger.error(
            "indexing invariant violated for %s: %d file(s) discovered but only %d accounted "
            "for (indexed=%d skipped=%d unsupported=%d failed=%d) -- %d file(s) vanished from "
            "the count",
            root, total, accounted, counts["indexed"], counts["skipped"], counts["unsupported"],
            counts["failed"], total - accounted,
        )
    if walk_errors:
        logger.warning(
            "indexing: %d director%s under %s could not be scanned and are NOT reflected in "
            "indexed/skipped/unsupported/failed above -- files inside may be missing from the "
            "index entirely",
            len(walk_errors), "y" if len(walk_errors) == 1 else "ies", root,
        )
    errors.extend(walk_errors)

    finished_at = _now()
    conn.execute(
        "UPDATE index_runs SET finished_at = ?, files_indexed = ?, files_skipped = ?, "
        "files_unsupported = ?, files_failed = ?, errors_json = ? WHERE id = ?",
        (
            finished_at,
            counts["indexed"],
            counts["skipped"],
            counts["unsupported"],
            counts["failed"],
            json.dumps(errors),
            run_id,
        ),
    )
    conn.commit()

    summary = IndexRunSummary(run_id=run_id, cancelled=cancelled, walk_errors=len(walk_errors), **counts)
    logger.info(
        "event=run_complete run_id=%s root=%s cancelled=%s total=%d indexed=%d skipped=%d "
        "unsupported=%d failed=%d walk_errors=%d duration_ms=%d",
        run_id, root, cancelled, total, counts["indexed"], counts["skipped"], counts["unsupported"],
        counts["failed"], len(walk_errors), (time.perf_counter() - run_start) * 1000,
    )
    if on_event is not None:
        on_event({"kind": "run_cancelled" if cancelled else "run_finished", "summary": summary.__dict__})

    return summary
