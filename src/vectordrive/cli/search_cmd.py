"""`vectordrive search "<query>" [--top-k N] [--mode hybrid|fts|vector]`:
wires the M8 search layer (keyword_search/vector_search/hybrid_search) to
real config/providers. Contains no search logic of its own (requirement
3) — only mode dispatch and presentation.

Requirement 7: FTS mode never constructs OllamaEmbeddingProvider at all
(no query embedding needed for keyword search), so it can never reach
Ollama. Vector/hybrid modes create exactly one embedding — the query's —
via embed_provider.embed_query() (M10.5: applies the Qwen retrieval
instruction, unlike document embedding); build_vector_index() (called
before searching) only reads the already-cached `chunks`/`embeddings`
tables and never calls embed_provider, so stored chunks are never
re-embedded at search time either.
"""
from __future__ import annotations

import sqlite3

from vectordrive.cli.format import format_table, truncate_excerpt
from vectordrive.config import get_db_path, load_config
from vectordrive.db.connection import connect
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.search.hybrid import hybrid_search, keyword_search, vector_search
from vectordrive.services.engines import build_search_stack
from vectordrive.services.errors import IndexIncomplete
from vectordrive.services.processing_run_service import require_completed_index

_VALID_MODES = {"hybrid", "fts", "vector"}


def _print_results(results) -> None:
    if not results:
        print("No results found.")
        return
    print(
        format_table(
            ["Path", "Page", "Chunk", "Type", "Score", "Excerpt"],
            [
                [
                    r.path,
                    str(r.page_number),
                    str(r.chunk_index),
                    r.source_type,
                    f"{r.score:.6g}",
                    truncate_excerpt(r.excerpt),
                ]
                for r in results
            ],
        )
    )


def run_search_command(query: str, top_k: int = 10, mode: str = "hybrid") -> int:
    if mode not in _VALID_MODES:
        print(f"error: invalid mode '{mode}' (choose from: {', '.join(sorted(_VALID_MODES))})")
        return 2

    if not query or not query.strip():
        print("error: query must not be empty")
        return 2

    db_path = get_db_path()
    if not db_path.exists():
        print(f"error: no index found at {db_path}. Run `vectordrive index --path <folder>` first.")
        return 1

    config = load_config()
    try:
        db = connect(db_path)
    except sqlite3.DatabaseError as exc:
        print(f"error: the database at {db_path} appears corrupt: {exc}")
        return 1

    try:
        require_completed_index(db.conn)
    except IndexIncomplete as exc:
        print(f"error: {exc} {exc.hint}")
        return 1

    if mode == "fts":
        results = keyword_search(db.conn, query, top_k)
        _print_results(results)
        return 0

    # v0.2 G1: engine construction moved to services/engines.py (shared
    # with the GUI's search tester, G7). readonly=False matches v0.1's
    # existing behavior exactly: refresh from the cached chunks/embeddings
    # tables on every CLI invocation (necessary for the in-memory
    # NumpyVectorIndex backend, a cheap no-op-ish refresh for the
    # persistent SqliteVecIndex backend — see the M10 report for the
    # O(n)-per-search tradeoff this implies). OllamaEmbeddingProvider is
    # still passed through explicitly so
    # tests/unit/test_cli_search.py's `monkeypatch.setattr(search_cmd,
    # "OllamaEmbeddingProvider", ...)` keeps taking effect exactly as
    # before — and, critically, is never constructed at all on the `fts`
    # branch above (requirement 7: FTS must never reach Ollama).
    stack = build_search_stack(
        db.conn, config, db.vector_backend, readonly=False, embed_provider_cls=OllamaEmbeddingProvider
    )
    embed_result = stack.embed_provider.embed_query(query, stack.embed_config)
    if not embed_result.success:
        print(f"error: could not create a query embedding via Ollama: {embed_result.error_message}")
        return 1

    if mode == "vector":
        results = vector_search(db.conn, stack.vector_index, stack.model_key, embed_result.vector, top_k)
    else:
        results = hybrid_search(db.conn, stack.vector_index, stack.model_key, query, embed_result.vector, top_k)

    _print_results(results)
    return 0
