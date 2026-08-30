"""Page-level OCR orchestrator: finds pages flagged needs_ocr for an
already-extracted document, runs tiered OCR on each, and writes the result
back into `pages`.

Filename and PDF page number are preserved structurally, not duplicated
here: `pages.page_number` was already recorded at extraction time (M4), and
the file's path is one join away via extracted_docs.file_id -> files.path —
both remain queryable after OCR runs (see test_page_ocr.py for a direct
proof). Standalone image files (jpg/png) use their own bytes as "page 1";
image-only PDF pages get rendered via page_render.render_page_to_image.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from vectordrive.pipeline.hashing import sha256_bytes
from vectordrive.pipeline.ocr.base import OcrEngine
from vectordrive.pipeline.ocr.merge import (
    combine_native_and_qwen_page_blocks,
    merge_native_and_ocr_text,
    merge_native_with_structured_ocr_markdown,
)
from vectordrive.pipeline.ocr.page_render import render_page_to_image
from vectordrive.pipeline.ocr.tiered import DEFAULT_CONFIDENCE_THRESHOLD, run_tiered_ocr

logger = logging.getLogger(__name__)

_IMAGE_FILE_TYPES = {"jpg", "jpeg", "png"}


@dataclass
class PageOcrOutcome:
    page_number: int
    success: bool
    engine_name: str | None
    text: str
    error_message: str | None
    # Slice A: additional selected-result provenance, populated only when
    # success is True (mirrors what gets persisted to pages.ocr_result_id).
    model_version: str | None = None
    language: str | None = None
    mean_confidence: float | None = None
    ocr_result_id: int | None = None
    # Requirement 9 (E1 benchmark observability): page-level timing/output
    # measured around this function's own render+OCR work for this page
    # (not a fabricated per-engine-attempt breakdown), the resolved text's
    # character count, the page image's content hash, and whether the
    # *selected* result came from the OCR cache. All optional/default —
    # existing production callers that don't read them are unaffected.
    elapsed_ms: float | None = None
    output_char_count: int | None = None
    page_image_hash: str | None = None
    cache_hit: bool | None = None


def _page_image_bytes(source_path: Path, file_type: str, page_number: int) -> bytes:
    if file_type.lower() in _IMAGE_FILE_TYPES:
        return Path(source_path).read_bytes()
    return render_page_to_image(source_path, page_number)


def ocr_needs_ocr_pages(
    conn: sqlite3.Connection,
    extracted_doc_id: int,
    source_path: Path,
    file_type: str,
    *,
    tier2_engine: OcrEngine,
    tier3_engine: OcrEngine | None = None,
    tier4_engine: OcrEngine | None = None,
    lang: str = "en",
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    preserve_ocr_structure: bool = False,
) -> list[PageOcrOutcome]:
    rows = conn.execute(
        "SELECT id, page_number, text, routing_action, routing_native_text "
        "FROM pages WHERE extracted_doc_id = ? AND needs_ocr = 1 "
        "ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()

    outcomes: list[PageOcrOutcome] = []
    for row in rows:
        page_id, page_number = row["id"], row["page_number"]
        routing_action = row["routing_action"]
        # Page-composition routing (extract/pdf.py) may have left real
        # native text here even though the page still needs_ocr (e.g. a
        # watermark over a full-page scan) — merge it in below rather than
        # discarding it.
        native_text = (row["text"] or "").strip()
        page_start = time.perf_counter()
        image_bytes = _page_image_bytes(source_path, file_type, page_number)
        page_image_hash = sha256_bytes(image_bytes)

        result = run_tiered_ocr(
            conn,
            image_bytes,
            tier2_engine=tier2_engine,
            tier3_engine=tier3_engine,
            tier4_engine=tier4_engine,
            lang=lang,
            confidence_threshold=confidence_threshold,
        )

        if result.success:
            if result.result_id is None:
                # Invariant violation: run_ocr_with_cache always populates
                # result_id (hit or miss) for a successful result. Writing
                # OCR text without a verifiable ocr_results row would leave
                # canonicalize.py unable to prove provenance for this page
                # — fail loudly rather than write unverifiable text.
                raise RuntimeError(
                    f"page {page_number}: successful OCR result from "
                    f"{result.engine_name!r} has no result_id; refusing to "
                    "persist unverifiable OCR text"
                )
            if routing_action == "qwen_page":
                # Requirement 6: the Qwen transcription is canonical. Any
                # hidden / watermark / native text layer this page carried is
                # diagnostic only -- it stays in pages.routing_native_text and
                # must never be prepended or similarity-merged into the
                # searchable page text.
                final_text = result.text
                final_source = "ocr"
            elif routing_action == "native_plus_qwen_page":
                # Requirement 6: preserve the native text and the Qwen
                # Markdown byte-for-byte, joined only by fixed machine-readable
                # markers. No dedup / rewrite / correction.
                page_native = row["routing_native_text"]
                if page_native is None:
                    page_native = row["text"] or ""
                final_text = combine_native_and_qwen_page_blocks(page_native, result.text)
                final_source = "merged"
            elif native_text:
                # Requirement 1: a full-page OCR pass re-transcribes
                # everything visible, including whatever the native text
                # layer already captured -- naive concatenation duplicated
                # most of the page. merge_native_and_ocr_text keeps native
                # text as the base and appends only OCR content that isn't
                # already (near-)covered by it (e.g. handwriting, a stamp).
                if preserve_ocr_structure:
                    # Requirement (release-blocking finding): the caller's
                    # OCR result is validated structured Markdown (Qwen), not
                    # plain OCR text -- running it through the line-by-line
                    # dedup below would drop blank lines, punctuation-only
                    # lines, and GFM table delimiter rows, corrupting an
                    # already-valid Markdown transcription. Never touch the
                    # OCR block's lines in that case.
                    final_text = merge_native_with_structured_ocr_markdown(
                        native_text, result.text
                    )
                else:
                    final_text = merge_native_and_ocr_text(native_text, result.text).text
                final_source = "merged"
            else:
                final_text = result.text
                final_source = "ocr"
            conn.execute(
                "UPDATE pages SET text = ?, text_source = ?, needs_ocr = 0, "
                "page_image_hash = ?, ocr_error = NULL, ocr_result_id = ? WHERE id = ?",
                (final_text, final_source, page_image_hash, result.result_id, page_id),
            )
        else:
            final_text = ""
            # ocr_error must stay non-empty: canonicalize.py classifies an
            # unresolved page as mode='failed' only when ocr_error is truthy
            # (mode='skipped' otherwise), and Phase 2 rejects failed pages but
            # would index a skipped-only sidecar as an empty success. Every
            # current engine failure path populates error_message, but a bare
            # OcrResult(success=False) would not -- pin a fallback so a failed
            # OCR page can never silently degrade to 'skipped'.
            ocr_error = result.error_message or "OCR failed (engine reported no error message)"
            conn.execute(
                "UPDATE pages SET page_image_hash = ?, ocr_error = ?, "
                "ocr_result_id = NULL WHERE id = ?",
                (page_image_hash, ocr_error, page_id),
            )
        conn.commit()
        elapsed_ms = (time.perf_counter() - page_start) * 1000
        logger.info(
            # source_path is the exact on-disk path (explicit operator
            # requirement). Only counts/metadata are logged, never page text.
            "event=page_ocr_action source_path=%s page=%d routing_action=%s engine=%s "
            "success=%s cache_hit=%s elapsed_ms=%d output_chars=%d",
            str(source_path),
            page_number,
            routing_action or "legacy",
            (result.engine_name if result.success else None),
            result.success,
            result.cache_hit,
            int(elapsed_ms),
            len(final_text),
        )

        outcomes.append(
            PageOcrOutcome(
                page_number=page_number,
                success=result.success,
                engine_name=result.engine_name if result.success else None,
                text=final_text,
                error_message=result.error_message,
                model_version=result.model_version if result.success else None,
                language=result.lang if result.success else None,
                mean_confidence=result.mean_confidence if result.success else None,
                ocr_result_id=result.result_id if result.success else None,
                elapsed_ms=elapsed_ms,
                output_char_count=len(final_text),
                page_image_hash=page_image_hash,
                cache_hit=result.cache_hit,
            )
        )
    return outcomes
