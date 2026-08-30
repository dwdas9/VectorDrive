"""Read-only status/stats queries shared by `vectordrive status` and (from
G4 onward) the GUI's Overview screen. Extracted from cli/status_cmd.py's
three inline queries, plus DB size (incl. WAL sidecar) and chunk/embedding
counts the Overview screen also needs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from vectordrive.pipeline.embed.base import active_corpus_embedding_config


@dataclass(frozen=True)
class StatusSnapshot:
    latest_run: dict | None
    file_status_counts: dict[str, int]
    embedding_provider: str
    embedding_model: str
    embedding_dims: int
    vector_backend: str
    db_bytes: int
    wal_bytes: int
    chunk_count: int
    # v0.5.2 Overview correction: the main "embeddings" statistic must
    # describe embeddings actually referenced by the current searchable
    # corpus. It is the number of LIVE CHUNKS (counted at most once each)
    # that have a stored, successful vector under the exact identity
    # indexing/search use — active provider/model/dimensions and the
    # current EmbeddingConfig.config_hash (incl. CONTEXT_RECIPE_VERSION).
    # With zero chunks it is zero, regardless of how many reusable rows the
    # content-addressed cache still holds.
    active_embedding_count: int
    # Every successful row in the embeddings cache. These survive file
    # removal / reset on purpose (content-addressed reuse) and are NOT part
    # of the indexed corpus — surfaced separately so a large cache is never
    # mistaken for a large index.
    cached_embedding_count: int
    # Read-only PRAGMA-derived storage figures (page_count * page_size and
    # freelist_count * page_size). db_file_bytes is the logical database
    # size; db_free_bytes is reusable free space already inside the file
    # (freelist pages) that a VACUUM would reclaim — shown, never acted on.
    db_file_bytes: int
    db_free_bytes: int
    # G12 finding: files counted under file_status_counts['indexed'] can
    # still have zero searchable text if OCR failed — see
    # pipeline/indexer.py's ocr_warning. Counted separately, not
    # subtracted from the 'indexed' bucket above.
    ocr_warning_count: int


def get_status_snapshot(
    conn: sqlite3.Connection,
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dims: int,
    vector_backend: str,
    db_path: Path,
) -> StatusSnapshot:
    latest_run_row = conn.execute("SELECT * FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    latest_run = dict(latest_run_row) if latest_run_row is not None else None

    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM files GROUP BY status ORDER BY status"
    ).fetchall()
    file_status_counts = {row["status"]: row["n"] for row in status_rows}

    chunk_count = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]

    # Reusable cache rows: every successful vector we could reuse, regardless
    # of whether anything in the current corpus points at it.
    cached_embedding_count = conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE success = 1"
    ).fetchone()["n"]

    # Active corpus embeddings: count LIVE CHUNKS, at most once each, that
    # have a stored successful vector under the exact identity indexing and
    # search use — active provider/model/dimensions AND the current
    # EmbeddingConfig.config_hash (which folds in CONTEXT_RECIPE_VERSION).
    # A chunk's effective input hash mirrors pipeline/embed/cache.py's
    # _input_identity: chunks.embedding_input_hash when the canonical
    # Markdown-aware chunker populated both it and embedding_text, else the
    # legacy chunks.chunk_hash. Counting chunks (not embeddings rows)
    # prevents overcounting a chunk that has several successful cache rows
    # under different config_hash values. Zero chunks -> zero.
    active_config_hash = active_corpus_embedding_config(embedding_model, embedding_dims).config_hash()
    active_embedding_count = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM chunks c
        WHERE EXISTS (
            SELECT 1 FROM embeddings e
            WHERE e.success = 1
              AND e.provider = ?
              AND e.model = ?
              AND e.dimensions = ?
              AND e.config_hash = ?
              AND e.input_hash = CASE
                  WHEN c.embedding_input_hash IS NOT NULL AND c.embedding_text IS NOT NULL
                  THEN c.embedding_input_hash
                  ELSE c.chunk_hash
              END
        )
        """,
        (embedding_provider, embedding_model, embedding_dims, active_config_hash),
    ).fetchone()["n"]

    ocr_warning_count = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status = 'indexed' AND ocr_warning IS NOT NULL"
    ).fetchone()["n"]

    # Read-only PRAGMAs — no VACUUM, no write, safe on a read-only
    # connection. page_count * page_size is the logical file size;
    # freelist_count * page_size is reclaimable free space already inside
    # the file.
    page_size = conn.execute("PRAGMA page_size").fetchone()[0] or 0
    page_count = conn.execute("PRAGMA page_count").fetchone()[0] or 0
    freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0] or 0
    db_file_bytes = int(page_size) * int(page_count)
    db_free_bytes = int(page_size) * int(freelist_count)

    db_path = Path(db_path)
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0

    return StatusSnapshot(
        latest_run=latest_run,
        file_status_counts=file_status_counts,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dims=embedding_dims,
        vector_backend=vector_backend,
        db_bytes=db_bytes,
        wal_bytes=wal_bytes,
        chunk_count=chunk_count,
        active_embedding_count=active_embedding_count,
        cached_embedding_count=cached_embedding_count,
        db_file_bytes=db_file_bytes,
        db_free_bytes=db_free_bytes,
        ocr_warning_count=ocr_warning_count,
    )
