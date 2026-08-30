"""Builds an in-memory CanonicalDocument from resolved database state.

Pure/read-only: never writes to the filesystem, never renders a sidecar
(that's a later slice). Reads exactly the rows a completed extraction (and
optionally OCR/structure) pass left behind and maps them onto the strict
types in pipeline/markdown_artifact.py, refusing to invent provenance that
isn't actually there — see CanonicalizationError's raise sites below.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from vectordrive import __version__ as PIPELINE_VERSION
from vectordrive.pipeline.markdown_artifact import (
    CanonicalDocument,
    GenerationProvenance,
    PageContent,
    SourceProvenance,
)

# Deterministic, explicit file_type -> media_type mapping. Anything not
# listed here (should not happen for a successfully extracted file, since
# extract/registry.py's _REGISTRY covers the same set) falls back to the
# generic binary type rather than guessing.
_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "eml": "message/rfc822",
    "txt": "text/plain",
    "md": "text/markdown",
    "text": "text/plain",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_FALLBACK_MEDIA_TYPE = "application/octet-stream"


class CanonicalizationError(ValueError):
    """Raised when resolved database state can't be safely turned into a
    CanonicalDocument — either it's internally contradictory, or the
    required provenance is missing rather than something this builder is
    willing to invent.
    """


def _media_type_for(file_type: str) -> str:
    return _MEDIA_TYPES.get((file_type or "").lower(), _FALLBACK_MEDIA_TYPE)


# Phase 1 page-level PDF routing provenance surfaced into PageContent.metadata
# (and therefore into the rendered page markers). Maps the public metadata key
# to its pages.* column. Only populated for PDF pages the Phase 1 router
# handled; every value is a JSON-safe scalar. Legacy/non-PDF pages have
# routing_fingerprint IS NULL and contribute no routing keys at all.
_ROUTING_METADATA_COLUMNS: dict[str, str] = {
    "routing_fingerprint": "routing_fingerprint",
    "extraction_action": "routing_action",
    "routing_visible_chars": "routing_visible_chars",
    "routing_invisible_chars": "routing_invisible_chars",
    "routing_max_image_coverage": "routing_max_image_coverage",
    "routing_summed_image_coverage": "routing_summed_image_coverage",
    "routing_union_image_coverage": "routing_union_image_coverage",
    "routing_image_instance_count": "routing_image_instance_count",
    "routing_font_count": "routing_font_count",
    "routing_vector_drawing_count": "routing_vector_drawing_count",
    "routing_inline_image_count": "routing_inline_image_count",
    "routing_replacement_char_ratio": "routing_replacement_char_ratio",
    "routing_control_char_ratio": "routing_control_char_ratio",
}


def _routing_metadata(row: sqlite3.Row) -> dict:
    if row["routing_fingerprint"] is None:
        return {}
    return {
        key: row[column]
        for key, column in _ROUTING_METADATA_COLUMNS.items()
        if row[column] is not None
    }


def _with_routing(structure_kwargs: dict, routing_meta: dict) -> dict:
    if not routing_meta:
        return structure_kwargs
    merged = dict(structure_kwargs.get("metadata") or {})
    merged.update(routing_meta)
    return {**structure_kwargs, "metadata": merged}


def _portable_relative_path(relative_path: str | None, filename: str) -> str:
    """Never leak the absolute on-disk path into a portable document.

    relative_path (root-relative, Stage 3) is preferred when available;
    otherwise fall back to the bare filename — never files.path itself,
    which is absolute. files.relative_path is *supposed* to already be
    root-relative, but it is not validated when written — if it was
    somehow populated with an absolute path (bug elsewhere, hand-edited
    DB, ...), serializing it verbatim would leak exactly the absolute
    on-disk path this function exists to hide. Detect that case (POSIX
    or Windows absolute form) and safely fall back to the bare filename
    instead of trusting it blindly.
    """
    if relative_path and not PurePosixPath(relative_path).is_absolute() and not PureWindowsPath(
        relative_path
    ).is_absolute():
        return relative_path
    return filename


def build_canonical_document(
    conn: sqlite3.Connection,
    file_id: int,
    extracted_doc_id: int,
    *,
    generated_at: datetime | None = None,
) -> CanonicalDocument:
    """Build a CanonicalDocument from already-resolved DB state.

    `generated_at` is injectable for deterministic tests; defaults to
    datetime.now(timezone.utc).
    """
    file_row = conn.execute(
        "SELECT id, path, relative_path, content_hash, size, file_type FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if file_row is None:
        raise CanonicalizationError(f"no file found with id={file_id}")

    doc_row = conn.execute(
        "SELECT id, file_id, content_hash, extractor_name, extractor_version, page_count "
        "FROM extracted_docs WHERE id = ?",
        (extracted_doc_id,),
    ).fetchone()
    if doc_row is None:
        raise CanonicalizationError(f"no extracted_doc found with id={extracted_doc_id}")
    # extracted_docs is intentionally content-addressed (UNIQUE(content_hash,
    # extractor_name, extractor_version)): a row can be shared across any
    # files with identical content and is not scoped to a single file_id
    # (M14). Checking doc_row["file_id"] == file_id would reject exactly
    # that legitimate sharing for a duplicate file. The invariant that
    # actually matters — this extracted_doc's content really is this
    # file's content, under the extractor identity the caller already
    # resolved it by — is that the content hashes agree.
    if doc_row["content_hash"] != file_row["content_hash"]:
        raise CanonicalizationError(
            f"extracted_doc {extracted_doc_id} has content_hash={doc_row['content_hash']!r}, "
            f"which does not match file_id={file_id}'s content_hash={file_row['content_hash']!r}"
        )

    filename = Path(file_row["path"]).name

    source = SourceProvenance(
        filename=filename,
        relative_path=_portable_relative_path(file_row["relative_path"], filename),
        sha256=file_row["content_hash"],
        media_type=_media_type_for(file_row["file_type"]),
        byte_size=file_row["size"],
        page_count=doc_row["page_count"],
    )

    generation = GenerationProvenance(
        pipeline_version=PIPELINE_VERSION,
        generated_at=generated_at if generated_at is not None else datetime.now(timezone.utc),
        extractor_name=doc_row["extractor_name"],
        extractor_version=doc_row["extractor_version"],
    )

    page_rows = conn.execute(
        "SELECT id, page_number, text, text_source, ocr_error, ocr_result_id, "
        "page_image_hash, structure_result_id, structure_error, "
        "routing_fingerprint, routing_action, routing_visible_chars, "
        "routing_invisible_chars, routing_max_image_coverage, "
        "routing_summed_image_coverage, routing_union_image_coverage, "
        "routing_image_instance_count, routing_font_count, "
        "routing_vector_drawing_count, routing_inline_image_count, "
        "routing_replacement_char_ratio, routing_control_char_ratio "
        "FROM pages WHERE extracted_doc_id = ? ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()

    pages = tuple(_build_page(conn, row) for row in page_rows)

    return CanonicalDocument(source=source, generation=generation, pages=pages)


def _build_page(conn: sqlite3.Connection, row: sqlite3.Row) -> PageContent:
    page_number = row["page_number"]
    text_source = row["text_source"]
    routing_meta = _routing_metadata(row)

    if text_source == "native":
        structure_kwargs, structure_text = _structure_kwargs(conn, row)
        structure_kwargs = _with_routing(structure_kwargs, routing_meta)
        text = structure_text if structure_text is not None else (row["text"] or "")
        return PageContent(
            page_number=page_number,
            mode="native",
            text=text,
            **structure_kwargs,
        )

    if text_source in ("ocr", "merged"):
        ocr_result_id = row["ocr_result_id"]
        if ocr_result_id is None:
            raise CanonicalizationError(
                f"page {page_number}: text_source={text_source!r} but ocr_result_id is "
                "missing — this database predates Slice A's durable OCR provenance "
                "(pages.ocr_result_id). Delete the derived database and re-index "
                "(or run a Full Rebuild) to regenerate it with provenance recorded."
            )

        ocr_row = conn.execute(
            "SELECT engine_name, model_version, lang, confidence, success, page_image_hash "
            "FROM ocr_results WHERE id = ?",
            (ocr_result_id,),
        ).fetchone()
        if ocr_row is None or not bool(ocr_row["success"]):
            raise CanonicalizationError(
                f"page {page_number}: ocr_result_id={ocr_result_id} does not point at a "
                "valid successful ocr_results row — refusing to invent OCR provenance. "
                "Delete the derived database and re-index to regenerate it."
            )
        if ocr_row["page_image_hash"] != row["page_image_hash"]:
            # The link is only trustworthy if it proves the exact page
            # image OCR was run against, not merely "some successful OCR
            # row happens to be referenced". A mismatch here means the
            # durable link doesn't correspond to this page's resolved
            # image — refuse rather than attribute the wrong provenance.
            raise CanonicalizationError(
                f"page {page_number}: ocr_result_id={ocr_result_id} has "
                f"page_image_hash={ocr_row['page_image_hash']!r}, which does not match "
                f"this page's page_image_hash={row['page_image_hash']!r} — refusing to "
                "attribute OCR provenance to a mismatched page image."
            )

        structure_kwargs, structure_text = _structure_kwargs(conn, row)
        structure_kwargs = _with_routing(structure_kwargs, routing_meta)
        text = structure_text if structure_text is not None else (row["text"] or "")
        return PageContent(
            page_number=page_number,
            mode=text_source,
            text=text,
            ocr_engine=ocr_row["engine_name"],
            ocr_model=ocr_row["model_version"],
            ocr_language=ocr_row["lang"],
            mean_confidence=ocr_row["confidence"],
            **structure_kwargs,
        )

    # text_source == 'none': unresolved. failed/skipped pages carry no
    # resolved text and must never acquire one, even when linked to a
    # successful structure result with nonblank Markdown — the structure
    # text override is deliberately discarded here, only structural_tags/
    # metadata (and provenance validation) survive.
    structure_kwargs, _structure_text_discarded = _structure_kwargs(conn, row)
    structure_kwargs = _with_routing(structure_kwargs, routing_meta)
    mode = "failed" if row["ocr_error"] else "skipped"
    return PageContent(page_number=page_number, mode=mode, text="", **structure_kwargs)


def _structure_kwargs(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, str | None]:
    """Derive structural_tags/metadata plus an optional canonical whole-page
    text override from a page's linked structure_results row, if any.

    Returns (public PageContent kwargs, text override or None). The text
    override is a successful, nonblank whole-page structure Markdown
    rendering (architecture decision 1) — callers decide whether to apply
    it; failed/skipped pages must always discard it (decision 4). Never
    represents a failed/malformed structure result as successful, and
    never lets a structure result attribute text to the wrong page image.
    """
    structure_result_id = row["structure_result_id"]
    if structure_result_id is None:
        return {}, None

    structure_row = conn.execute(
        "SELECT engine_name, model_version, success, blocks_json, markdown, page_image_hash "
        "FROM structure_results WHERE id = ?",
        (structure_result_id,),
    ).fetchone()
    if structure_row is None:
        raise CanonicalizationError(
            f"page {row['page_number']}: structure_result_id={structure_result_id} does "
            "not point at any structure_results row"
        )

    if not bool(structure_row["success"]):
        # A failed structure result must not be silently represented as
        # successful: omit structural_tags entirely and surface an explicit
        # scalar error indicator instead of guessing block types. It also
        # never replaces page text (architecture decision 1).
        return {"metadata": {"structure_error": True}}, None

    page_image_hash = row["page_image_hash"]
    structure_image_hash = structure_row["page_image_hash"]
    if page_image_hash is not None and structure_image_hash is not None and page_image_hash != structure_image_hash:
        # The link is only trustworthy if it proves the exact page image
        # structure analysis was run against — a mismatch means the durable
        # link doesn't correspond to this page's resolved image. Refuse
        # rather than attribute the wrong structure result (decision 2).
        raise CanonicalizationError(
            f"page {row['page_number']}: structure_result_id={structure_result_id} has "
            f"page_image_hash={structure_image_hash!r}, which does not match this page's "
            f"page_image_hash={page_image_hash!r} — refusing to attribute structure "
            "provenance to a mismatched page image."
        )

    blocks_json = structure_row["blocks_json"]
    try:
        blocks = json.loads(blocks_json) if blocks_json else []
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(
            f"page {row['page_number']}: structure_result_id={structure_result_id} has "
            f"malformed blocks_json: {exc}"
        ) from exc
    if not isinstance(blocks, list):
        raise CanonicalizationError(
            f"page {row['page_number']}: structure_result_id={structure_result_id} "
            "blocks_json is not a JSON array"
        )

    block_types = sorted(
        {block.get("type") for block in blocks if isinstance(block, dict) and block.get("type")}
    )

    result = {
        "structural_tags": tuple(block_types),
        "metadata": {
            "structure_engine": structure_row["engine_name"],
            "structure_model": structure_row["model_version"],
            "structure_block_count": len(blocks),
        },
    }
    markdown = structure_row["markdown"]
    text_override = None
    if markdown and markdown.strip():
        # A successful structure result with blank Markdown does not
        # replace page text (decision 1) — only a nonblank whole-page
        # rendering does.
        text_override = markdown
    return result, text_override
