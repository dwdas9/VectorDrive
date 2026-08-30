"""Read-only Phase 2 preparation from adjacent VectorDrive Markdown.

This module is the boundary between a durable ``*.vectordrive.md``
artifact and indexing.  It deliberately has no dependency on the source
extractor, OCR, or sidecar-persistence paths: Phase 2 validates and reads
an existing artifact, then sends its canonical page payload through the
production Markdown-aware chunker.

Neither the source nor its sidecar is ever written by this module.  A
``final`` lifecycle locks regeneration, not indexing, so final sidecars
follow the same read-only indexing route as generated sidecars.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from vectordrive.pipeline.chunk.base import Chunk, ChunkerConfig, PageForChunking
from vectordrive.pipeline.chunk.markdown_chunker import (
    chunk_canonical_markdown_document,
    derive_document_title,
)
from vectordrive.pipeline.embed.context import folder_from_relative_path
from vectordrive.pipeline.embed.base import (
    DEFAULT_EMBED_BATCH_SIZE,
    EmbeddingConfig,
    EmbeddingProvider,
)
from vectordrive.pipeline.embed.embed_chunks import embed_chunks
from vectordrive.pipeline.markdown_artifact import (
    CanonicalDocument,
    MarkdownArtifactError,
    is_vectordrive_markdown_name,
    parse_markdown,
    searchable_text,
    sidecar_path_for,
)
from vectordrive.search.index_builder import VectorIndexBuildPaused, build_vector_index
from vectordrive.search.vector_index.base import VectorIndex, compute_model_key
from vectordrive.services.processing_run_service import (
    ProcessingItemSeed,
    claim_next_item,
    complete_item,
    complete_run,
    create_run,
    fail_item,
    fail_run,
    pause_run,
    resume_run,
)

logger = logging.getLogger(__name__)


class SidecarIndexError(RuntimeError):
    """A sidecar cannot safely be used as the Phase 2 source of truth."""


@dataclass(frozen=True)
class PreparedSidecarIndex:
    """Validated canonical document and its production indexing chunks."""

    source_path: Path
    sidecar_path: Path
    document: CanonicalDocument
    chunks: tuple[Chunk, ...]
    source_sha256: str
    sidecar_sha256: str
    body_sha256: str


@dataclass(frozen=True)
class SidecarIndexRunSummary:
    """Outcome of a new or resumed durable Phase 2 run."""

    run_id: int
    indexed: int
    skipped: int
    failed: int
    cancelled: bool
    model_key: str


EventCallback = Callable[[dict], None]
CancelCallback = Callable[[], bool]


_TEXT_SOURCE_BY_MODE: dict[str, str] = {
    "native": "native",
    "ocr": "ocr",
    "merged": "ocr",
    "failed": "none",
    "skipped": "none",
}


def prepare_sidecar_for_indexing(
    source_path: Path,
    *,
    corpus_root: Path,
    chunker_config: ChunkerConfig | None = None,
) -> PreparedSidecarIndex:
    """Validate and chunk one source's adjacent Markdown sidecar.

    Validation is fail-closed: the original source must be a regular file
    inside ``corpus_root``; the exact adjacent sidecar must be valid UTF-8;
    its schema, lifecycle and body digest must parse; and its filename,
    root-relative path, byte size, and SHA-256 must bind to the current
    source.  No fallback to extraction or OCR is attempted on any failure.
    """
    source_path = Path(source_path)
    corpus_root = Path(corpus_root).resolve()

    if is_vectordrive_markdown_name(source_path):
        raise SidecarIndexError("Phase 2 input must be the original source, not a sidecar")

    try:
        resolved_source = source_path.resolve(strict=True)
    except OSError as exc:
        raise SidecarIndexError(f"Source file is unavailable: {source_path.name}") from exc
    if not resolved_source.is_file():
        raise SidecarIndexError(f"Source is not a regular file: {resolved_source.name}")

    try:
        relative_path = resolved_source.relative_to(corpus_root).as_posix()
    except ValueError as exc:
        raise SidecarIndexError("Source file is outside the declared corpus root") from exc

    sidecar_path = sidecar_path_for(resolved_source)
    if sidecar_path.is_symlink():
        raise SidecarIndexError(f"Adjacent sidecar must not be a symbolic link: {sidecar_path.name}")
    if not sidecar_path.is_file():
        raise SidecarIndexError(f"Adjacent sidecar does not exist: {sidecar_path.name}")
    try:
        resolved_sidecar = sidecar_path.resolve(strict=True)
        resolved_sidecar.relative_to(corpus_root)
    except (OSError, ValueError) as exc:
        raise SidecarIndexError("Adjacent sidecar is outside the declared corpus root") from exc
    if resolved_sidecar.parent != resolved_source.parent or resolved_sidecar != sidecar_path.absolute():
        raise SidecarIndexError("Sidecar is not the exact adjacent artifact for its source")

    try:
        sidecar_bytes = sidecar_path.read_bytes()
        sidecar_text = sidecar_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SidecarIndexError(f"Adjacent sidecar is unreadable: {sidecar_path.name}") from exc

    try:
        document = parse_markdown(sidecar_text)
    except MarkdownArtifactError as exc:
        raise SidecarIndexError(
            f"Adjacent sidecar failed integrity validation: {sidecar_path.name}: {exc}"
        ) from exc

    if document.source.filename != resolved_source.name:
        raise SidecarIndexError(
            "Sidecar source filename mismatch: "
            f"declared {document.source.filename!r}, actual {resolved_source.name!r}"
        )
    if document.source.relative_path != relative_path:
        raise SidecarIndexError(
            "Sidecar source relative-path mismatch: "
            f"declared {document.source.relative_path!r}, actual {relative_path!r}"
        )

    try:
        source_size = resolved_source.stat().st_size
        source_sha256 = _hash_file_sha256(resolved_source)
    except OSError as exc:
        raise SidecarIndexError(f"Source file became unreadable: {resolved_source.name}") from exc
    if document.source.byte_size != source_size:
        raise SidecarIndexError(
            "Sidecar source size mismatch: "
            f"declared {document.source.byte_size}, actual {source_size}"
        )
    if document.source.sha256 != source_sha256:
        raise SidecarIndexError(
            "Sidecar source hash mismatch: the source changed after Markdown generation"
        )

    failed_pages = [page.page_number for page in document.pages if page.mode == "failed"]
    if failed_pages:
        raise SidecarIndexError(
            "Sidecar records failed OCR on page(s) "
            f"{', '.join(str(n) for n in failed_pages)}: Phase 1 produced a diagnostic "
            "artifact with no usable text; re-run Markdown generation before indexing"
        )

    pages = [
        PageForChunking(
            page_number=page.page_number,
            text=page.text if page.mode not in ("failed", "skipped") else "",
            text_source=_TEXT_SOURCE_BY_MODE[page.mode],
        )
        for page in document.pages
    ]
    title = derive_document_title([page.text for page in pages])
    chunks = chunk_canonical_markdown_document(
        pages,
        chunker_config or ChunkerConfig(),
        filename=document.source.filename,
        folder=folder_from_relative_path(document.source.relative_path),
        media_type=document.source.media_type,
        title=title,
    )

    sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
    body_sha256 = hashlib.sha256(searchable_text(document).encode("utf-8")).hexdigest()
    logger.info(
        "event=sidecar_prepared_for_index source_path=%s sidecar_path=%s "
        "content_status=%s index_policy=%s pages=%d chunks=%d source_sha256=%s "
        "sidecar_sha256=%s body_sha256=%s",
        resolved_source,
        sidecar_path,
        document.lifecycle.content_status,
        document.lifecycle.index_policy,
        len(document.pages),
        len(chunks),
        source_sha256,
        sidecar_sha256,
        body_sha256,
    )
    return PreparedSidecarIndex(
        source_path=resolved_source,
        sidecar_path=resolved_sidecar,
        document=document,
        chunks=tuple(chunks),
        source_sha256=source_sha256,
        sidecar_sha256=sidecar_sha256,
        body_sha256=body_sha256,
    )


def _hash_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_sidecar_index(
    conn: sqlite3.Connection,
    root: Path,
    *,
    embed_provider: EmbeddingProvider,
    embed_config: EmbeddingConfig,
    vector_index: VectorIndex,
    chunker_config: ChunkerConfig | None = None,
    embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    root_id: int | None = None,
    run_id: int | None = None,
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    excluded_paths: set[str] | None = None,
) -> SidecarIndexRunSummary:
    """Index adjacent validated sidecars with durable file-level checkpoints.

    This is the Phase 2 service entry point.  Discovery starts from
    ``*.vectordrive.md`` artifacts, derives their adjacent source identity,
    and never calls the legacy source extraction/OCR pipeline.  Passing a
    paused ``run_id`` resumes it when the plan still matches.  A changed
    sidecar, source binding, chunk configuration, or embedding configuration
    creates a successor plan: matching completed items remain completed and
    only stale items are queued again.

    Pause/cancel is observed only between documents.  Each document's DB,
    embedding-cache and checkpoint state is complete before the next pause
    boundary.  The vector index is rebuilt idempotently after all items are
    complete; cancelling before that leaves a resumable paused run rather
    than a falsely completed index.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise SidecarIndexError(f"Corpus root is not a folder: {root}")
    chunker_config = chunker_config or ChunkerConfig()
    config_fingerprint = _index_config_fingerprint(
        chunker_config, embed_provider.NAME, embed_config, embed_batch_size
    )
    seeds = _discover_sidecar_seeds(root, config_fingerprint)
    if excluded_paths:
        # Review panel "Remove from VectorDrive": skip a sidecar whose
        # source the user excluded, even if the sidecar file is still on
        # disk (its own removal is best-effort).
        seeds = [
            seed for seed in seeds
            if str((root / seed.item_key).resolve()) not in excluded_paths
        ]
    if not seeds:
        raise SidecarIndexError("No adjacent *.vectordrive.md artifacts were found")
    corpus_fingerprint = _corpus_fingerprint(seeds)
    active_run_id, inherited = _select_or_create_run(
        conn,
        root=root,
        seeds=seeds,
        corpus_fingerprint=corpus_fingerprint,
        config_fingerprint=config_fingerprint,
        requested_run_id=run_id,
    )
    resume_run(conn, active_run_id)
    indexed = 0
    failed = 0
    completed = inherited
    if on_event:
        on_event({"kind": "run_started", "run_id": active_run_id,
                  "completed": completed, "total": len(seeds)})

    while True:
        if should_cancel is not None and should_cancel():
            pause_run(conn, active_run_id)
            if on_event:
                on_event({"kind": "run_paused", "run_id": active_run_id,
                          "completed": completed, "total": len(seeds)})
            return SidecarIndexRunSummary(
                run_id=active_run_id,
                indexed=indexed,
                skipped=inherited,
                failed=failed,
                cancelled=True,
                model_key=_model_key(embed_provider, embed_config),
            )
        claimed = claim_next_item(conn, active_run_id)
        if claimed is None:
            break
        source_path = root / claimed.item_key
        if on_event:
            on_event({"kind": "file_started", "run_id": active_run_id,
                      "path": str(source_path), "completed": completed, "total": len(seeds)})
        try:
            prepared = prepare_sidecar_for_indexing(
                source_path, corpus_root=root, chunker_config=chunker_config
            )
            current = _seed_for_source(root, source_path, config_fingerprint)
            if (
                current.content_fingerprint != claimed.content_fingerprint
                or claimed.config_fingerprint != config_fingerprint
            ):
                raise SidecarIndexError(
                    "Sidecar/source/config changed after this run was planned; start a fresh resume"
                )
            _persist_prepared_sidecar(
                conn,
                prepared,
                root=root,
                root_id=root_id,
                chunker_config=chunker_config,
            )
            outcomes = embed_chunks(
                conn,
                list(prepared.chunks),
                embed_provider,
                embed_config,
                batch_size=embed_batch_size,
            )
            errors = [outcome.error_message or "embedding failed" for outcome in outcomes if not outcome.success]
            if errors:
                raise SidecarIndexError("; ".join(sorted(set(errors))))
            _mark_file_indexed(conn, prepared.source_path)
            complete_item(conn, active_run_id, claimed.id)
            indexed += 1
            completed += 1
            if on_event:
                on_event({"kind": "file_done", "run_id": active_run_id,
                          "path": str(source_path), "outcome": "indexed",
                          "completed": completed, "total": len(seeds)})
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            _mark_file_failed(conn, source_path, message, root=root, root_id=root_id)
            fail_item(conn, active_run_id, claimed.id, message)
            failed += 1
            completed += 1
            if on_event:
                on_event({"kind": "file_done", "run_id": active_run_id,
                          "path": str(source_path), "outcome": "failed", "error": message,
                          "completed": completed, "total": len(seeds)})

    model_key = _model_key(embed_provider, embed_config)
    if failed:
        fail_run(conn, active_run_id, f"{failed} sidecar(s) failed Phase 2 indexing")
    else:
        try:
            build_vector_index(
                conn,
                vector_index,
                embed_provider.NAME,
                embed_config.model,
                embed_config.dimensions,
                embed_config.config_hash(),
                should_cancel=should_cancel,
            )
        except VectorIndexBuildPaused:
            pause_run(conn, active_run_id)
            if on_event:
                on_event({"kind": "run_paused", "run_id": active_run_id,
                          "stage": "vector_finalize", "completed": completed,
                          "total": len(seeds)})
            return SidecarIndexRunSummary(
                run_id=active_run_id, indexed=indexed, skipped=inherited,
                failed=0, cancelled=True, model_key=model_key,
            )
        complete_run(conn, active_run_id)
    if on_event:
        on_event({"kind": "run_finished", "run_id": active_run_id,
                  "indexed": indexed, "skipped": inherited, "failed": failed,
                  "completed": completed, "total": len(seeds)})
    return SidecarIndexRunSummary(
        run_id=active_run_id,
        indexed=indexed,
        skipped=inherited,
        failed=failed,
        cancelled=False,
        model_key=model_key,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _model_key(provider: EmbeddingProvider, config: EmbeddingConfig) -> str:
    return compute_model_key(
        provider.NAME, config.model, config.dimensions, config.config_hash()
    )


def _index_config_fingerprint(
    chunker: ChunkerConfig,
    provider_name: str,
    embed_config: EmbeddingConfig,
    batch_size: int,
) -> str:
    payload = json.dumps(
        {
            "chunker": chunker.config_hash(),
            "embedding_provider": provider_name,
            "embedding": embed_config.config_hash(),
            "batch_size": max(1, batch_size),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _discover_sidecar_seeds(root: Path, config_fingerprint: str) -> list[ProcessingItemSeed]:
    suffix = ".vectordrive.md"
    seeds: list[ProcessingItemSeed] = []
    for sidecar in sorted(root.rglob(f"*{suffix}")):
        if sidecar.is_symlink():
            raise SidecarIndexError(f"Adjacent sidecar must not be a symbolic link: {sidecar.name}")
        if not sidecar.is_file():
            continue
        source = Path(str(sidecar)[: -len(suffix)])
        seeds.append(_seed_for_source(root, source, config_fingerprint))
    return seeds


def _seed_for_source(
    root: Path, source: Path, config_fingerprint: str
) -> ProcessingItemSeed:
    try:
        item_key = source.resolve(strict=False).relative_to(root).as_posix()
        source_hash = _hash_file_sha256(source)
        sidecar_hash = _hash_file_sha256(sidecar_path_for(source))
    except (OSError, ValueError) as exc:
        raise SidecarIndexError(f"Unable to fingerprint sidecar pair: {source.name}") from exc
    content = hashlib.sha256(f"{source_hash}:{sidecar_hash}".encode("ascii")).hexdigest()
    return ProcessingItemSeed(item_key, content, config_fingerprint)


def _corpus_fingerprint(seeds: list[ProcessingItemSeed]) -> str:
    payload = "\n".join(f"{s.item_key}:{s.content_fingerprint}" for s in seeds)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_or_create_run(
    conn: sqlite3.Connection,
    *,
    root: Path,
    seeds: list[ProcessingItemSeed],
    corpus_fingerprint: str,
    config_fingerprint: str,
    requested_run_id: int | None,
) -> tuple[int, int]:
    previous_items: dict[str, sqlite3.Row] = {}
    previous_config: str | None = None
    if requested_run_id is not None:
        run = conn.execute(
            "SELECT * FROM processing_runs WHERE id = ? AND phase = 'index'",
            (requested_run_id,),
        ).fetchone()
        if run is None:
            raise SidecarIndexError(f"Index run {requested_run_id} does not exist")
        if Path(run["root_path"]).resolve() != root:
            raise SidecarIndexError("Requested resume run belongs to a different corpus root")
        previous_config = run["config_fingerprint"]
        previous_items = {
            row["item_key"]: row
            for row in conn.execute(
                "SELECT * FROM processing_run_items WHERE run_id = ?", (requested_run_id,)
            ).fetchall()
        }
        exact = (
            run["status"] in ("queued", "paused")
            and run["content_fingerprint"] == corpus_fingerprint
            and previous_config == config_fingerprint
            and len(previous_items) == len(seeds)
            and all(
                seed.item_key in previous_items
                and previous_items[seed.item_key]["content_fingerprint"] == seed.content_fingerprint
                and previous_items[seed.item_key]["config_fingerprint"] == seed.config_fingerprint
                for seed in seeds
            )
        )
        if exact:
            inherited = sum(row["status"] == "completed" for row in previous_items.values())
            return requested_run_id, inherited

    new_run_id = create_run(
        conn,
        phase="index",
        root_path=str(root),
        content_fingerprint=corpus_fingerprint,
        config_fingerprint=config_fingerprint,
        items=seeds,
    )
    inherited = 0
    if previous_config == config_fingerprint:
        now = _utc_now()
        with conn:
            for seed in seeds:
                old = previous_items.get(seed.item_key)
                if (
                    old is not None
                    and old["status"] == "completed"
                    and old["content_fingerprint"] == seed.content_fingerprint
                    and old["config_fingerprint"] == seed.config_fingerprint
                ):
                    conn.execute(
                        "UPDATE processing_run_items SET status = 'completed', "
                        "completed_at = ?, updated_at = ? WHERE run_id = ? AND item_key = ?",
                        (now, now, new_run_id, seed.item_key),
                    )
                    inherited += 1
    return new_run_id, inherited


def _persist_prepared_sidecar(
    conn: sqlite3.Connection,
    prepared: PreparedSidecarIndex,
    *,
    root: Path,
    root_id: int | None,
    chunker_config: ChunkerConfig,
) -> None:
    stat = prepared.source_path.stat()
    now = _utc_now()
    relative_path = prepared.source_path.relative_to(root).as_posix()
    file_type = prepared.source_path.suffix.lower().lstrip(".") or "unknown"
    with conn:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
            "first_indexed_at, last_indexed_at, root_id, relative_path, markdown_status, "
            "markdown_path, markdown_body_hash, markdown_updated_at, markdown_error) "
            "VALUES (?, ?, ?, ?, ?, ?, 'not_indexed', ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
            "mtime_ns=excluded.mtime_ns, content_hash=excluded.content_hash, "
            "file_type=excluded.file_type, status='not_indexed', error_message=NULL, "
            "root_id=excluded.root_id, relative_path=excluded.relative_path, "
            "markdown_status=excluded.markdown_status, markdown_path=excluded.markdown_path, "
            "markdown_body_hash=excluded.markdown_body_hash, "
            "markdown_updated_at=excluded.markdown_updated_at, markdown_error=NULL",
            (
                str(prepared.source_path), stat.st_size, stat.st_mtime, stat.st_mtime_ns,
                prepared.source_sha256, file_type, now, now, root_id, relative_path,
                ("final" if prepared.document.lifecycle.content_status == "final" else "ready"),
                str(prepared.sidecar_path),
                prepared.body_sha256, now,
            ),
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE path = ?", (str(prepared.source_path),)
        ).fetchone()[0]
        conn.execute("DELETE FROM extracted_docs WHERE file_id = ?", (file_id,))
        cur = conn.execute(
            "INSERT INTO extracted_docs (file_id, content_hash, extractor_name, "
            "extractor_version, page_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, prepared.sidecar_sha256, "vectordrive-markdown", "1",
             len(prepared.document.pages), now),
        )
        extracted_doc_id = int(cur.lastrowid)
        for page in prepared.document.pages:
            text_source = _TEXT_SOURCE_BY_MODE[page.mode]
            text = page.text if text_source != "none" else ""
            conn.execute(
                "INSERT INTO pages (extracted_doc_id, page_number, text, text_source, needs_ocr) "
                "VALUES (?, ?, ?, ?, 0)",
                (extracted_doc_id, page.page_number, text, text_source),
            )
        for chunk in prepared.chunks:
            conn.execute(
                "INSERT INTO chunks (file_id, extracted_doc_id, chunk_index, page_start, "
                "page_end, source_type, text, chunk_hash, created_at, section, block_type, "
                "bbox_json, reading_order, embedding_text, embedding_input_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id, extracted_doc_id, chunk.chunk_index, chunk.page_start,
                    chunk.page_end, chunk.source_type, chunk.text, chunk.chunk_hash, now,
                    chunk.section, chunk.block_type, chunk.bbox_json, chunk.reading_order,
                    chunk.embedding_text, chunk.embedding_input_hash,
                ),
            )
        source_content_hash = hashlib.sha256(
            "\n".join(chunk.chunk_hash for chunk in prepared.chunks).encode("ascii")
        ).hexdigest()
        conn.execute(
            "INSERT INTO chunk_runs (extracted_doc_id, source_content_hash, chunker_name, "
            "chunker_version, config_hash, chunk_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (extracted_doc_id, source_content_hash, "canonical-markdown", "1",
             chunker_config.config_hash(), len(prepared.chunks), now),
        )


def _mark_file_indexed(conn: sqlite3.Connection, source_path: Path) -> None:
    with conn:
        conn.execute(
            "UPDATE files SET status='indexed', error_message=NULL, ocr_warning=NULL, "
            "last_indexed_at=? WHERE path=?",
            (_utc_now(), str(source_path)),
        )


def _mark_file_failed(
    conn: sqlite3.Connection,
    source_path: Path,
    message: str,
    *,
    root: Path,
    root_id: int | None,
) -> None:
    """Record a failed Phase 2 source, creating a complete row if absent.

    A sidecar rejected before ``_persist_prepared_sidecar`` runs (for
    example a ``failed``-mode page on a fresh DB) has no ``files`` row yet,
    so a plain UPDATE would silently drop the failure.  This upserts a
    minimal, safe row bound to the exact source path with ``status='failed'``.

    Failure is also fail-closed for a previously indexed source: remove its
    old extracted document in the same transaction so cascading deletes clear
    pages, chunks, and FTS rows.  Otherwise a newly diagnostic/invalid sidecar
    could leave the prior document searchable even though the file now reports
    ``status='failed'``.  Content-addressed embedding-cache rows are intentionally
    retained; they are not file/chunk associations and may be shared.
    """
    resolved = source_path.resolve(strict=False)
    now = _utc_now()
    try:
        stat = resolved.stat()
        size, mtime, mtime_ns = stat.st_size, stat.st_mtime, stat.st_mtime_ns
    except OSError:
        size, mtime, mtime_ns = 0, 0.0, 0
    try:
        content_hash = _hash_file_sha256(resolved)
    except (OSError, ValueError):
        content_hash = ""
    file_type = resolved.suffix.lower().lstrip(".") or "unknown"
    try:
        relative_path = resolved.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        relative_path = None
    with conn:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, "
            "status, error_message, first_indexed_at, last_indexed_at, root_id, relative_path) "
            "VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET status='failed', error_message=excluded.error_message, "
            "last_indexed_at=excluded.last_indexed_at, root_id=excluded.root_id, "
            "relative_path=excluded.relative_path",
            (
                str(resolved), size, mtime, mtime_ns, content_hash, file_type,
                message, now, now, root_id, relative_path,
            ),
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE path = ?", (str(resolved),)
        ).fetchone()[0]
        conn.execute("DELETE FROM extracted_docs WHERE file_id = ?", (file_id,))
