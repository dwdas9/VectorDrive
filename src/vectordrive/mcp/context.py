"""Long-lived, read-only search context for one MCP server process.

Built exactly once per process (in run_mcp_stdio_server()), then reused by
every tool call for the process's whole lifetime — this is what makes
"reuse the persistent sqlite-vec index, never rebuild per query"
(requirement 6) and "build the NumPy fallback in memory once, without
modifying the database" (requirement 7) structural rather than incidental:
there is nowhere in the tool-call path that constructs a new VectorIndex
or calls build_vector_index() again.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from vectordrive.config import Config
from vectordrive.db.readonly import ReadOnlySqliteVecIndex as _ReadOnlySqliteVecIndex
from vectordrive.db.readonly import try_load_sqlite_vec as _try_load_sqlite_vec
from vectordrive.mcp.readonly_db import open_readonly_connection
from vectordrive.pipeline.embed.base import EmbeddingConfig, EmbeddingProvider
from vectordrive.pipeline.embed.context import CONTEXT_RECIPE_VERSION
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.search.index_builder import build_vector_index
from vectordrive.search.vector_index.base import VectorIndex, compute_model_key
from vectordrive.search.vector_index.numpy_index import NumpyVectorIndex

# v0.2 G1: _ReadOnlySqliteVecIndex and _try_load_sqlite_vec now live in
# db/readonly.py (shared with the GUI's read screens) and are imported
# here under their original private names. This keeps
# tests/unit/test_mcp_context.py's `monkeypatch.setattr(context_module,
# "_try_load_sqlite_vec", ...)` working unchanged — the patch replaces
# this module's own attribute, which build_context() below still looks up
# by bare name at call time, regardless of where the real implementation
# lives.


@dataclass
class McpSearchContext:
    """None fields mean "the database wasn't available at startup" — kept
    so the MCP server can still start and list its tools even when
    vectordrive.db doesn't exist yet; each tool call raises a clear
    ToolError instead (see tools.py's _require_ready), rather than the
    whole process failing to start.
    """

    conn: sqlite3.Connection | None
    startup_error: str | None
    vector_backend: str | None
    vector_index: VectorIndex | None
    embed_provider: EmbeddingProvider | None
    embed_config: EmbeddingConfig | None
    model_key: str | None
    vector_index_build_count: int = 0  # test/observability hook for requirement 6's proof


def _degraded_context(message: str) -> McpSearchContext:
    return McpSearchContext(
        conn=None, startup_error=message, vector_backend=None,
        vector_index=None, embed_provider=None, embed_config=None, model_key=None,
    )


def build_context(db_path: Path, config: Config) -> McpSearchContext:
    try:
        conn = open_readonly_connection(db_path)
    except ToolError as exc:
        return _degraded_context(str(exc))

    embed_config = EmbeddingConfig(
        model=config.embedding_model,
        dimensions=config.embedding_dims,
        context_recipe_version=CONTEXT_RECIPE_VERSION,
    )
    embed_provider = OllamaEmbeddingProvider(endpoint=config.ollama_endpoint)
    model_key = compute_model_key(
        embed_provider.NAME, embed_config.model, embed_config.dimensions, embed_config.config_hash()
    )

    ctx = McpSearchContext(
        conn=conn, startup_error=None, vector_backend=None,
        vector_index=None, embed_provider=embed_provider, embed_config=embed_config, model_key=model_key,
    )

    # M12: a corrupt (but openable) database file passes open_readonly_connection()
    # fine — SQLite only discovers "this isn't actually a database" on the
    # first real query, which happens here (the sqlite-vec probe, or
    # build_vector_index()'s SELECT below). Caught the same way a missing
    # database is: the server starts and lists tools regardless, and every
    # tool call gets a clear ToolError instead of the whole process crashing.
    try:
        if _try_load_sqlite_vec(conn):
            ctx.vector_backend = "sqlite_vec"
            # Reuse whatever `vectordrive index` already persisted to disk —
            # no upsert/build call here at all (requirement 6).
            ctx.vector_index = _ReadOnlySqliteVecIndex(conn, dimensions=embed_config.dimensions)
        else:
            ctx.vector_backend = "numpy"
            # Built exactly once, here, from the already-cached chunks/embeddings
            # tables — build_vector_index() only SELECTs from conn and mutates
            # the in-memory NumpyVectorIndex; it issues no INSERT/UPDATE/DELETE
            # against the database (requirement 7).
            numpy_index = NumpyVectorIndex()
            build_vector_index(
                conn, numpy_index, embed_provider.NAME, embed_config.model,
                embed_config.dimensions, embed_config.config_hash(),
            )
            ctx.vector_index = numpy_index
            ctx.vector_index_build_count = 1
    except sqlite3.DatabaseError as exc:
        return _degraded_context(f"The VectorDrive database at {db_path} is corrupt: {exc}")

    return ctx
