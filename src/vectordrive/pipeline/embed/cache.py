"""Embedding-stage cache: wraps any EmbeddingProvider with a DB-backed
cache keyed by (input_hash, provider, model, dimensions, config_hash).

Section 3: `input_hash`/the text actually sent to the provider is
`chunk.embedding_input_hash`/`chunk.embedding_text` when the chunk carries
them (canonical Markdown-aware chunks — a trusted-provenance-prefixed
string distinct from the visible `chunk.text`), falling back to
`chunk.chunk_hash`/`chunk.text` otherwise (legacy flat/structure-aware
chunks — unchanged behavior). Content-addressed either way — two chunks
whose provider input is identical, anywhere, share one cached vector.
Failures are cached too (success=0, error_message set), mirroring the
OCR/extraction caches: a known-bad input/config combination is recorded
clearly rather than silently retried on every run.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from vectordrive.pipeline.chunk.base import Chunk
from vectordrive.pipeline.embed.base import (
    DEFAULT_EMBED_BATCH_SIZE,
    EmbeddingConfig,
    EmbeddingProvider,
    EmbeddingResult,
)


def get_cached_embedding(
    conn: sqlite3.Connection,
    input_hash: str,
    provider_name: str,
    model: str,
    dimensions: int,
    config_hash: str,
) -> EmbeddingResult | None:
    row = conn.execute(
        "SELECT * FROM embeddings WHERE input_hash = ? AND provider = ? AND model = ? "
        "AND dimensions = ? AND config_hash = ?",
        (input_hash, provider_name, model, dimensions, config_hash),
    ).fetchone()
    if row is None:
        return None
    vector = json.loads(row["vector_json"]) if row["vector_json"] else None
    return EmbeddingResult(
        provider=provider_name,
        model=model,
        dimensions=dimensions,
        success=bool(row["success"]),
        vector=vector,
        error_message=row["error_message"],
    )


def store_embedding(
    conn: sqlite3.Connection, input_hash: str, config_hash: str, result: EmbeddingResult
) -> int:
    vector_json = json.dumps(result.vector) if result.vector is not None else None
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO embeddings "
        "(input_hash, provider, model, dimensions, config_hash, vector_json, success, "
        " error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            input_hash,
            result.provider,
            result.model,
            result.dimensions,
            config_hash,
            vector_json,
            int(result.success),
            result.error_message,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _input_identity(chunk: Chunk) -> tuple[str, str]:
    """(input_hash, input_text) actually sent to/cached for the provider —
    the chunk's contextual embedding identity when present (canonical
    Markdown-aware chunks), else its visible text identity (legacy
    chunks). Never the other way around: `chunk.chunk_hash`/`chunk.text`
    are only ever a fallback, never overridden when contextual fields ARE
    present.
    """
    if chunk.embedding_input_hash is not None and chunk.embedding_text is not None:
        return chunk.embedding_input_hash, chunk.embedding_text
    return chunk.chunk_hash, chunk.text


def get_or_create_embeddings(
    conn: sqlite3.Connection,
    chunks: list[Chunk],
    provider: EmbeddingProvider,
    config: EmbeddingConfig,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
) -> list[tuple[EmbeddingResult, bool]]:
    """Get-or-batch-compute embeddings for `chunks` under `provider`+`config`
    (M10.5 requirement 3).

    Cache lookups happen first, once per unique input_hash — a chunk whose
    embedding is already cached is NEVER sent to the provider, and two
    chunks sharing identical provider input (same input_hash) share one
    lookup/embed even within the same call. Every input_hash still missing
    afterward is embedded via provider.embed_documents() in groups of at most
    `batch_size` — one HTTP request per group, not one per chunk. An
    unexpected exception from the provider, or a provider that returns the
    wrong number of results for a batch, is caught here as a defensive
    second layer and turned into a clearly-recorded failure for every chunk
    in that batch, never silently lost or misaligned.

    Returns (result, cache_hit) pairs in the same order as `chunks`.
    """
    config_hash = config.config_hash()
    step = max(batch_size, 1)

    text_by_hash: dict[str, str] = {}
    hash_order: list[str] = []
    hash_for_chunk: list[str] = []
    for chunk in chunks:
        input_hash, input_text = _input_identity(chunk)
        hash_for_chunk.append(input_hash)
        if input_hash not in text_by_hash:
            hash_order.append(input_hash)
        text_by_hash[input_hash] = input_text

    result_by_hash: dict[str, tuple[EmbeddingResult, bool]] = {}
    to_embed: list[str] = []
    for input_hash in hash_order:
        cached = get_cached_embedding(
            conn, input_hash, provider.NAME, config.model, config.dimensions, config_hash
        )
        if cached is not None:
            result_by_hash[input_hash] = (cached, True)
        else:
            to_embed.append(input_hash)

    for start in range(0, len(to_embed), step):
        batch_hashes = to_embed[start : start + step]
        batch_texts = [text_by_hash[h] for h in batch_hashes]

        try:
            batch_results = provider.embed_documents(batch_texts, config)
        except Exception as exc:
            batch_results = None
            error_message = f"{type(exc).__name__}: {exc}"
        else:
            error_message = f"Provider returned {len(batch_results)} results for {len(batch_texts)} inputs."

        if batch_results is None or len(batch_results) != len(batch_texts):
            batch_results = [
                EmbeddingResult(
                    provider=provider.NAME, model=config.model, dimensions=config.dimensions,
                    success=False, error_message=error_message,
                )
                for _ in batch_texts
            ]

        for input_hash, result in zip(batch_hashes, batch_results):
            store_embedding(conn, input_hash, config_hash, result)
            result_by_hash[input_hash] = (result, False)

    return [result_by_hash[input_hash] for input_hash in hash_for_chunk]
