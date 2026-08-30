"""Atomic sidecar persistence for canonical Markdown documents.

Write *.vectordrive.md sidecars adjacent to source files, with strict
source-hash validation, lifecycle preservation, and regeneration locking.
Never logs or includes extracted text; only safe structured values.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vectordrive.pipeline.markdown_artifact import (
    ArtifactLifecycle,
    CanonicalDocument,
    MarkdownArtifactError,
    parse_markdown,
    render_markdown,
    searchable_text,
    sidecar_path_for,
)
from vectordrive.services.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


class SidecarPersistenceError(RuntimeError):
    """Raised when a sidecar cannot be safely written or read.

    Message is always safe for logging: never includes extracted body text.
    """

    pass


@dataclass(frozen=True)
class SidecarPersistenceResult:
    """Result of a sidecar persistence operation."""

    source_path: Path
    sidecar_path: Path
    outcome: Literal["created", "updated", "skipped_unchanged", "skipped_final"]
    lifecycle: ArtifactLifecycle
    body_sha256: str


@dataclass(frozen=True)
class SidecarPreflightResult:
    """Result of a preflight sidecar check before extraction.

    Returned by preflight_sidecar() to inform extraction callers
    whether to proceed (extract) or skip (skip_final) based on existing
    sidecar state.
    """

    source_path: Path
    sidecar_path: Path
    decision: Literal["extract", "skip_final"]
    source_sha256: str
    lifecycle: ArtifactLifecycle | None = None  # Only when sidecar present and valid
    body_sha256: str | None = None  # Only when sidecar present and valid


def persist_sidecar(source_path: Path, document: CanonicalDocument) -> SidecarPersistenceResult:
    """Atomically persist a CanonicalDocument to its adjacent sidecar file.

    Args:
        source_path: Path to the source file (must exist and be regular).
        document: CanonicalDocument to persist.

    Returns:
        SidecarPersistenceResult describing the operation outcome.

    Raises:
        SidecarPersistenceError: If validation fails or sidecar is locked/tampered.
    """
    source_path = Path(source_path).resolve()

    # --- Validation phase (before any I/O) ---

    # Reject if source name is itself a sidecar name
    if source_path.name.endswith(".vectordrive.md"):
        raise SidecarPersistenceError(
            f"Cannot persist sidecar for {source_path.name}: "
            "source filename cannot itself be a vectordrive sidecar"
        )

    # Require existing regular source file
    try:
        if not source_path.exists():
            raise SidecarPersistenceError(
                f"Source file does not exist: {source_path.name}"
            )
        if not source_path.is_file():
            raise SidecarPersistenceError(
                f"Source path is not a regular file: {source_path.name}"
            )
    except OSError as exc:
        raise SidecarPersistenceError(f"Cannot access source file: {exc}") from exc

    # Require filename match
    if document.source.filename != source_path.name:
        raise SidecarPersistenceError(
            f"Document source filename mismatch: "
            f"document.source.filename={document.source.filename!r} "
            f"but source_path.name={source_path.name!r}"
        )

    # Stream-hash source bytes and validate against document
    try:
        source_sha256 = _hash_file_sha256(source_path)
    except OSError as exc:
        raise SidecarPersistenceError(f"Cannot read source file for hashing: {exc}") from exc

    if source_sha256 != document.source.sha256:
        raise SidecarPersistenceError(
            f"Source file hash mismatch: "
            f"computed {source_sha256[:8]}… "
            f"but document.source.sha256={document.source.sha256[:8]}…"
        )

    # --- Compute body digest for incoming document ---
    incoming_body_sha256 = hashlib.sha256(
        searchable_text(document).encode("utf-8")
    ).hexdigest()

    # --- Render the document to Markdown text ---
    try:
        rendered_text = render_markdown(document)
    except MarkdownArtifactError as exc:
        raise SidecarPersistenceError(
            f"Cannot render document to Markdown: {exc}"
        ) from exc

    sidecar_path = sidecar_path_for(source_path)

    # --- Handle existing sidecar ---
    if sidecar_path.exists():
        try:
            existing_bytes = sidecar_path.read_bytes()
            existing_text = existing_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Sidecar is unreadable or invalid UTF-8: preserve it and raise
            raise SidecarPersistenceError(
                f"Existing sidecar is invalid or unreadable: {exc}"
            ) from exc

        # Try to parse the existing sidecar
        try:
            existing_doc = parse_markdown(existing_text)
        except MarkdownArtifactError as exc:
            # Sidecar is malformed/tampered: preserve it and raise
            raise SidecarPersistenceError(
                f"Existing sidecar is malformed or tampered: {exc}"
            ) from exc

        # Verify source binding before honoring existing sidecar lifecycle
        if existing_doc.source.filename != source_path.name:
            raise SidecarPersistenceError(
                f"Existing sidecar source filename mismatch: "
                f"sidecar claims {existing_doc.source.filename!r} "
                f"but actual source is {source_path.name!r}"
            )

        if existing_doc.source.sha256 != source_sha256:
            raise SidecarPersistenceError(
                f"Existing sidecar source hash mismatch: "
                f"sidecar claims {existing_doc.source.sha256[:8]}… "
                f"but computed source hash is {source_sha256[:8]}…"
            )

        existing_body_sha256 = hashlib.sha256(
            searchable_text(existing_doc).encode("utf-8")
        ).hexdigest()

        # Check regeneration_locked before anything else
        if existing_doc.lifecycle.regeneration_locked:
            logger.info(
                "sidecar_skipped_final",
                extra={
                    "source_path": str(source_path),
                    "sidecar_path": str(sidecar_path),
                    "outcome": "skipped_final",
                    "source_sha256": source_sha256,
                    "body_sha256": existing_body_sha256,
                    "content_status": existing_doc.lifecycle.content_status,
                    "extraction_policy": existing_doc.lifecycle.extraction_policy,
                    "index_policy": existing_doc.lifecycle.index_policy,
                },
            )
            return SidecarPersistenceResult(
                source_path=source_path,
                sidecar_path=sidecar_path,
                outcome="skipped_final",
                lifecycle=existing_doc.lifecycle,
                body_sha256=existing_body_sha256,
            )

        # Check if content is unchanged (byte-identical UTF-8)
        rendered_bytes = rendered_text.encode("utf-8")
        if rendered_bytes == existing_bytes:
            # Never touch mtime on unchanged file
            logger.info(
                "sidecar_skipped_unchanged",
                extra={
                    "source_path": str(source_path),
                    "sidecar_path": str(sidecar_path),
                    "outcome": "skipped_unchanged",
                    "source_sha256": source_sha256,
                    "body_sha256": incoming_body_sha256,
                    "content_status": existing_doc.lifecycle.content_status,
                    "extraction_policy": existing_doc.lifecycle.extraction_policy,
                    "index_policy": existing_doc.lifecycle.index_policy,
                },
            )
            return SidecarPersistenceResult(
                source_path=source_path,
                sidecar_path=sidecar_path,
                outcome="skipped_unchanged",
                lifecycle=existing_doc.lifecycle,
                body_sha256=incoming_body_sha256,
            )

        # Content differs: atomically replace
        try:
            atomic_write_text(sidecar_path, rendered_text)
        except OSError as exc:
            raise SidecarPersistenceError(
                f"Cannot write sidecar file: {exc}"
            ) from exc

        logger.info(
            "sidecar_updated",
            extra={
                "source_path": str(source_path),
                "sidecar_path": str(sidecar_path),
                "outcome": "updated",
                "source_sha256": source_sha256,
                "body_sha256": incoming_body_sha256,
                "content_status": document.lifecycle.content_status,
                "extraction_policy": document.lifecycle.extraction_policy,
                "index_policy": document.lifecycle.index_policy,
            },
        )
        return SidecarPersistenceResult(
            source_path=source_path,
            sidecar_path=sidecar_path,
            outcome="updated",
            lifecycle=document.lifecycle,
            body_sha256=incoming_body_sha256,
        )

    # --- No existing sidecar: create ---
    try:
        atomic_write_text(sidecar_path, rendered_text)
    except OSError as exc:
        raise SidecarPersistenceError(
            f"Cannot create sidecar file: {exc}"
        ) from exc

    logger.info(
        "sidecar_created",
        extra={
            "source_path": str(source_path),
            "sidecar_path": str(sidecar_path),
            "outcome": "created",
            "source_sha256": source_sha256,
            "body_sha256": incoming_body_sha256,
            "content_status": document.lifecycle.content_status,
            "extraction_policy": document.lifecycle.extraction_policy,
            "index_policy": document.lifecycle.index_policy,
        },
    )
    return SidecarPersistenceResult(
        source_path=source_path,
        sidecar_path=sidecar_path,
        outcome="created",
        lifecycle=document.lifecycle,
        body_sha256=incoming_body_sha256,
    )


def preflight_sidecar(source_path: Path) -> SidecarPreflightResult:
    """Check if source should be extracted or if extraction should be skipped.

    This is a PRE-EXTRACTION gate: call BEFORE extraction/OCR to decide
    whether to proceed with extraction or skip because a final sidecar exists.

    Args:
        source_path: Path to the source file.

    Returns:
        SidecarPreflightResult with decision (extract or skip_final) and
        safe metadata. No extracted body text is included.

    Raises:
        SidecarPersistenceError: If validation fails or sidecar is unsafe
            (malformed, wrong binding, etc.). Preserves bytes/mtime of any
            corrupted sidecar and raises before any extraction caller could proceed.
    """
    source_path = Path(source_path).resolve()

    # --- Validation phase (before any I/O) ---

    # Reject if source name is itself a sidecar name
    if source_path.name.endswith(".vectordrive.md"):
        raise SidecarPersistenceError(
            f"Cannot preflight {source_path.name}: "
            "source filename cannot itself be a vectordrive sidecar"
        )

    # Require existing regular source file
    try:
        if not source_path.exists():
            raise SidecarPersistenceError(
                f"Source file does not exist: {source_path.name}"
            )
        if not source_path.is_file():
            raise SidecarPersistenceError(
                f"Source path is not a regular file: {source_path.name}"
            )
    except OSError as exc:
        raise SidecarPersistenceError(f"Cannot access source file: {exc}") from exc

    # Stream-hash source bytes
    try:
        source_sha256 = _hash_file_sha256(source_path)
    except OSError as exc:
        raise SidecarPersistenceError(f"Cannot read source file for hashing: {exc}") from exc

    sidecar_path = sidecar_path_for(source_path)

    # --- No existing sidecar: proceed with extraction ---
    if not sidecar_path.exists():
        logger.info(
            "sidecar_preflight_extract",
            extra={
                "source_path": str(source_path),
                "sidecar_path": str(sidecar_path),
                "decision": "extract",
                "source_sha256": source_sha256,
                "body_sha256": None,
                "content_status": None,
                "extraction_policy": None,
                "index_policy": None,
            },
        )
        return SidecarPreflightResult(
            source_path=source_path,
            sidecar_path=sidecar_path,
            decision="extract",
            source_sha256=source_sha256,
        )

    # --- Handle existing sidecar ---
    try:
        existing_bytes = sidecar_path.read_bytes()
        existing_text = existing_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Sidecar is unreadable or invalid UTF-8: preserve it and raise
        raise SidecarPersistenceError(
            f"Existing sidecar is invalid or unreadable: {exc}"
        ) from exc

    # Try to parse the existing sidecar
    try:
        existing_doc = parse_markdown(existing_text)
    except MarkdownArtifactError as exc:
        # Sidecar is malformed/tampered: preserve it and raise
        raise SidecarPersistenceError(
            f"Existing sidecar is malformed or tampered: {exc}"
        ) from exc

    # Verify source binding before honoring existing sidecar lifecycle
    if existing_doc.source.filename != source_path.name:
        raise SidecarPersistenceError(
            f"Existing sidecar source filename mismatch: "
            f"sidecar claims {existing_doc.source.filename!r} "
            f"but actual source is {source_path.name!r}"
        )

    if existing_doc.source.sha256 != source_sha256:
        raise SidecarPersistenceError(
            f"Existing sidecar source hash mismatch: "
            f"sidecar claims {existing_doc.source.sha256[:8]}… "
            f"but computed source hash is {source_sha256[:8]}…"
        )

    existing_body_sha256 = hashlib.sha256(
        searchable_text(existing_doc).encode("utf-8")
    ).hexdigest()

    # Check regeneration_locked to decide whether to skip extraction
    if existing_doc.lifecycle.regeneration_locked:
        logger.info(
            "sidecar_preflight_skip_final",
            extra={
                "source_path": str(source_path),
                "sidecar_path": str(sidecar_path),
                "decision": "skip_final",
                "source_sha256": source_sha256,
                "body_sha256": existing_body_sha256,
                "content_status": existing_doc.lifecycle.content_status,
                "extraction_policy": existing_doc.lifecycle.extraction_policy,
                "index_policy": existing_doc.lifecycle.index_policy,
            },
        )
        return SidecarPreflightResult(
            source_path=source_path,
            sidecar_path=sidecar_path,
            decision="skip_final",
            source_sha256=source_sha256,
            lifecycle=existing_doc.lifecycle,
            body_sha256=existing_body_sha256,
        )

    # Sidecar exists but is not final (generated, refreshable): extraction allowed
    logger.info(
        "sidecar_preflight_extract",
        extra={
            "source_path": str(source_path),
            "sidecar_path": str(sidecar_path),
            "decision": "extract",
            "source_sha256": source_sha256,
            "body_sha256": existing_body_sha256,
            "content_status": existing_doc.lifecycle.content_status,
            "extraction_policy": existing_doc.lifecycle.extraction_policy,
            "index_policy": existing_doc.lifecycle.index_policy,
        },
    )
    return SidecarPreflightResult(
        source_path=source_path,
        sidecar_path=sidecar_path,
        decision="extract",
        source_sha256=source_sha256,
        lifecycle=existing_doc.lifecycle,
        body_sha256=existing_body_sha256,
    )


def _hash_file_sha256(path: Path) -> str:
    """Stream-hash a file's bytes to SHA-256 hex digest."""
    sha256 = hashlib.sha256()
    chunk_size = 65536  # 64 KB chunks
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()
