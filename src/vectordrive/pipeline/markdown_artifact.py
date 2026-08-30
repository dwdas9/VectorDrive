"""Canonical Markdown-derived document artifact.

Defines the single internal representation that every extractor/OCR
output converges to before chunking, embedding, or indexing: a
structured Markdown document carrying compact JSON frontmatter plus
per-page machine-readable HTML-comment markers. See
``docs/adr/0001-canonical-markdown-derived-artifacts.md`` for the
design rationale.

This module is standard-library only. It never writes to the
filesystem and never logs or prints extracted text.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Union

SCHEMA_VERSION = 1
ARTIFACT_TYPE = "vectordrive-derived-document"

_SIDECAR_SUFFIX = ".vectordrive.md"

_ALLOWED_MODES = frozenset({"native", "ocr", "merged", "failed", "skipped"})
_OCR_MODES = frozenset({"ocr", "merged"})

_FRONTMATTER_DELIM = "---"
_PAGE_START_TAG = "<!-- vectordrive:page-start "
_PAGE_END_TAG = "<!-- vectordrive:page-end "
_COMMENT_CLOSE = " -->"

JsonScalar = Union[str, int, float, bool, None]


class MarkdownArtifactError(ValueError):
    """Raised when a document cannot be rendered or parsed safely."""


def _reject_json_constant(token: str) -> None:
    raise MarkdownArtifactError(f"non-standard JSON constant {token!r} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[object, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise MarkdownArtifactError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _strict_json_loads(raw: str) -> object:
    """The single strict JSON loader used for frontmatter and both markers.

    Rejects non-standard NaN/Infinity/-Infinity tokens (even in ignored
    fields) and rejects duplicate object keys, so signed/provenance fields
    cannot be ambiguous.
    """
    try:
        return json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise MarkdownArtifactError(f"not valid JSON: {exc}") from exc


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MarkdownArtifactError(f"{name} must be nonblank")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarkdownArtifactError(f"{name} must be an integer")
    return value


def _require_optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarkdownArtifactError(f"{name} must be a string or None")
    return value


@dataclass(frozen=True)
class SourceProvenance:
    filename: str
    relative_path: str
    sha256: str
    media_type: str
    byte_size: int
    page_count: int

    def __post_init__(self) -> None:
        _require_str(self.filename, "source.filename")
        _require_str(self.relative_path, "source.relative_path")
        if not isinstance(self.sha256, str) or not _is_sha256_hex(self.sha256):
            raise MarkdownArtifactError(
                "source.sha256 must be exactly 64 lowercase hex characters"
            )
        _require_str(self.media_type, "source.media_type")
        _require_int(self.byte_size, "source.byte_size")
        _require_int(self.page_count, "source.page_count")
        if self.byte_size < 0:
            raise MarkdownArtifactError("source.byte_size must be nonnegative")
        if self.page_count < 0:
            raise MarkdownArtifactError("source.page_count must be nonnegative")


@dataclass(frozen=True)
class GenerationProvenance:
    pipeline_version: str
    generated_at: datetime
    extractor_name: str
    extractor_version: str

    def __post_init__(self) -> None:
        _require_str(self.pipeline_version, "generation.pipeline_version")
        _require_str(self.extractor_name, "generation.extractor_name")
        _require_str(self.extractor_version, "generation.extractor_version")
        if not isinstance(self.generated_at, datetime):
            raise MarkdownArtifactError("generation.generated_at must be a datetime")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise MarkdownArtifactError("generation.generated_at must be timezone-aware")


@dataclass(frozen=True)
class PageContent:
    page_number: int
    mode: str
    text: str
    ocr_engine: str | None = None
    ocr_model: str | None = None
    ocr_language: str | None = None
    mean_confidence: float | None = None
    structural_tags: tuple[str, ...] = ()
    metadata: Mapping[str, JsonScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_int(self.page_number, "page_number")
        if self.page_number < 1:
            raise MarkdownArtifactError("page_number must be a positive 1-based integer")
        if not isinstance(self.mode, str) or self.mode not in _ALLOWED_MODES:
            raise MarkdownArtifactError(
                f"page {self.page_number}: mode {self.mode!r} is not one of {sorted(_ALLOWED_MODES)}"
            )
        if not isinstance(self.text, str):
            raise MarkdownArtifactError(f"page {self.page_number}: text must be a string")

        object.__setattr__(self, "ocr_engine", _require_optional_str(self.ocr_engine, "ocr_engine"))
        object.__setattr__(self, "ocr_model", _require_optional_str(self.ocr_model, "ocr_model"))
        object.__setattr__(self, "ocr_language", _require_optional_str(self.ocr_language, "ocr_language"))

        if self.mode in _OCR_MODES:
            for attr_name in ("ocr_engine", "ocr_model", "ocr_language"):
                value = getattr(self, attr_name)
                if not value or not value.strip():
                    raise MarkdownArtifactError(
                        f"page {self.page_number}: mode {self.mode!r} requires nonblank {attr_name}"
                    )

        if self.mean_confidence is not None:
            if isinstance(self.mean_confidence, bool) or not isinstance(
                self.mean_confidence, (int, float)
            ):
                raise MarkdownArtifactError(
                    f"page {self.page_number}: mean_confidence must be a number or None"
                )
            if not math.isfinite(self.mean_confidence):
                raise MarkdownArtifactError(
                    f"page {self.page_number}: mean_confidence must be finite"
                )
            if not (0.0 <= self.mean_confidence <= 1.0):
                raise MarkdownArtifactError(
                    f"page {self.page_number}: mean_confidence must be within [0, 1]"
                )

        if not isinstance(self.structural_tags, (tuple, list)) or isinstance(
            self.structural_tags, (str, bytes)
        ):
            raise MarkdownArtifactError(
                f"page {self.page_number}: structural_tags must be a tuple or list"
            )
        object.__setattr__(self, "structural_tags", tuple(self.structural_tags))
        for tag in self.structural_tags:
            if not isinstance(tag, str):
                raise MarkdownArtifactError(
                    f"page {self.page_number}: structural_tags must be strings"
                )

        if not isinstance(self.metadata, Mapping) or isinstance(self.metadata, (list, tuple)):
            raise MarkdownArtifactError(f"page {self.page_number}: metadata must be a mapping")
        frozen_metadata: dict[str, JsonScalar] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str):
                raise MarkdownArtifactError(f"page {self.page_number}: metadata keys must be strings")
            if value is None or isinstance(value, (str, bool)):
                frozen_metadata[key] = value
            elif isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    raise MarkdownArtifactError(
                        f"page {self.page_number}: metadata values must be finite numbers"
                    )
                frozen_metadata[key] = value
            else:
                raise MarkdownArtifactError(
                    f"page {self.page_number}: metadata values must be JSON-safe scalars"
                )
        object.__setattr__(self, "metadata", MappingProxyType(frozen_metadata))


# --- lifecycle -------------------------------------------------------------
#
# Phase 1 (source -> durable adjacent *.vectordrive.md) and Phase 2
# (validated Markdown -> chunks/index) are governed by two independent
# axes, not one: whether a source may ever be re-extracted/re-OCR'd
# (extraction_policy) is a separate question from whether an already-valid
# artifact needs re-chunking/re-embedding for an index-format migration
# (index_policy). A user who has manually OCR'd a frozen source (e.g. a
# passport) and marked its sidecar `final` is saying "never touch my
# extraction again" -- not "never index this document again". See
# docs/adr/0001-canonical-markdown-derived-artifacts.md.
#
# Only the two combinations below are valid for now: `generated` artifacts
# are routinely re-extractable (`if_source_changed`); `final` artifacts
# never are (`never`). `index_policy` currently has exactly one value
# (`on_content_change`) for both -- a content-based skip, not a ban: it
# means routine indexing skips redundant work, not that a future
# chunker/embedding migration can never touch this content again.

_VALID_CONTENT_STATUS = frozenset({"generated", "final"})
_VALID_EXTRACTION_POLICY = frozenset({"if_source_changed", "never"})
_VALID_INDEX_POLICY = frozenset({"on_content_change"})

# The only accepted (content_status -> extraction_policy) pairings.
_LIFECYCLE_EXTRACTION_POLICY_FOR_STATUS = {
    "generated": "if_source_changed",
    "final": "never",
}


@dataclass(frozen=True)
class ArtifactLifecycle:
    content_status: str
    extraction_policy: str
    index_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.content_status, str) or self.content_status not in _VALID_CONTENT_STATUS:
            raise MarkdownArtifactError(
                f"lifecycle.content_status must be one of {sorted(_VALID_CONTENT_STATUS)}, "
                f"got {self.content_status!r}"
            )
        if (
            not isinstance(self.extraction_policy, str)
            or self.extraction_policy not in _VALID_EXTRACTION_POLICY
        ):
            raise MarkdownArtifactError(
                f"lifecycle.extraction_policy must be one of {sorted(_VALID_EXTRACTION_POLICY)}, "
                f"got {self.extraction_policy!r}"
            )
        if not isinstance(self.index_policy, str) or self.index_policy not in _VALID_INDEX_POLICY:
            raise MarkdownArtifactError(
                f"lifecycle.index_policy must be one of {sorted(_VALID_INDEX_POLICY)}, "
                f"got {self.index_policy!r}"
            )
        expected_extraction_policy = _LIFECYCLE_EXTRACTION_POLICY_FOR_STATUS[self.content_status]
        if self.extraction_policy != expected_extraction_policy:
            raise MarkdownArtifactError(
                f"lifecycle.content_status {self.content_status!r} must pair with "
                f"extraction_policy {expected_extraction_policy!r}, got {self.extraction_policy!r}"
            )

    @property
    def regeneration_locked(self) -> bool:
        """Read-only: is routine extraction/OCR forbidden from overwriting
        this artifact? Derived solely from the recorded policy field --
        never inferred from a filename or the document's own content.
        """
        return self.extraction_policy == "never"


#: Default lifecycle for a routinely generated artifact. Shared as a single
#: immutable instance across documents -- safe because ArtifactLifecycle is
#: a frozen dataclass with no mutable state.
DEFAULT_LIFECYCLE = ArtifactLifecycle(
    content_status="generated",
    extraction_policy="if_source_changed",
    index_policy="on_content_change",
)

#: Lifecycle for a manually finalized artifact (e.g. a hand-OCR'd passport)
#: whose extraction must never be regenerated by routine code.
FINAL_LIFECYCLE = ArtifactLifecycle(
    content_status="final",
    extraction_policy="never",
    index_policy="on_content_change",
)


def _default_lifecycle() -> ArtifactLifecycle:
    return DEFAULT_LIFECYCLE


@dataclass(frozen=True)
class CanonicalDocument:
    source: SourceProvenance
    generation: GenerationProvenance
    pages: tuple[PageContent, ...]
    lifecycle: ArtifactLifecycle = field(default_factory=_default_lifecycle)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceProvenance):
            raise MarkdownArtifactError("source must be a SourceProvenance instance")
        if not isinstance(self.generation, GenerationProvenance):
            raise MarkdownArtifactError("generation must be a GenerationProvenance instance")
        if not isinstance(self.pages, tuple):
            raise MarkdownArtifactError("pages must be a tuple")
        for page in self.pages:
            if not isinstance(page, PageContent):
                raise MarkdownArtifactError("every page must be a PageContent instance")
        if not isinstance(self.lifecycle, ArtifactLifecycle):
            raise MarkdownArtifactError("lifecycle must be an ArtifactLifecycle instance")
        _validate_page_accounting(self.pages, self.source.page_count)


def _is_sha256_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value or ""))


def _validate_page_accounting(pages: tuple[PageContent, ...], expected_count: int) -> None:
    numbers = [page.page_number for page in pages]
    if len(numbers) != len(set(numbers)):
        raise MarkdownArtifactError("page numbers must be unique")
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise MarkdownArtifactError("page numbers must be contiguous starting at 1")
    if len(numbers) != expected_count:
        raise MarkdownArtifactError(
            f"page_count mismatch: source declares {expected_count}, got {len(numbers)} pages"
        )


# --- sidecar naming -------------------------------------------------------


def sidecar_name_for(source_name: str) -> str:
    """Return the sidecar filename for a source filename, preserving it whole."""
    _require_str(source_name, "source_name")
    return f"{source_name}{_SIDECAR_SUFFIX}"


def sidecar_path_for(path: Path) -> Path:
    """Return the sidecar path for a source path, preserving the filename whole."""
    if not isinstance(path, Path):
        raise MarkdownArtifactError("path must be a pathlib.Path")
    return path.with_name(sidecar_name_for(path.name))


def is_vectordrive_markdown_name(path_or_name: str | Path) -> bool:
    """Suffix-only check: does this name look like a VectorDrive sidecar?

    Never raises; returns False for any malformed input.
    """
    try:
        name = Path(path_or_name).name
    except Exception:
        return False
    return name.endswith(_SIDECAR_SUFFIX) and len(name) > len(_SIDECAR_SUFFIX)


def is_vectordrive_markdown(path_or_name: str | Path, rendered_text: str) -> bool:
    """Content-aware check: valid suffix AND parseable VectorDrive artifact content.

    Never raises; returns False for any malformed input.
    """
    if not is_vectordrive_markdown_name(path_or_name):
        return False
    try:
        parse_markdown(rendered_text)
    except Exception:
        return False
    return True


# --- searchable text -------------------------------------------------------

_PAGE_SEPARATOR = "\n\n"


def searchable_text(document: CanonicalDocument) -> str:
    """Canonical concatenation of only page text, excluding metadata/markers.

    Pages are joined by a deterministic double-newline separator, in page
    order. Empty page text contributes an empty segment (no content is
    invented).
    """
    return _PAGE_SEPARATOR.join(page.text for page in document.pages)


# --- rendering ---------------------------------------------------------


def _canonical_json(obj: object) -> str:
    try:
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except ValueError as exc:
        raise MarkdownArtifactError(f"value is not JSON-safe: {exc}") from exc


def _comment_safe_json(obj: object) -> str:
    """Canonical JSON, guaranteed not to contain a literal '--' run.

    Marker JSON is embedded inside an HTML comment (`<!-- ... -->`), so any
    raw `--` (and in particular `-->`) inside a string value would
    prematurely close the comment. `\\u002d` is a JSON-valid escape for `-`,
    so re-escaping every run of consecutive hyphens keeps the JSON
    semantically identical (decodes back to the exact original string)
    while making the serialized form comment-safe. Our domain never emits
    bare, unquoted hyphens (no negative numbers appear in page markers), so
    every `-` here is always inside a quoted JSON string.
    """
    raw = _canonical_json(obj)
    while "--" in raw:
        raw = raw.replace("--", "-\\u002d")
    return raw


def _source_to_dict(source: SourceProvenance) -> dict:
    return {
        "filename": source.filename,
        "relative_path": source.relative_path,
        "sha256": source.sha256,
        "media_type": source.media_type,
        "byte_size": source.byte_size,
        "page_count": source.page_count,
    }


def _generation_to_dict(generation: GenerationProvenance) -> dict:
    return {
        "pipeline_version": generation.pipeline_version,
        "generated_at": generation.generated_at.isoformat(),
        "extractor_name": generation.extractor_name,
        "extractor_version": generation.extractor_version,
    }


def _lifecycle_to_dict(lifecycle: ArtifactLifecycle) -> dict:
    return {
        "content_status": lifecycle.content_status,
        "extraction_policy": lifecycle.extraction_policy,
        "index_policy": lifecycle.index_policy,
    }


def _page_marker_dict(page: PageContent) -> dict:
    return {
        "page_number": page.page_number,
        "mode": page.mode,
        "ocr_engine": page.ocr_engine,
        "ocr_model": page.ocr_model,
        "ocr_language": page.ocr_language,
        "mean_confidence": page.mean_confidence,
        "structural_tags": list(page.structural_tags),
        "metadata": dict(page.metadata),
        "text_length": len(page.text),
    }


def render_markdown(document: CanonicalDocument) -> str:
    """Render a CanonicalDocument to its deterministic UTF-8 Markdown form."""
    body_sha256 = hashlib.sha256(searchable_text(document).encode("utf-8")).hexdigest()

    frontmatter = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "source": _source_to_dict(document.source),
        "generation": _generation_to_dict(document.generation),
        "lifecycle": _lifecycle_to_dict(document.lifecycle),
        "page_count": len(document.pages),
        "body_sha256": body_sha256,
    }

    out = [_FRONTMATTER_DELIM, "\n", _canonical_json(frontmatter), "\n", _FRONTMATTER_DELIM, "\n"]

    for page in document.pages:
        marker = _comment_safe_json(_page_marker_dict(page))
        out.append("\n")
        out.append(f"{_PAGE_START_TAG}{marker}{_COMMENT_CLOSE}")
        out.append("\n")
        out.append(page.text)
        out.append("\n")
        out.append(f"{_PAGE_END_TAG}{marker}{_COMMENT_CLOSE}")
        out.append("\n")

    return "".join(out)


# --- parsing -------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MarkdownArtifactError(message)


def parse_markdown(text: str) -> CanonicalDocument:
    """Parse rendered Markdown back into a validated CanonicalDocument."""
    if not isinstance(text, str) or not text:
        raise MarkdownArtifactError("input text must be a nonempty string")

    try:
        return _parse_markdown_unsafe(text)
    except MarkdownArtifactError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert any malformed-input error
        raise MarkdownArtifactError(f"malformed artifact: {exc}") from exc


def _parse_markdown_unsafe(text: str) -> CanonicalDocument:
    pos = 0

    def expect(literal: str) -> None:
        nonlocal pos
        _require(text.startswith(literal, pos), f"expected {literal!r} at offset {pos}")
        pos += len(literal)

    expect(_FRONTMATTER_DELIM + "\n")
    nl = text.find("\n", pos)
    _require(nl != -1, "missing frontmatter closing '---'")
    frontmatter_raw = text[pos:nl]
    pos = nl + 1
    expect(_FRONTMATTER_DELIM + "\n")

    frontmatter = _strict_json_loads(frontmatter_raw)
    _require(isinstance(frontmatter, dict), "frontmatter must be a JSON object")

    _require(frontmatter.get("artifact_type") == ARTIFACT_TYPE, "unsupported or missing artifact_type")
    schema_version = frontmatter.get("schema_version")
    _require(schema_version == SCHEMA_VERSION, f"unsupported schema_version: {schema_version!r}")

    source_dict = frontmatter.get("source")
    _require(isinstance(source_dict, dict), "frontmatter.source must be an object")
    try:
        source = SourceProvenance(
            filename=source_dict["filename"],
            relative_path=source_dict["relative_path"],
            sha256=source_dict["sha256"],
            media_type=source_dict["media_type"],
            byte_size=source_dict["byte_size"],
            page_count=source_dict["page_count"],
        )
    except KeyError as exc:
        raise MarkdownArtifactError(f"frontmatter.source is malformed: {exc}") from exc

    generation_dict = frontmatter.get("generation")
    _require(isinstance(generation_dict, dict), "frontmatter.generation must be an object")
    generated_at_raw = generation_dict.get("generated_at")
    _require(isinstance(generated_at_raw, str), "frontmatter.generation.generated_at must be a string")
    try:
        generated_at = datetime.fromisoformat(generated_at_raw)
    except ValueError as exc:
        raise MarkdownArtifactError(f"frontmatter.generation.generated_at is malformed: {exc}") from exc
    try:
        generation = GenerationProvenance(
            pipeline_version=generation_dict["pipeline_version"],
            generated_at=generated_at,
            extractor_name=generation_dict["extractor_name"],
            extractor_version=generation_dict["extractor_version"],
        )
    except KeyError as exc:
        raise MarkdownArtifactError(f"frontmatter.generation is malformed: {exc}") from exc

    # Absent lifecycle means an old artifact predating this field --
    # defaults to the routinely-generated lifecycle for backward
    # compatibility (no persisted production sidecars predate this field,
    # but SCHEMA_VERSION does not need to bump for an additive default).
    # If the key is present but explicitly null (JSON null), that is an error --
    # a present lifecycle must be a complete object.
    if "lifecycle" not in frontmatter:
        lifecycle = DEFAULT_LIFECYCLE
    else:
        lifecycle_dict = frontmatter["lifecycle"]
        _require(isinstance(lifecycle_dict, dict), "frontmatter.lifecycle must be an object, not null")
        try:
            lifecycle = ArtifactLifecycle(
                content_status=lifecycle_dict["content_status"],
                extraction_policy=lifecycle_dict["extraction_policy"],
                index_policy=lifecycle_dict["index_policy"],
            )
        except KeyError as exc:
            raise MarkdownArtifactError(f"frontmatter.lifecycle is missing required field: {exc}") from exc

    declared_page_count = frontmatter.get("page_count")
    _require(
        isinstance(declared_page_count, int)
        and not isinstance(declared_page_count, bool)
        and declared_page_count >= 0,
        "frontmatter.page_count must be a nonnegative integer",
    )
    body_sha256_declared = frontmatter.get("body_sha256")
    _require(
        isinstance(body_sha256_declared, str) and _is_sha256_hex(body_sha256_declared),
        "frontmatter.body_sha256 must be 64 lowercase hex chars",
    )

    pages: list[PageContent] = []
    for _ in range(declared_page_count):
        page, pos = _parse_one_page(text, pos)
        pages.append(page)

    _require(pos == len(text), "unexpected trailing content after last page frame")

    document = CanonicalDocument(
        source=source, generation=generation, pages=tuple(pages), lifecycle=lifecycle
    )

    actual_digest = hashlib.sha256(searchable_text(document).encode("utf-8")).hexdigest()
    _require(actual_digest == body_sha256_declared, "body_sha256 does not match page text (tampered content)")

    return document


def _parse_one_page(text: str, pos: int) -> tuple[PageContent, int]:
    def expect(literal: str, at: int) -> int:
        _require(text.startswith(literal, at), f"expected {literal!r} at offset {at}")
        return at + len(literal)

    pos = expect("\n", pos)
    pos = expect(_PAGE_START_TAG, pos)
    close_idx = text.find(_COMMENT_CLOSE + "\n", pos)
    _require(close_idx != -1, "unterminated page-start marker")
    start_marker_raw = text[pos:close_idx]
    pos = close_idx + len(_COMMENT_CLOSE + "\n")

    start_marker = _strict_json_loads(start_marker_raw)
    _require(isinstance(start_marker, dict), "page-start marker must be a JSON object")

    text_length = start_marker.get("text_length")
    _require(
        isinstance(text_length, int) and not isinstance(text_length, bool) and text_length >= 0,
        "page marker text_length must be a nonnegative integer",
    )

    page_text = text[pos : pos + text_length]
    _require(len(page_text) == text_length, "page text is shorter than declared text_length")
    pos += text_length

    pos = expect("\n", pos)
    pos = expect(_PAGE_END_TAG, pos)
    close_idx2 = text.find(_COMMENT_CLOSE + "\n", pos)
    _require(close_idx2 != -1, "unterminated page-end marker")
    end_marker_raw = text[pos:close_idx2]
    pos = close_idx2 + len(_COMMENT_CLOSE + "\n")

    end_marker = _strict_json_loads(end_marker_raw)

    _require(start_marker == end_marker, "page-start and page-end markers do not match")

    raw_structural_tags = start_marker.get("structural_tags")
    if raw_structural_tags is None:
        structural_tags: object = ()
    elif isinstance(raw_structural_tags, list) and all(
        isinstance(tag, str) for tag in raw_structural_tags
    ):
        structural_tags = tuple(raw_structural_tags)
    else:
        raise MarkdownArtifactError(
            "page marker structural_tags must be a JSON array of strings"
        )

    raw_metadata = start_marker.get("metadata")
    if raw_metadata is None:
        metadata: object = {}
    elif isinstance(raw_metadata, dict):
        metadata = raw_metadata
    else:
        raise MarkdownArtifactError("page marker metadata must be a JSON object")

    try:
        page = PageContent(
            page_number=start_marker["page_number"],
            mode=start_marker["mode"],
            text=page_text,
            ocr_engine=start_marker.get("ocr_engine"),
            ocr_model=start_marker.get("ocr_model"),
            ocr_language=start_marker.get("ocr_language"),
            mean_confidence=start_marker.get("mean_confidence"),
            structural_tags=structural_tags,
            metadata=metadata,
        )
    except KeyError as exc:
        raise MarkdownArtifactError(f"page marker is missing required field: {exc}") from exc

    return page, pos

