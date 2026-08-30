"""Chunk-stage cache: wraps chunk_document() with a DB-backed cache keyed
by (source_content_hash, chunker_name, chunker_version, config_hash),
tracked per-document in chunk_runs.

Unlike the extraction/OCR caches (content-addressed, dedup across many
files), chunks are cached per-document: chunk_runs holds exactly one row
per extracted_doc_id, recording the key its currently-stored `chunks` rows
were computed under. A cache hit reads existing rows back unchanged; a
cache miss deletes and replaces them — chunks are never accumulated
alongside stale ones.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from vectordrive.pipeline.chunk.base import Chunk, ChunkerConfig, PageForChunking
from vectordrive.pipeline.chunk.chunker import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    chunk_document,
    chunk_page_text,
    compute_source_content_hash,
)
from vectordrive.pipeline.chunk.markdown_chunker import (
    CANONICAL_MARKDOWN_CHUNKER_NAME,
    CANONICAL_MARKDOWN_CHUNKER_VERSION,
    chunk_canonical_markdown_document,
    derive_document_title,
)
from vectordrive.pipeline.chunk.structure_chunker import (
    STRUCTURE_CHUNKER_NAME,
    STRUCTURE_CHUNKER_VERSION,
    chunk_page_blocks,
)
from vectordrive.pipeline.embed.context import CONTEXT_RECIPE_VERSION, folder_from_relative_path
from vectordrive.pipeline.markdown_artifact import CanonicalDocument, SourceProvenance

# Section 3: the canonical route (CanonicalDocument -> Markdown-aware,
# page-aware chunking over each page's already-resolved Markdown text)
# gets its own chunker identity so it can never falsely cache-hit a
# legacy/structure-aware run, OR the older flat "canonical-flat-page-
# aware" route this superseded — chunk_runs compares (source_content_hash,
# chunker_name, chunker_version, config_hash) together, and a name/version
# mismatch alone forces a miss. Old databases (which may hold rows under
# the retired "canonical-flat-page-aware" name) simply rechunk once under
# this name; no migration is needed or provided.
CANONICAL_CHUNKER_NAME = CANONICAL_MARKDOWN_CHUNKER_NAME
CANONICAL_CHUNKER_VERSION = CANONICAL_MARKDOWN_CHUNKER_VERSION

# Maps CanonicalDocument PageContent.mode -> chunks.source_type. failed/
# skipped pages carry no resolved text and are never chunked (decision 4).
_CANONICAL_MODE_TO_SOURCE_TYPE = {"native": "native", "ocr": "ocr", "merged": "merged"}


def get_pages_for_chunking(conn: sqlite3.Connection, extracted_doc_id: int) -> list[PageForChunking]:
    """Every page's resolved text for this document, in page order —
    native PDF text or cached OCR text, whichever `pages.text_source`
    already resolved to (M4/M5). Pages still text_source='none' are
    included here (chunk_document skips their empty text) so the source
    content hash still reflects "not yet resolved" honestly.

    Stage 10: also selects structure_result_id (NULL for legacy rows
    and documents structure analysis never touched) so get_or_create_
    chunks can look up each page's blocks via get_blocks_for_page().
    """
    rows = conn.execute(
        "SELECT page_number, text, text_source, structure_result_id FROM pages "
        "WHERE extracted_doc_id = ? ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()
    return [
        PageForChunking(
            page_number=row["page_number"],
            text=row["text"] or "",
            text_source=row["text_source"],
            structure_result_id=row["structure_result_id"],
        )
        for row in rows
    ]


def get_blocks_for_page(conn: sqlite3.Connection, structure_result_id: int | None) -> list[dict] | None:
    """Deserialize a page's structure blocks, only when its structure
    analysis succeeded. Returns None for structure_result_id=None (no
    structure attempted), a row that no longer exists, or success=0 (a
    cached failure) — all three mean "no usable structure for this page,
    fall back to flat chunking."
    """
    if structure_result_id is None:
        return None
    row = conn.execute(
        "SELECT success, blocks_json FROM structure_results WHERE id = ?", (structure_result_id,)
    ).fetchone()
    if row is None or not row["success"] or not row["blocks_json"]:
        return None
    return json.loads(row["blocks_json"])


def _pages_for_canonical_chunking(
    conn: sqlite3.Connection, extracted_doc_id: int, canonical_document: CanonicalDocument
) -> list[PageForChunking]:
    """Derive PageForChunking exclusively from canonical_document.pages
    (decision 4) — never reads pages.text or blocks_json as a body-text
    source for this route. Validates that the canonical page numbers
    exactly match the database document's page numbers; fails loudly on
    mismatch rather than silently chunking a stale/mismatched document.
    """
    db_page_numbers = [
        row["page_number"]
        for row in conn.execute(
            "SELECT page_number FROM pages WHERE extracted_doc_id = ? ORDER BY page_number",
            (extracted_doc_id,),
        ).fetchall()
    ]
    canonical_page_numbers = [p.page_number for p in canonical_document.pages]
    if canonical_page_numbers != db_page_numbers:
        raise ValueError(
            f"extracted_doc_id={extracted_doc_id}: canonical_document page numbers "
            f"{canonical_page_numbers} do not match database page numbers {db_page_numbers}"
        )

    pages: list[PageForChunking] = []
    for page in canonical_document.pages:
        source_type = _CANONICAL_MODE_TO_SOURCE_TYPE.get(page.mode, "none")
        text = page.text if source_type != "none" else ""
        pages.append(
            PageForChunking(page_number=page.page_number, text=text, text_source=source_type)
        )
    return pages


def _load_stored_chunks(conn: sqlite3.Connection, extracted_doc_id: int) -> list[Chunk]:
    rows = conn.execute(
        "SELECT chunk_index, page_start, page_end, source_type, text, chunk_hash, "
        "section, block_type, bbox_json, reading_order, embedding_text, embedding_input_hash "
        "FROM chunks WHERE extracted_doc_id = ? ORDER BY chunk_index",
        (extracted_doc_id,),
    ).fetchall()
    return [
        Chunk(
            chunk_index=row["chunk_index"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            source_type=row["source_type"],
            text=row["text"],
            chunk_hash=row["chunk_hash"],
            section=row["section"],
            block_type=row["block_type"],
            bbox_json=row["bbox_json"],
            reading_order=row["reading_order"],
            embedding_text=row["embedding_text"],
            embedding_input_hash=row["embedding_input_hash"],
        )
        for row in rows
    ]


def _canonical_source_content_hash(
    pages: list[PageForChunking], source: SourceProvenance, title: str | None
) -> str:
    """Extends the base source-content hash (page text) with exactly the
    trusted metadata that affects embedding input under the canonical
    route (filename, root-relative relative_path, media_type, derived
    title, context recipe version) — a relink/rename/path change or a
    title-affecting content edit must bust this document's chunk cache,
    never silently serve stale contextual chunks. Absolute paths are
    never included (relative_path is already root-relative, per
    SourceProvenance).
    """
    base = compute_source_content_hash(pages)
    payload = json.dumps(
        {
            "filename": source.filename,
            "relative_path": source.relative_path,
            "media_type": source.media_type,
            "title": title,
            "context_recipe_version": CONTEXT_RECIPE_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(f"{base}\x1d{payload}".encode("utf-8")).hexdigest()


def _chunk_document_with_structure(
    conn: sqlite3.Connection, pages: list[PageForChunking], config: ChunkerConfig
) -> list[Chunk]:
    """Per-page: a page with a successful structure result is chunked by
    chunk_page_blocks(); any other page (structure never ran, or failed)
    falls back to the same flat chunk_page_text() logic chunk_document()
    itself uses — chunk_index stays one running sequence across the
    whole document, matching chunk_document()'s convention.
    """
    chunks: list[Chunk] = []
    chunk_index = 0
    for page in pages:
        blocks = get_blocks_for_page(conn, page.structure_result_id)
        if blocks is not None:
            page_chunks = chunk_page_blocks(
                page.page_number, page.text_source, blocks,
                config.chunk_size, config.chunk_overlap, chunk_index,
            )
        else:
            page_chunks = [
                Chunk(
                    chunk_index=chunk_index + i,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    source_type=page.text_source,
                    text=piece,
                    chunk_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                )
                for i, piece in enumerate(chunk_page_text(page.text, config.chunk_size, config.chunk_overlap))
            ]
        chunks.extend(page_chunks)
        chunk_index += len(page_chunks)
    return chunks


def get_or_create_chunks(
    conn: sqlite3.Connection,
    file_id: int,
    extracted_doc_id: int,
    config: ChunkerConfig,
    *,
    canonical_document: CanonicalDocument | None = None,
) -> tuple[list[Chunk], bool]:
    """Get-or-compute this document's chunks under `config`.

    Slice B / Section 3: when `canonical_document` is supplied, canonical
    Markdown page text (decision 1) is the sole body-text authority for
    this call — pages are derived exclusively from
    canonical_document.pages (decision 4), chunked Markdown-aware and
    page-aware by chunk/markdown_chunker.py (never blocks_json, never
    chunk_page_blocks), under a distinct chunker name/version so this
    route can never falsely cache-hit a legacy/structure-aware run.
    Canonical chunks also carry a separate `embedding_text`/
    `embedding_input_hash` (trusted-provenance-prefixed, for embedding
    input only — see pipeline/embed/context.py); `chunk_hash`/`text`
    remain the exact visible/searchable identity, untouched. Omitting
    `canonical_document` (the default) preserves every existing caller's
    behavior and cache compatibility unchanged (decision 3).

    Returns (chunks, cache_hit). On a cache hit, no chunker is invoked and
    the `chunks` table is never touched — only chunk_runs (and, on a
    miss, `chunks`) are read/written. Extraction (`extracted_docs`,
    `pages`) and OCR (`ocr_results`) are never written by this function at
    all, on either path — a chunk-settings-only change structurally cannot
    repeat extraction or OCR.

    Stage 10: `use_structure` (any page of the doc has a successful
    structure result) decides both which chunker name/version the cache
    key records and which chunker actually runs on a miss. A document
    without structure keeps cache-hitting its existing flat chunks
    exactly as before Stage 10 (structure_hashes omitted from the source
    content hash, so the hash — and therefore chunk_runs comparison —
    stays byte-identical). A document that *gains* structure busts its
    cache exactly once, via both the name/version change and the hash
    change together; after that it cache-hits normally.
    """
    config_hash = config.config_hash()
    use_structure = False
    canonical_title: str | None = None
    canonical_folder: str = ""

    if canonical_document is not None:
        pages = _pages_for_canonical_chunking(conn, extracted_doc_id, canonical_document)
        chunker_name, chunker_version = CANONICAL_CHUNKER_NAME, CANONICAL_CHUNKER_VERSION
        canonical_title = derive_document_title([p.text for p in canonical_document.pages])
        canonical_folder = folder_from_relative_path(canonical_document.source.relative_path)
        source_content_hash = _canonical_source_content_hash(
            pages, canonical_document.source, canonical_title
        )
    else:
        pages = get_pages_for_chunking(conn, extracted_doc_id)
        structure_blocks = [get_blocks_for_page(conn, p.structure_result_id) for p in pages]
        use_structure = any(b is not None for b in structure_blocks)
        if use_structure:
            chunker_name, chunker_version = STRUCTURE_CHUNKER_NAME, STRUCTURE_CHUNKER_VERSION
            structure_hashes = [
                hashlib.sha256(json.dumps(b, sort_keys=True).encode("utf-8")).hexdigest() if b is not None else ""
                for b in structure_blocks
            ]
            source_content_hash = compute_source_content_hash(pages, structure_hashes)
        else:
            chunker_name, chunker_version = CHUNKER_NAME, CHUNKER_VERSION
            source_content_hash = compute_source_content_hash(pages)

    run_row = conn.execute(
        "SELECT * FROM chunk_runs WHERE extracted_doc_id = ?", (extracted_doc_id,)
    ).fetchone()
    if (
        run_row is not None
        and run_row["source_content_hash"] == source_content_hash
        and run_row["chunker_name"] == chunker_name
        and run_row["chunker_version"] == chunker_version
        and run_row["config_hash"] == config_hash
    ):
        return _load_stored_chunks(conn, extracted_doc_id), True

    if canonical_document is not None:
        chunks = chunk_canonical_markdown_document(
            pages,
            config,
            filename=canonical_document.source.filename,
            folder=canonical_folder,
            media_type=canonical_document.source.media_type,
            title=canonical_title,
        )
    elif use_structure:
        chunks = _chunk_document_with_structure(conn, pages, config)
    else:
        chunks = chunk_document(pages, config)

    conn.execute("DELETE FROM chunks WHERE extracted_doc_id = ?", (extracted_doc_id,))
    now = datetime.now(timezone.utc).isoformat()
    for chunk in chunks:
        conn.execute(
            "INSERT INTO chunks "
            "(file_id, extracted_doc_id, chunk_index, page_start, page_end, source_type, "
            " text, chunk_hash, section, block_type, bbox_json, reading_order, "
            " embedding_text, embedding_input_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                extracted_doc_id,
                chunk.chunk_index,
                chunk.page_start,
                chunk.page_end,
                chunk.source_type,
                chunk.text,
                chunk.chunk_hash,
                chunk.section,
                chunk.block_type,
                chunk.bbox_json,
                chunk.reading_order,
                chunk.embedding_text,
                chunk.embedding_input_hash,
                now,
            ),
        )
    conn.execute(
        "INSERT INTO chunk_runs "
        "(extracted_doc_id, source_content_hash, chunker_name, chunker_version, config_hash, "
        " chunk_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(extracted_doc_id) DO UPDATE SET "
        "source_content_hash = excluded.source_content_hash, "
        "chunker_name = excluded.chunker_name, "
        "chunker_version = excluded.chunker_version, "
        "config_hash = excluded.config_hash, "
        "chunk_count = excluded.chunk_count, "
        "created_at = excluded.created_at",
        (
            extracted_doc_id,
            source_content_hash,
            chunker_name,
            chunker_version,
            config_hash,
            len(chunks),
            now,
        ),
    )
    conn.commit()

    return chunks, False
