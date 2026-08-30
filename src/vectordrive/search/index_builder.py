"""Populates a VectorIndex from the already-cached `chunks`+`embeddings`
tables — never re-runs extraction, OCR, chunking, or embedding (this
module doesn't import anything from those pipeline stages at all, and
reads no table but `chunks`/`embeddings`, so requirement 10 holds
structurally, not just by convention).

Correctly removes stale entries (requirement 9): any chunk_id currently in
the index under this model_key that's no longer a live, successfully-
embedded chunk (e.g. because M6 rechunked and deleted the old row) gets
deleted from the index too.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from vectordrive.search.vector_index.base import VectorIndex, compute_model_key


@dataclass(frozen=True)
class IndexBuildStats:
    indexed: int
    removed: int


class VectorIndexBuildPaused(RuntimeError):
    """Vector finalization stopped at a safe, idempotent item boundary."""


def build_vector_index(
    conn: sqlite3.Connection,
    index: VectorIndex,
    provider: str,
    model: str,
    dimensions: int,
    config_hash: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    cancellation_batch_size: int = 256,
) -> IndexBuildStats:
    model_key = compute_model_key(provider, model, dimensions, config_hash)

    # Section 3: joins on each chunk's embedding-input identity —
    # embedding_input_hash when the chunk carries one (canonical
    # Markdown-aware chunks, contextual embedding input), falling back to
    # chunk_hash (legacy flat/structure-aware chunks, plain visible-text
    # identity) via COALESCE. This must exactly mirror
    # embed/cache.py's _input_identity() resolution order.
    rows = conn.execute(
        "SELECT chunks.id AS chunk_id, embeddings.vector_json AS vector_json "
        "FROM chunks JOIN embeddings "
        "ON COALESCE(chunks.embedding_input_hash, chunks.chunk_hash) = embeddings.input_hash "
        "WHERE embeddings.provider = ? AND embeddings.model = ? AND embeddings.dimensions = ? "
        "AND embeddings.config_hash = ? AND embeddings.success = 1",
        (provider, model, dimensions, config_hash),
    ).fetchall()

    current_ids: set[int] = set()
    poll_every = max(1, cancellation_batch_size)
    for position, row in enumerate(rows):
        if position % poll_every == 0 and should_cancel is not None and should_cancel():
            raise VectorIndexBuildPaused("vector finalization paused")
        vector = json.loads(row["vector_json"])
        index.upsert(row["chunk_id"], model_key, vector)
        current_ids.add(row["chunk_id"])

    existing_ids = index.list_chunk_ids(model_key)
    stale_ids = existing_ids - current_ids
    for position, chunk_id in enumerate(sorted(stale_ids)):
        if position % poll_every == 0 and should_cancel is not None and should_cancel():
            raise VectorIndexBuildPaused("vector finalization paused")
        index.delete(chunk_id, model_key)

    return IndexBuildStats(indexed=len(current_ids), removed=len(stale_ids))
