"""Shared types for the chunking stage."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 150


@dataclass(frozen=True)
class ChunkerConfig:
    """Configurable chunk size/overlap. Part of the chunk cache key
    alongside the chunker's own name/version and the document's
    source_content_hash — any change here is a deliberate cache-busting
    change (requirement 9: rebuilds only chunks, never extraction/OCR).
    """

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP

    def config_hash(self) -> str:
        payload = json.dumps(
            {"chunk_size": self.chunk_size, "chunk_overlap": self.chunk_overlap}, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PageForChunking:
    """A page's resolved text, ready to chunk: native PDF text for normal
    pages, cached OCR text for scanned pages. text_source is 'native',
    'ocr', or 'none' (no resolved text yet — such pages are skipped, never
    chunked).

    Stage 10: structure_result_id is optional (None for legacy rows and
    documents structure analysis never ran on) — get_or_create_chunks
    uses it to look up this page's blocks via chunk/cache.py's
    get_blocks_for_page() and decide flat vs. structure-aware chunking.
    """

    page_number: int
    text: str
    text_source: str
    structure_result_id: int | None = None


@dataclass(frozen=True)
class Chunk:
    """One chunk of searchable text, always confined to a single page
    (page_start == page_end) — requirement 3.

    Stage 10: section/block_type/bbox_json/reading_order are optional,
    structure-aware metadata (populated only by structure_chunker.py's
    chunk_page_blocks; the flat char-sliding-window chunker inserts
    None for all four, matching schema.sql's NULL default). All
    existing Chunk(...) constructor calls are unaffected.
    """

    chunk_index: int
    page_start: int
    page_end: int
    source_type: str  # 'native' | 'ocr'
    text: str
    chunk_hash: str
    section: str | None = None
    block_type: str | None = None
    bbox_json: str | None = None
    reading_order: int | None = None
    # Section 3: embedding-only contextual input, distinct from `text`/
    # `chunk_hash` (the exact user-facing/searchable identity — never
    # touched by this). `embedding_text` is `text` with a deterministic
    # trusted-provenance prefix (filename/folder/title/type/page/section)
    # prepended for embedding provider input only; `embedding_input_hash`
    # is its hash, used as the embedding cache/index key. Both are None
    # for legacy chunkers (flat/structure-aware) — callers fall back to
    # `text`/`chunk_hash` in that case (see embed/cache.py). Only the
    # canonical Markdown-aware chunker (chunk/markdown_chunker.py)
    # populates these.
    embedding_text: str | None = None
    embedding_input_hash: str | None = None
