"""Shared read-only retrieval orchestration for the GUI and local chat.

The low-level search package deliberately works with an already-open
connection and vector index.  This service owns the application-level work
of opening VectorDrive's database, selecting the persisted vector backend,
embedding a semantic query, and preserving the stable user-facing errors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from vectordrive.config import get_config_path, get_db_path, load_config
from vectordrive.db.readonly import open_readonly_connection, try_load_sqlite_vec, vec_table_exists
from vectordrive.pipeline.embed.base import EmbeddingProvider
from vectordrive.search.hybrid import SearchResult, hybrid_search, keyword_search, vector_search
from vectordrive.services.engines import build_search_stack
from vectordrive.services.errors import VectorDriveError
from vectordrive.services.processing_run_service import require_completed_index

SearchMode = Literal["fts", "vector", "hybrid"]


def search_index(
    data_home: Path,
    query: str,
    *,
    mode: SearchMode | str = "hybrid",
    top_k: int = 10,
    embed_provider_cls: type[EmbeddingProvider] | None = None,
) -> list[SearchResult]:
    """Search the indexed database without ever opening it for writes.

    ``embed_provider_cls`` is an explicit test/integration seam.  Production
    callers leave it unset and receive the engine factory's normal Ollama
    provider.  Keyword-only search does not construct an embedding provider.
    """
    if not query or not query.strip():
        raise VectorDriveError("Query must not be empty.", code="invalid_query")
    if mode not in ("fts", "vector", "hybrid"):
        raise VectorDriveError(f"Unknown search mode: {mode!r}", code="invalid_mode")

    conn = open_readonly_connection(get_db_path(Path(data_home)))
    try:
        require_completed_index(conn)
        if mode == "fts":
            return keyword_search(conn, query, top_k)

        config = load_config(get_config_path(Path(data_home)))
        vector_backend = "sqlite_vec" if (vec_table_exists(conn) and try_load_sqlite_vec(conn)) else "numpy"
        stack_kwargs = {}
        if embed_provider_cls is not None:
            stack_kwargs["embed_provider_cls"] = embed_provider_cls
        stack = build_search_stack(conn, config, vector_backend, readonly=True, **stack_kwargs)
        embed_result = stack.embed_provider.embed_query(query, stack.embed_config)
        if not embed_result.success:
            raise VectorDriveError(
                f"Could not create a query embedding: {embed_result.error_message}",
                code="embedding_failed",
                hint="Try FTS mode instead — it doesn't need Ollama.",
            )
        if mode == "vector":
            return vector_search(conn, stack.vector_index, stack.model_key, embed_result.vector, top_k)
        return hybrid_search(conn, stack.vector_index, stack.model_key, query, embed_result.vector, top_k)
    finally:
        conn.close()
