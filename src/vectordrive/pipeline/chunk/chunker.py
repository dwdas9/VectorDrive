"""Page-aware, deterministic chunker: normalized document text (native PDF
text or cached OCR text) -> searchable Chunk objects. A chunk never spans
two pages (requirement 3) — this is enforced structurally: chunk_page_text
operates on one page's text at a time, and chunk_document never
concatenates across the page loop boundary.
"""
from __future__ import annotations

import hashlib

from vectordrive.pipeline.chunk.base import Chunk, ChunkerConfig, PageForChunking

CHUNKER_NAME = "char-sliding-window"
CHUNKER_VERSION = "1"


def chunk_page_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split one page's text into overlapping character-window pieces.

    Deterministic (pure function of text + config), never returns empty or
    whitespace-only pieces, and never produces a zero/negative step (which
    would otherwise loop forever or emit an identical slice twice).
    """
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be smaller than chunk_size ({chunk_size})"
        )

    stripped = text.strip()
    if not stripped:
        return []

    step = chunk_size - chunk_overlap
    pieces: list[str] = []
    start = 0
    length = len(stripped)
    while start < length:
        end = min(start + chunk_size, length)
        piece = stripped[start:end].strip()
        if piece:
            pieces.append(piece)
        if end == length:
            break
        start += step
    return pieces


def chunk_document(pages: list[PageForChunking], config: ChunkerConfig) -> list[Chunk]:
    """Chunk every page's resolved text, in page order. Pages with no
    resolved text (text_source='none', or blank) are skipped — never
    chunked, never produce empty Chunks (requirement 10).
    """
    chunks: list[Chunk] = []
    chunk_index = 0
    for page in pages:
        for piece in chunk_page_text(page.text, config.chunk_size, config.chunk_overlap):
            chunk_hash = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            chunks.append(
                Chunk(
                    chunk_index=chunk_index,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    source_type=page.text_source,
                    text=piece,
                    chunk_hash=chunk_hash,
                )
            )
            chunk_index += 1
    return chunks


def compute_source_content_hash(
    pages: list[PageForChunking], structure_hashes: list[str] | None = None
) -> str:
    """Hash of every page's resolved (text, text_source), in page order.

    This is the "source-content hash" half of the chunk cache key
    (requirement 7): it changes whenever extraction or OCR output changes
    for this document, correctly busting the chunk cache, but is entirely
    independent of the chunker/config — so a chunk-settings-only change
    never needs to touch this value or anything upstream of it.

    Stage 10: `structure_hashes` (one per page, "" where a page has no
    successful structure result) folds each page's blocks_json hash into
    the same payload, so a document whose structure analysis result
    changes (a re-run with a different engine/config, or newly-succeeded
    structure on a previously-failed page) also busts the chunk cache —
    otherwise get_or_create_chunks() would keep serving stale flat
    chunks forever, never noticing structure became available. `None`
    (the default) omits this entirely, producing byte-identical output
    to before Stage 10 — every chunk_runs row written pre-Stage-10 still
    matches on the next call.
    """
    parts = [f"{p.page_number}\x1f{p.text_source}\x1f{p.text}" for p in pages]
    if structure_hashes is not None:
        parts = [f"{part}\x1f{h}" for part, h in zip(parts, structure_hashes)]
    payload = "\x1e".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
