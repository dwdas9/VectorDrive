"""Embeds the cached chunks produced by M6 (requirement 4) — a thin
orchestration wrapper over get_or_create_embeddings (M10.5: batched, one
HTTP request per group of at most `batch_size` chunks rather than one per
chunk). Never touches extraction/OCR/chunking tables — those are read
nowhere in this module, so a settings-only change here structurally
cannot repeat them (requirement 9).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from vectordrive.pipeline.chunk.base import Chunk
from vectordrive.pipeline.embed.base import DEFAULT_EMBED_BATCH_SIZE, EmbeddingConfig, EmbeddingProvider
from vectordrive.pipeline.embed.cache import get_or_create_embeddings


@dataclass
class ChunkEmbeddingOutcome:
    chunk_index: int
    success: bool
    dimensions: int | None
    error_message: str | None


def embed_chunks(
    conn: sqlite3.Connection,
    chunks: list[Chunk],
    provider: EmbeddingProvider,
    config: EmbeddingConfig,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
) -> list[ChunkEmbeddingOutcome]:
    outcomes: list[ChunkEmbeddingOutcome] = []
    for chunk, (result, _) in zip(chunks, get_or_create_embeddings(conn, chunks, provider, config, batch_size)):
        outcomes.append(
            ChunkEmbeddingOutcome(
                chunk_index=chunk.chunk_index,
                success=result.success,
                dimensions=result.dimensions if result.success else None,
                error_message=result.error_message,
            )
        )
    return outcomes
