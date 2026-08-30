"""Deterministic embedding-only context prefix construction (Section 3).

`Chunk.text` (searchable, FTS-indexed, user-facing) always stays exactly
the extracted/chunked content -- no metadata is ever prepended there.
`embedding_text` is a *separate* string, used only as embedding-provider
input, carrying a deterministic prefix of trusted canonical provenance so
the embedding can disambiguate chunks that would otherwise look
textually identical (e.g. the same boilerplate paragraph recurring in
many files/folders).

Deferred, deliberately: true late chunking (per-token contextual
embeddings derived from a single long-document forward pass) is NOT
implemented here. Ollama's `/api/embed` returns one pooled vector per
input and exposes no token-level hidden states, so there is no way to
compute genuine late chunking against it today. What this module
provides is Markdown-aware early/eager chunking (see
pipeline/chunk/markdown_chunker.py) plus a prepended context string for
embedding input only -- a documented, bounded substitute, never
represented as late chunking. See docs/architecture.md and
docs/adr/0002-embedding-context-vs-late-chunking.md.

No date field is included: the canonical schema (SourceProvenance,
GenerationProvenance) has no reliable explicit *document* date --
`generation.generated_at` is pipeline processing time, not the source
document's own date. Inventing one from body text would be a
hallucination risk this module deliberately avoids.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

# Bumping this partitions the embedding cache and vector index from any
# prior recipe (folded into EmbeddingConfig.context_recipe_version, which
# is part of EmbeddingConfig.config_hash() -- see pipeline/embed/base.py).
CONTEXT_RECIPE_VERSION = "1"


def folder_from_relative_path(relative_path: str) -> str:
    """Root-relative folder only: the parent of `relative_path`, never an
    absolute machine path. Returns "" for a file directly under the
    indexed root (no parent folder to report), a blank/None input, or a
    malformed absolute path (POSIX `/...` or Windows `C:\\...`) -- callers
    must never pass an absolute path here, but this function still refuses
    to ever surface one rather than trust the caller.
    """
    if not relative_path:
        return ""
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return ""
    parent = PurePosixPath(normalized).parent
    parent_str = str(parent)
    return "" if parent_str in (".", "") else parent_str


def build_embedding_text(
    chunk_text: str,
    *,
    filename: str,
    folder: str,
    media_type: str,
    page_number: int,
    title: str | None = None,
    section: str | None = None,
    recipe_version: str = CONTEXT_RECIPE_VERSION,
) -> str:
    """Deterministic context prefix + chunk text, for embedding input
    only -- never written into `Chunk.text` or indexed by FTS.

    Only trusted canonical provenance is used: `filename` and `folder`
    come from `SourceProvenance` (folder via `folder_from_relative_path`,
    already root-relative -- callers must never pass an absolute path
    here), `title` only when derived from a real ATX heading (None
    otherwise -- never invented), `media_type` and `page_number` from the
    canonical document, and `section` from the chunk's own heading
    metadata when present. No document date is ever added (see module
    docstring).
    """
    lines = [f"Recipe: {recipe_version}", f"File: {filename}"]
    if folder:
        lines.append(f"Folder: {folder}")
    if title:
        lines.append(f"Title: {title}")
    lines.append(f"Type: {media_type}")
    lines.append(f"Page: {page_number}")
    if section:
        lines.append(f"Section: {section}")
    header = "\n".join(lines)
    return f"{header}\n\n{chunk_text}"


def compute_embedding_input_hash(embedding_text: str) -> str:
    return hashlib.sha256(embedding_text.encode("utf-8")).hexdigest()
