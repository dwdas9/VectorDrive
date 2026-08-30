"""Primary VectorIndex backend: sqlite-vec's vec0 virtual table.

Two things were verified live against the real extension before writing
this (not assumed from docs):
- `vec0`'s `+column` auxiliary columns CANNOT be used in a KNN query's
  WHERE clause ("illegal WHERE constraint on a vec0 auxiliary column").
  A real `PARTITION KEY` column is required instead, and that *does* work
  combined with `MATCH ... ORDER BY distance`.
- `INSERT OR REPLACE` is not supported on vec0 tables (raises a UNIQUE
  constraint error even with OR REPLACE). Upsert is delete-then-insert.

This table is NOT created by schema.sql/connection.py (see the comment
there) — its shape depends on the active embedding dimensionality, decided
here, at the search layer.
"""
from __future__ import annotations

import sqlite3

import sqlite_vec

from vectordrive.search.vector_index.base import VectorHit


class SqliteVecIndex:
    def __init__(self, conn: sqlite3.Connection, dimensions: int, table_name: str = "vec_chunks"):
        self._conn = conn
        self._dimensions = dimensions
        self.table_name = table_name
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table_name} USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, "
            f"embedding float[{self._dimensions}], "
            f"model_key TEXT PARTITION KEY)"
        )
        self._conn.commit()

    def _validate(self, vector: list[float]) -> None:
        if len(vector) != self._dimensions:
            raise ValueError(
                f"Expected a {self._dimensions}-dimensional vector, got {len(vector)}."
            )

    def upsert(self, chunk_id: int, model_key: str, vector: list[float]) -> None:
        """Insert or replace `chunk_id`'s vector under `model_key`.

        Verified live: vec0's `chunk_id INTEGER PRIMARY KEY` is unique
        across the *whole* table, not per-partition — PARTITION KEY only
        affects physical layout/filtering, not primary-key scope. So the
        delete here is intentionally by chunk_id alone (any partition),
        not chunk_id+model_key: re-indexing a chunk under a new model_key
        evicts whatever model_key it was previously indexed under. In
        practice this matches how the index is actually used — one active
        embedding model's vector per chunk at a time.
        """
        self._validate(vector)
        self._conn.execute(f"DELETE FROM {self.table_name} WHERE chunk_id = ?", (chunk_id,))
        self._conn.execute(
            f"INSERT INTO {self.table_name}(chunk_id, embedding, model_key) VALUES (?, ?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(vector), model_key),
        )
        self._conn.commit()

    def delete(self, chunk_id: int, model_key: str) -> None:
        self._conn.execute(
            f"DELETE FROM {self.table_name} WHERE chunk_id = ? AND model_key = ?",
            (chunk_id, model_key),
        )
        self._conn.commit()

    def list_chunk_ids(self, model_key: str) -> set[int]:
        rows = self._conn.execute(
            f"SELECT chunk_id FROM {self.table_name} WHERE model_key = ?", (model_key,)
        ).fetchall()
        return {row["chunk_id"] for row in rows}

    def search(self, model_key: str, query_vector: list[float], top_k: int) -> list[VectorHit]:
        self._validate(query_vector)
        rows = self._conn.execute(
            f"SELECT chunk_id, distance FROM {self.table_name} "
            f"WHERE embedding MATCH ? AND model_key = ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(query_vector), model_key, top_k),
        ).fetchall()
        # score is higher-is-better (negated L2 distance): 0.0 at an exact
        # match, more negative the farther away — see VectorHit's docstring.
        return [VectorHit(chunk_id=row["chunk_id"], score=-row["distance"]) for row in rows]
