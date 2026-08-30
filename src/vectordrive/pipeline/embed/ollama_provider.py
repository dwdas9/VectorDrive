"""Ollama-backed EmbeddingProvider.

API verified live against the actually-running local Ollama (v0.32.1)
before writing this, not assumed from docs: `POST /api/embed` with
`{"model", "input", "dimensions"}` returns `{"embeddings": [[...]], ...}`.
`input` accepts either a single string or a list of strings for batched
embedding in one request — both return `embeddings` as a list, in the
same order as the input. qwen3-embedding:8b's *native* output is
4096-dimensional — passing `dimensions=1024` in the request makes Ollama
return real 1024-float vectors (Qwen3-Embedding models are trained with
Matryoshka Representation Learning, so this truncation is a supported,
intended usage, not a hack).

M10.5 requirement 1: embed_documents (batched, requirement 3) and
embed_query (single text, Qwen retrieval instruction applied, requirement
2) are separate operations, both built on the same internal _embed_batch
so the HTTP/error-handling/validation logic isn't duplicated. Document
text is sent completely unmodified — no "search_document:"/instruction
prefix (Nomic-style prefixes are a different model family's convention
and would corrupt qwen3-embedding inputs); only embed_query applies the
instruction, via format_query_with_instruction().
"""
from __future__ import annotations

from collections.abc import Callable

import httpx

from vectordrive.pipeline.embed.base import (
    EmbeddingConfig,
    EmbeddingResult,
    format_query_with_instruction,
    validate_vector_dimensions,
)

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_TIMEOUT = 30.0


class OllamaEmbeddingProvider:
    NAME = "ollama"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        request_fn: Callable[[str, dict, float], httpx.Response] | None = None,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self._request_fn = request_fn

    def _fail_batch(self, config: EmbeddingConfig, message: str, count: int) -> list[EmbeddingResult]:
        return [
            EmbeddingResult(
                provider=self.NAME, model=config.model, dimensions=config.dimensions,
                success=False, error_message=message,
            )
            for _ in range(count)
        ]

    def embed_documents(self, texts: list[str], config: EmbeddingConfig) -> list[EmbeddingResult]:
        """Batched, unmodified document/chunk text — no instruction, no
        per-chunk HTTP request (requirement 3: one request per batch).
        """
        return self._embed_batch(list(texts), config)

    def embed_query(self, text: str, config: EmbeddingConfig) -> EmbeddingResult:
        """A single query, wrapped in Qwen3-Embedding's documented
        retrieval instruction (requirement 2) — never applied to
        documents.
        """
        formatted = format_query_with_instruction(text)
        return self._embed_batch([formatted], config)[0]

    def _embed_batch(self, texts: list[str], config: EmbeddingConfig) -> list[EmbeddingResult]:
        if not texts:
            return []

        payload = {"model": config.model, "input": texts, "dimensions": config.dimensions, **config.extra}

        try:
            # Ollama is a local dependency. Never inherit HTTP(S)_PROXY for
            # embedding requests: grounded Chat sends the user's question
            # through this path, and a missing NO_PROXY must not leak it to
            # an environment-configured proxy. Redirects stay disabled so a
            # local endpoint cannot bounce document/query text elsewhere.
            url = f"{self.endpoint}/api/embed"
            if self._request_fn is not None:
                response = self._request_fn(url, payload, self.timeout)
            else:
                response = httpx.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                    trust_env=False,
                    follow_redirects=False,
                )
        except httpx.TimeoutException as exc:
            return self._fail_batch(config, f"Ollama request timed out after {self.timeout}s: {exc}", len(texts))
        except httpx.ConnectError as exc:
            return self._fail_batch(
                config,
                f"Could not connect to Ollama at {self.endpoint} (is it running?): {exc}",
                len(texts),
            )
        except Exception as exc:
            return self._fail_batch(config, f"{type(exc).__name__}: {exc}", len(texts))

        if response.status_code == 404:
            return self._fail_batch(
                config,
                f"Model '{config.model}' not found on Ollama at {self.endpoint}. "
                f"Run `ollama pull {config.model}`. Response: {response.text[:300]}",
                len(texts),
            )
        try:
            response.raise_for_status()
        except Exception as exc:
            return self._fail_batch(
                config, f"Ollama returned HTTP {response.status_code}: {response.text[:300]} ({exc})", len(texts)
            )

        try:
            data = response.json()
            vectors = data["embeddings"]
        except Exception as exc:
            return self._fail_batch(
                config, f"Malformed response from Ollama: {type(exc).__name__}: {exc}", len(texts)
            )

        # requirement 3: validate the NUMBER of returned vectors, not just
        # each one's dimensionality — a silently-short/long response would
        # otherwise misalign results with the input texts.
        if len(vectors) != len(texts):
            return self._fail_batch(
                config,
                f"Ollama returned {len(vectors)} vectors for {len(texts)} inputs (expected them to match).",
                len(texts),
            )

        results: list[EmbeddingResult] = []
        for vector in vectors:
            try:
                validate_vector_dimensions(vector, expected=config.dimensions)
            except ValueError as exc:
                results.append(
                    EmbeddingResult(
                        provider=self.NAME, model=config.model, dimensions=config.dimensions,
                        success=False, error_message=str(exc),
                    )
                )
                continue
            results.append(
                EmbeddingResult(
                    provider=self.NAME, model=config.model, dimensions=config.dimensions,
                    success=True, vector=vector,
                )
            )
        return results
