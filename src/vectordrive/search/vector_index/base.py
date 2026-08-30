"""Shared types for the replaceable VectorIndex abstraction."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VectorHit:
    """A search hit from a VectorIndex. score is always higher-is-better,
    regardless of the backend's native metric (e.g. SqliteVecIndex negates
    L2 distance; NumpyVectorIndex uses cosine similarity directly) — this
    keeps FTS and vector hits comparable for RRF fusion.
    """

    chunk_id: int
    score: float


def compute_model_key(provider: str, model: str, dimensions: int, config_hash: str) -> str:
    """A single partition key identifying one (provider, model, dimensions,
    config) combination — the same identity that keys the M7 embeddings
    cache, reused here so the vector index and the embedding cache always
    agree on what "the same embedding configuration" means.
    """
    payload = f"{provider}:{model}:{dimensions}:{config_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class VectorIndex(Protocol):
    """Any vector search backend (sqlite-vec, NumPy, ...) implements this.
    All vectors for a given model_key are logically partitioned from every
    other model_key — switching embedding models never mixes results.
    """

    def upsert(self, chunk_id: int, model_key: str, vector: list[float]) -> None: ...
    def delete(self, chunk_id: int, model_key: str) -> None: ...
    def list_chunk_ids(self, model_key: str) -> set[int]: ...
    def search(self, model_key: str, query_vector: list[float], top_k: int) -> list[VectorHit]: ...
