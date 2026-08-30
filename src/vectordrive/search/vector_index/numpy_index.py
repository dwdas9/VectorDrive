"""Fallback VectorIndex backend: pure NumPy cosine similarity, in-memory.

Used only if `doctor`'s sqlite-vec Apple Silicon smoke test ever fails
(per spec). No dependency on sqlite_vec at all — deliberately, so this
backend works even in an environment where the extension can't load.
Unlike SqliteVecIndex, a chunk_id has no cross-partition uniqueness
constraint here: each model_key is an independent namespace.
"""
from __future__ import annotations

import numpy as np

from vectordrive.search.vector_index.base import VectorHit


class NumpyVectorIndex:
    def __init__(self):
        self._vectors: dict[str, dict[int, np.ndarray]] = {}

    def upsert(self, chunk_id: int, model_key: str, vector: list[float]) -> None:
        self._vectors.setdefault(model_key, {})[chunk_id] = np.asarray(vector, dtype=np.float32)

    def delete(self, chunk_id: int, model_key: str) -> None:
        self._vectors.get(model_key, {}).pop(chunk_id, None)

    def list_chunk_ids(self, model_key: str) -> set[int]:
        return set(self._vectors.get(model_key, {}).keys())

    def search(self, model_key: str, query_vector: list[float], top_k: int) -> list[VectorHit]:
        partition = self._vectors.get(model_key, {})
        if not partition:
            return []

        chunk_ids = list(partition.keys())
        matrix = np.stack([partition[cid] for cid in chunk_ids])
        query = np.asarray(query_vector, dtype=np.float32)

        matrix_norms = np.linalg.norm(matrix, axis=1)
        query_norm = np.linalg.norm(query)
        denom = matrix_norms * query_norm
        denom = np.where(denom == 0, 1e-10, denom)  # avoid div-by-zero for a zero vector
        similarities = (matrix @ query) / denom

        # Deterministic ordering: best score first, ties broken by chunk_id
        # ascending — argsort alone isn't guaranteed stable/tie-consistent
        # across NumPy versions, so tie-break explicitly.
        ranked = sorted(
            zip(chunk_ids, similarities.tolist()), key=lambda pair: (-pair[1], pair[0])
        )
        return [VectorHit(chunk_id=cid, score=score) for cid, score in ranked[:top_k]]
