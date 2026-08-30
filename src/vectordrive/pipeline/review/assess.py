"""DB-aware orchestration for OCR quality review: reads a document's
resolved `pages`/`ocr_results` rows, calls the pure assessment functions
in pipeline/ocr/quality.py, and persists the result to
page_quality_reviews/document_quality_reviews.

Deliberately independent of pipeline/canonicalize.py's CanonicalDocument
construction: canonicalize.py raises on any missing/mismatched provenance
(by design, for the artifact it builds), which would make a quality
assessment pass fail closed for a case it should instead flag rather than
abort. The page-mode mapping (`_resolve_mode`) intentionally mirrors
canonicalize.py's `_build_page` mapping — kept in sync by the docstring
note below, not by a shared import, to avoid coupling this module to
CanonicalDocument's stricter validation.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from vectordrive.pipeline.ocr.cache import _json_to_lines
from vectordrive.pipeline.ocr.quality import (
    DocumentQualityAssessment,
    PageQualityAssessment,
    assess_document_quality,
    assess_page_quality,
)

logger = logging.getLogger("vectordrive.review")


def _resolve_mode(text_source: str, ocr_error: str | None) -> str:
    """Mirrors pipeline/canonicalize.py's `_build_page` mode mapping."""
    if text_source == "native":
        return "native"
    if text_source in ("ocr", "merged"):
        return text_source
    return "failed" if ocr_error else "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def assess_document(conn: sqlite3.Connection, extracted_doc_id: int) -> dict[int, PageQualityAssessment]:
    """Pure read + assess (no writes): one PageQualityAssessment per page,
    keyed by 1-based page_number.
    """
    page_rows = conn.execute(
        "SELECT id, page_number, text, text_source, ocr_error, ocr_result_id "
        "FROM pages WHERE extracted_doc_id = ? ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()

    assessments: dict[int, PageQualityAssessment] = {}
    for row in page_rows:
        mode = _resolve_mode(row["text_source"], row["ocr_error"])

        ocr_row = None
        ocr_lines: list = []
        if row["ocr_result_id"] is not None:
            ocr_row = conn.execute(
                "SELECT engine_name, model_version, confidence, success, bboxes_json, "
                "reading_order_json FROM ocr_results WHERE id = ?",
                (row["ocr_result_id"],),
            ).fetchone()
            if ocr_row is not None:
                ocr_lines = _json_to_lines(ocr_row["bboxes_json"], ocr_row["reading_order_json"])

        assessments[row["page_number"]] = assess_page_quality(
            mode=mode,
            resolved_text=row["text"] or "",
            ocr_success=bool(ocr_row["success"]) if ocr_row is not None else None,
            ocr_error=row["ocr_error"],
            mean_confidence=ocr_row["confidence"] if ocr_row is not None else None,
            ocr_lines=ocr_lines,
            engine_name=ocr_row["engine_name"] if ocr_row is not None else None,
            model_version=ocr_row["model_version"] if ocr_row is not None else None,
        )
    return assessments


def assess_and_persist_document(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    extracted_doc_id: int,
    source_relative_path: str | None,
) -> DocumentQualityAssessment:
    """Assess every page of `extracted_doc_id` and persist both the
    per-page and document-level results, replacing any prior assessment
    for the same pages/file (a re-index recomputes from scratch — stale
    rows from a previous OCR/extraction result must never linger).

    Transactional and self-healing for this `file_id`: any existing
    page_quality_reviews row for `file_id` whose page_id is not among the
    pages currently belonging to `extracted_doc_id` is deleted first, in
    the same uncommitted transaction as the upserts below (a single
    conn.commit() at the end covers delete + upserts atomically). This
    matters because `pages` has no direct file_id column — a page's file
    association is only via extracted_docs.file_id, which (per M14) is
    not reliably scoped to every file sharing that content — so ordinary
    FK CASCADE alone cannot be trusted to clean up every stale row for
    every duplicate file whenever content changes; this explicit sweep
    is the belt-and-suspenders guarantee that reassessing one file's
    pages can never leave a phantom row behind, and — because it deletes
    by (file_id, page_id) — can never touch another file's rows for a
    page they happen to share (UNIQUE(page_id, file_id) below, and the
    reassessment upsert loop, use that same pair as their key throughout).

    Logging is content-safe: only counts/decisions/reason codes, never
    page text (requirement 5's "never log extracted text" convention,
    reused here for consistency with the rest of the pipeline).
    """
    page_rows = conn.execute(
        "SELECT id, page_number FROM pages WHERE extracted_doc_id = ? ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()
    page_id_by_number = {row["page_number"]: row["id"] for row in page_rows}
    current_page_ids = set(page_id_by_number.values())

    existing_page_ids = {
        row["page_id"]
        for row in conn.execute(
            "SELECT page_id FROM page_quality_reviews WHERE file_id = ?", (file_id,)
        ).fetchall()
    }
    stale_page_ids = existing_page_ids - current_page_ids
    if stale_page_ids:
        placeholders = ",".join("?" for _ in stale_page_ids)
        conn.execute(
            f"DELETE FROM page_quality_reviews WHERE file_id = ? AND page_id IN ({placeholders})",
            (file_id, *stale_page_ids),
        )

    page_assessments = assess_document(conn, extracted_doc_id)
    document_assessment = assess_document_quality(page_assessments)

    now = _now()
    for page_number, assessment in page_assessments.items():
        page_id = page_id_by_number.get(page_number)
        if page_id is None:
            continue  # defensive: page row vanished between the two queries
        conn.execute(
            "INSERT INTO page_quality_reviews "
            "(page_id, extracted_doc_id, file_id, page_number, decision, reason_codes_json, "
            " reasons_json, metrics_json, thresholds_json, engine_name, model_version, "
            " source_relative_path, assessed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(page_id, file_id) DO UPDATE SET "
            "extracted_doc_id = excluded.extracted_doc_id, "
            "page_number = excluded.page_number, decision = excluded.decision, "
            "reason_codes_json = excluded.reason_codes_json, reasons_json = excluded.reasons_json, "
            "metrics_json = excluded.metrics_json, thresholds_json = excluded.thresholds_json, "
            "engine_name = excluded.engine_name, model_version = excluded.model_version, "
            "source_relative_path = excluded.source_relative_path, assessed_at = excluded.assessed_at",
            (
                page_id,
                extracted_doc_id,
                file_id,
                page_number,
                assessment.decision,
                json.dumps(list(assessment.reason_codes)),
                json.dumps([{"code": r.code, "message": r.message} for r in assessment.reasons]),
                json.dumps(dict(assessment.metrics)),
                json.dumps(dict(assessment.thresholds)),
                assessment.metrics.get("engine_name"),
                assessment.metrics.get("model_version"),
                source_relative_path,
                now,
            ),
        )

    conn.execute(
        "INSERT INTO document_quality_reviews "
        "(extracted_doc_id, file_id, decision, reason_codes_json, metrics_json, "
        " flagged_page_numbers_json, source_relative_path, assessed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(file_id) DO UPDATE SET "
        "extracted_doc_id = excluded.extracted_doc_id, decision = excluded.decision, "
        "reason_codes_json = excluded.reason_codes_json, metrics_json = excluded.metrics_json, "
        "flagged_page_numbers_json = excluded.flagged_page_numbers_json, "
        "source_relative_path = excluded.source_relative_path, assessed_at = excluded.assessed_at",
        (
            extracted_doc_id,
            file_id,
            document_assessment.decision,
            json.dumps(list(document_assessment.reason_codes)),
            json.dumps(dict(document_assessment.metrics)),
            json.dumps(list(document_assessment.flagged_page_numbers)),
            source_relative_path,
            now,
        ),
    )
    conn.commit()

    logger.info(
        "event=quality_assessment file_id=%s decision=%s pages_total=%s pages_flagged=%s",
        file_id,
        document_assessment.decision,
        document_assessment.metrics.get("page_count"),
        len(document_assessment.flagged_page_numbers),
    )
    return document_assessment
