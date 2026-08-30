"""Builds the same pipeline/search engine objects from Config that
cli/index_cmd.py, cli/search_cmd.py used to each construct independently.

Before v0.2 G1, "OllamaEmbeddingProvider + a chunk config + tier2/3/4 OCR
engines + a vector index, all built from Config" was written out three
times (cli/index_cmd.py, cli/search_cmd.py, mcp/context.py). This module
is the one place index-time and search-time engine construction happens
now, for the CLI and GUI. mcp/context.py deliberately keeps its own
bespoke construction (documented there) so
tests/unit/test_mcp_context.py's monkeypatching of its private helpers
keeps working without editing a single test file — the MCP path is
functionally equivalent but not literally routed through these functions.

Provider/engine classes are accepted as injectable parameters (defaulting
to the real implementations) specifically so cli/index_cmd.py and
cli/search_cmd.py can keep passing their own *module-level*
`OllamaEmbeddingProvider` name through — which is exactly what
tests/unit/test_cli_index.py and tests/unit/test_cli_search.py
monkeypatch (`monkeypatch.setattr(index_cmd, "OllamaEmbeddingProvider",
_StubProvider)`). Because index_cmd.py still imports the real class at
module level and passes it in explicitly, that patch continues to take
effect exactly as before.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from vectordrive.config import Config
from vectordrive.db.readonly import ReadOnlySqliteVecIndex
from vectordrive.pipeline.chunk.base import ChunkerConfig
from vectordrive.pipeline.embed.base import (
    EmbeddingConfig,
    EmbeddingProvider,
    active_corpus_embedding_config,
)
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.pipeline.ocr.base import OcrEngine
from vectordrive.pipeline.ocr.paddleocr_vl_engine import PaddleOcrVlEngine
from vectordrive.pipeline.ocr.qwen_vl_engine import QwenVlOcrEngine
from vectordrive.pipeline.ocr.rapidocr_engine import RapidOcrEngine
from vectordrive.pipeline.ocr.tesseract_engine import TesseractEngine
from vectordrive.pipeline.structure.ppstructure_engine import PPStructureEngine
from vectordrive.search.index_builder import build_vector_index
from vectordrive.search.vector_index.base import VectorIndex, compute_model_key
from vectordrive.search.vector_index.numpy_index import NumpyVectorIndex
from vectordrive.search.vector_index.sqlite_vec_index import SqliteVecIndex


@dataclass(frozen=True)
class ExtractionEngines:
    """Minimal engine set for extraction/OCR-only pipelines (Phase 1
    Markdown generation): tier2/3/4 OCR engines and optional structure
    analysis, but no embedding or vector indexing.

    Typed against the `OcrEngine` protocol rather than a concrete engine
    class: tier2 here is `QwenVlOcrEngine` (see `build_extraction_engines`),
    not `RapidOcrEngine` — a concrete-class annotation would misdescribe
    what's actually constructed.
    """

    tier2: OcrEngine
    tier3: OcrEngine | None
    tier4: OcrEngine | None
    structure_engine: PPStructureEngine | None
    structure_analyze_native: bool


@dataclass(frozen=True)
class IndexEngines:
    """Everything pipeline.indexer.index_folder() needs, built once from
    Config for a single indexing run.
    """

    tier2: RapidOcrEngine
    tier3: PaddleOcrVlEngine | None
    tier4: TesseractEngine | None
    chunk_config: ChunkerConfig
    embed_provider: EmbeddingProvider
    embed_config: EmbeddingConfig
    embed_batch_size: int
    vector_index: VectorIndex
    structure_engine: PPStructureEngine | None
    structure_analyze_native: bool


@dataclass(frozen=True)
class SidecarIndexEngines:
    """Phase 2 engines only: no extractor, OCR, or structure engine."""

    chunk_config: ChunkerConfig
    embed_provider: EmbeddingProvider
    embed_config: EmbeddingConfig
    embed_batch_size: int
    vector_index: VectorIndex


def build_sidecar_index_engines(
    conn: sqlite3.Connection,
    config: Config,
    vector_backend: str,
    *,
    embed_provider_cls: type[EmbeddingProvider] = OllamaEmbeddingProvider,
    sqlite_vec_index_cls: type = SqliteVecIndex,
    numpy_vector_index_cls: type = NumpyVectorIndex,
) -> SidecarIndexEngines:
    """Build the minimal read-sidecar -> chunk -> embed -> vector stack."""
    vector_index = (
        sqlite_vec_index_cls(conn, dimensions=config.embedding_dims)
        if vector_backend == "sqlite_vec"
        else numpy_vector_index_cls()
    )
    return SidecarIndexEngines(
        chunk_config=ChunkerConfig(),
        embed_provider=embed_provider_cls(endpoint=config.ollama_endpoint),
        embed_config=active_corpus_embedding_config(config.embedding_model, config.embedding_dims),
        embed_batch_size=config.embedding_batch_size,
        vector_index=vector_index,
    )


def build_extraction_engines(
    config: Config,
    *,
    tier2_cls: type = QwenVlOcrEngine,
    structure_cls: type = PPStructureEngine,
) -> ExtractionEngines:
    """Build the minimal engine set for extraction/OCR-only pipelines
    (Phase 1 Markdown generation). No embedding provider, chunk config, or
    vector index — those are not needed until indexing proper.

    Phase 1's primary (and only) OCR engine is Qwen3-VL via local Ollama
    (`config.ollama_endpoint`, `config.ocr.phase1_markdown_vision_model`),
    not the legacy RapidOCR/PaddleOCR-VL/Tesseract tier stack — that stack
    stays exactly as-is for `build_index_engines` below. tier3/tier4 are
    always None here: Qwen truthfully reports `mean_confidence=None`
    (it's a vision-language model, not a confidence-scored OCR engine), and
    the tiered-escalation policy (`pipeline/ocr/tiered.py`) only escalates
    past tier2 when tier3/tier4 are configured — with both None, a
    successful tier2 result is used as-is, no policy change needed (see
    `tests/unit/services/test_engines.py`'s
    `test_qwen_result_is_not_escalated_without_configured_tier3_or_tier4`).
    """
    tier2 = tier2_cls(endpoint=config.ollama_endpoint, model=config.ocr.phase1_markdown_vision_model)
    structure_engine = structure_cls() if config.structure.enabled else None
    return ExtractionEngines(
        tier2=tier2,
        tier3=None,
        tier4=None,
        structure_engine=structure_engine,
        structure_analyze_native=config.structure.analyze_native_pages,
    )


def build_index_engines(
    conn: sqlite3.Connection,
    config: Config,
    vector_backend: str,
    *,
    embed_provider_cls: type[EmbeddingProvider] = OllamaEmbeddingProvider,
    tier2_cls: type = RapidOcrEngine,
    tier3_cls: type = PaddleOcrVlEngine,
    tier4_cls: type = TesseractEngine,
    structure_cls: type = PPStructureEngine,
    sqlite_vec_index_cls: type = SqliteVecIndex,
    numpy_vector_index_cls: type = NumpyVectorIndex,
) -> IndexEngines:
    """Build the engine set for one indexing run against a writable `conn`."""
    tier3 = tier3_cls() if config.ocr.tier3_paddleocr_vl_enabled else None
    tier4 = tier4_cls() if config.ocr.tier4_tesseract_enabled else None
    structure_engine = structure_cls() if config.structure.enabled else None
    vector_index = (
        sqlite_vec_index_cls(conn, dimensions=config.embedding_dims)
        if vector_backend == "sqlite_vec"
        else numpy_vector_index_cls()
    )
    return IndexEngines(
        tier2=tier2_cls(),
        tier3=tier3,
        tier4=tier4,
        chunk_config=ChunkerConfig(),
        embed_provider=embed_provider_cls(endpoint=config.ollama_endpoint),
        # Section 3: context_recipe_version (folded into config_hash by
        # active_corpus_embedding_config) partitions the embedding
        # cache/vector-index model_key by the contextual-embedding-input
        # recipe (pipeline/embed/context.py) that canonical Markdown-aware
        # chunks are built under — never sent to Ollama (EmbeddingConfig's
        # own docstring). build_search_stack and status_service use the
        # same helper so every consumer reads the partition written here.
        embed_config=active_corpus_embedding_config(config.embedding_model, config.embedding_dims),
        embed_batch_size=config.embedding_batch_size,
        vector_index=vector_index,
        structure_engine=structure_engine,
        structure_analyze_native=config.structure.analyze_native_pages,
    )


@dataclass(frozen=True)
class SearchStack:
    """Everything search.hybrid's search functions need, built once from
    Config for a single search invocation (CLI) or reused across many
    (a future SearchService, G7).
    """

    embed_provider: EmbeddingProvider
    embed_config: EmbeddingConfig
    vector_index: VectorIndex
    model_key: str


def build_search_stack(
    conn: sqlite3.Connection,
    config: Config,
    vector_backend: str,
    *,
    readonly: bool,
    embed_provider_cls: type[EmbeddingProvider] = OllamaEmbeddingProvider,
    sqlite_vec_index_cls: type = SqliteVecIndex,
    readonly_sqlite_vec_index_cls: type = ReadOnlySqliteVecIndex,
    numpy_vector_index_cls: type = NumpyVectorIndex,
) -> SearchStack:
    """Build the search stack for vector/hybrid search.

    Never call this for FTS-only search — it always constructs an
    embedding provider, which FTS mode must structurally never do (see
    cli/search_cmd.py's module docstring). Callers own that branch.

    `readonly=True` (MCP, GUI read screens) never calls build_vector_index
    against a persistent sqlite_vec table — it reuses exactly what
    `vectordrive index` already wrote, and uses the read-only index class
    (no `CREATE VIRTUAL TABLE`). `readonly=False` (CLI, GUI indexing
    flows) refreshes from the cached chunks/embeddings tables every call,
    matching v0.1's existing cli/search_cmd.py behavior exactly — cheap
    because it only SELECTs already-cached rows, never re-embeds.
    """
    from vectordrive.services.processing_run_service import require_completed_index

    require_completed_index(conn)
    # Must match build_index_engines' embed_config exactly (same
    # context_recipe_version) so the vector index/embedding cache lookup
    # hits the partition indexing actually wrote to. Query text itself
    # never gets document context prepended — only chunk/document text
    # does (pipeline/embed/context.py); the existing Qwen query
    # instruction (format_query_with_instruction) is untouched.
    embed_config = active_corpus_embedding_config(config.embedding_model, config.embedding_dims)
    embed_provider = embed_provider_cls(endpoint=config.ollama_endpoint)
    config_hash = embed_config.config_hash()
    model_key = compute_model_key(embed_provider.NAME, embed_config.model, embed_config.dimensions, config_hash)

    if vector_backend == "sqlite_vec":
        index_cls = readonly_sqlite_vec_index_cls if readonly else sqlite_vec_index_cls
        vector_index = index_cls(conn, dimensions=config.embedding_dims)
        if not readonly:
            build_vector_index(
                conn, vector_index, embed_provider.NAME, embed_config.model, embed_config.dimensions, config_hash
            )
    else:
        vector_index = numpy_vector_index_cls()
        build_vector_index(
            conn, vector_index, embed_provider.NAME, embed_config.model, embed_config.dimensions, config_hash
        )

    return SearchStack(
        embed_provider=embed_provider, embed_config=embed_config, vector_index=vector_index, model_key=model_key
    )
