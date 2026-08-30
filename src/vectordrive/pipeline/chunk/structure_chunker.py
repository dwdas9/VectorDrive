"""Structure-aware chunker (Stage 10): turns a page's PP-StructureV3
blocks (deserialized from structure_results.blocks_json — see
pipeline/structure/cache.py's wire format, §6.7 of the plan) into
Chunk objects carrying section/block_type/bbox_json/reading_order,
instead of the flat chunker's plain character-window slicing.

Only used for a page whose structure analysis succeeded; get_or_create_
chunks (chunk/cache.py) decides per-document whether to use this or the
flat chunker and falls back to flat for any page without blocks.
"""
from __future__ import annotations

import hashlib
import json

from vectordrive.pipeline.chunk.base import Chunk
from vectordrive.pipeline.chunk.chunker import chunk_page_text

STRUCTURE_CHUNKER_NAME = "structure-aware"
STRUCTURE_CHUNKER_VERSION = "1"

_SKIP_TYPES = ("header", "footer")
_HEADING_TYPES = ("title", "paragraph_title", "doc_title")


def _chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_pieces(table_text: str, chunk_size: int) -> list[str]:
    if len(table_text) <= chunk_size:
        return [table_text]

    rows = table_text.splitlines()
    header = rows[0:2]  # GFM: header row + separator row
    pieces: list[str] = []
    buf = list(header)
    for row in rows[2:]:
        if len("\n".join(buf + [row])) > chunk_size and len(buf) > 2:
            pieces.append("\n".join(buf))
            buf = list(header)
        buf.append(row)
    pieces.append("\n".join(buf))
    return pieces


def chunk_page_blocks(
    page_number: int,
    text_source: str,
    blocks: list[dict],
    chunk_size: int,
    chunk_overlap: int,
    start_index: int,
) -> list[Chunk]:
    """Returns Chunks with chunk_index starting at `start_index`, so a
    multi-page document's chunk_index stays a single running sequence
    across pages (matching chunk_document()'s convention).
    """
    ordered = sorted(blocks, key=lambda b: b.get("reading_order", 0))

    out: list[Chunk] = []
    current_section: str | None = None
    next_index = start_index

    for block in ordered:
        block_type = block.get("type") or ""
        text = block.get("text") or ""
        bbox = block.get("bbox")
        reading_order = block.get("reading_order")
        bbox_json = json.dumps(bbox) if bbox else None

        if block_type in _SKIP_TYPES:
            continue

        if block_type in _HEADING_TYPES:
            heading_text = text.strip()
            if heading_text:
                current_section = heading_text
            continue  # heading itself is never its own chunk — it rides along as a prefix

        if block_type == "table":
            pieces = _table_pieces(text, chunk_size)
        else:
            body = text.strip()
            if not body:
                continue
            prefix = (current_section + "\n") if current_section else ""
            if len(prefix + body) <= chunk_size:
                pieces = [prefix + body]
            else:
                budget = max(chunk_size - len(prefix), 1)
                pieces = [prefix + p for p in chunk_page_text(body, budget, chunk_overlap)]

        for piece in pieces:
            out.append(
                Chunk(
                    chunk_index=next_index,
                    page_start=page_number,
                    page_end=page_number,
                    source_type=text_source,
                    text=piece,
                    chunk_hash=_chunk_hash(piece),
                    section=current_section,
                    block_type=block_type,
                    bbox_json=bbox_json,
                    reading_order=reading_order,
                )
            )
            next_index += 1

    return out
