"""Shared types for the embedding stage: replaceable EmbeddingProvider
interface, EmbeddingConfig (part of the cache key), EmbeddingResult.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_DIMENSIONS = 1024
DEFAULT_EMBED_BATCH_SIZE = 16

# Qwen3-Embedding's documented usage for asymmetric retrieval: apply an
# "Instruct: ...\nQuery: ..." wrapper to the QUERY side only, never to
# document/passage text (M10.5 requirement 2). This is the model's own
# recommended instruct-tuning format for retrieval, not a VectorDrive
# convention — hence "documented" in the milestone spec.
DEFAULT_QUERY_INSTRUCTION = "Given a search query, retrieve relevant passages that answer the query"


@dataclass(frozen=True)
class EmbeddingConfig:
    """model + dimensions + extra form part of the embedding cache key
    alongside the provider's own NAME and the chunk's content hash — any
    change here is a deliberate cache-busting change (requirement 9:
    rebuilds only embeddings, never extraction/OCR/chunking).
    """

    model: str = DEFAULT_MODEL
    dimensions: int = DEFAULT_DIMENSIONS
    extra: dict[str, Any] = field(default_factory=dict)
    # Section 3: partitions the embedding cache/vector-index model_key by
    # which contextual-embedding-input recipe (pipeline/embed/context.py's
    # CONTEXT_RECIPE_VERSION) produced the chunks being embedded — a
    # dedicated, internal field, NEVER sent to the provider (ollama_
    # provider.py's request payload is built only from model/dimensions/
    # extra; this field is deliberately excluded). None (the default)
    # means "no contextual recipe" (legacy/plain chunk text), matching
    # every existing caller unchanged; setting it busts the cache/index
    # exactly once when the recipe changes, without touching OCR/chunks.
    context_recipe_version: str | None = None

    def config_hash(self) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "dimensions": self.dimensions,
                "extra": self.extra,
                "context_recipe_version": self.context_recipe_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def active_corpus_embedding_config(model: str, dimensions: int) -> EmbeddingConfig:
    """The exact embedding identity indexing and search use for the live
    corpus — see services/engines.py (build_index_engines /
    build_sidecar_index_engines / build_search_stack) and mcp/context.py,
    which all construct this same triple. Any consumer that must match rows
    already written to the embeddings cache / vector index (e.g.
    status_service's active-embedding count) must derive config_hash from
    this, never an approximation that omits context_recipe_version.
    """
    from vectordrive.pipeline.embed.context import CONTEXT_RECIPE_VERSION

    return EmbeddingConfig(
        model=model,
        dimensions=dimensions,
        context_recipe_version=CONTEXT_RECIPE_VERSION,
    )


@dataclass
class EmbeddingResult:
    provider: str
    model: str
    dimensions: int
    success: bool = True
    vector: list[float] | None = None
    error_message: str | None = None


class EmbeddingProvider(Protocol):
    """Any embedding backend (Ollama, ...) implements this. NAME forms
    part of the cache key alongside the chunk's content hash and the
    EmbeddingConfig.

    Document and query embedding are separate operations (M10.5
    requirement 1) because they are NOT symmetric: embed_query applies the
    Qwen retrieval instruction (requirement 2), embed_documents never does,
    and embed_documents is batched (requirement 3) while a query is always
    a single text.
    """

    NAME: str

    def embed_documents(self, texts: list[str], config: EmbeddingConfig) -> list[EmbeddingResult]: ...
    def embed_query(self, text: str, config: EmbeddingConfig) -> EmbeddingResult: ...


def format_query_with_instruction(query: str, instruction: str = DEFAULT_QUERY_INSTRUCTION) -> str:
    """Qwen3-Embedding's documented instruct format for the query side of
    asymmetric retrieval. Applied only by embed_query — never to document
    chunk text (requirement 2).
    """
    return f"Instruct: {instruction}\nQuery: {query}"


def validate_vector_dimensions(vector: list[float], expected: int) -> None:
    """Raise ValueError unless `vector` has exactly `expected` dimensions
    (requirement 10). Used both by providers (before returning success) and
    by the cache layer (before persisting) — defense in depth.
    """
    if len(vector) != expected:
        raise ValueError(
            f"Expected a {expected}-dimensional vector, got {len(vector)}."
        )
