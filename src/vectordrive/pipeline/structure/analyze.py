"""Structure-analysis orchestrator: finds pages of an already-extracted
document that haven't been structure-analyzed yet, runs PP-StructureV3
(or any PPStructureEngine-shaped engine) on each, and writes the result
back into `pages`.

Runs over ALL pages of extracted_doc_id (not just needs_ocr=1 ones, the
way page_ocr.py's ocr_needs_ocr_pages() does) — structure analysis is an
optional pass over pages that already have resolved text, native or OCR,
gated by analyze_native_pages rather than by whether OCR ran.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from vectordrive.pipeline.ocr.page_ocr import _page_image_bytes
from vectordrive.pipeline.structure.cache import run_structure_with_cache


def analyze_document_structure(
    conn: sqlite3.Connection,
    extracted_doc_id: int,
    source_path: Path,
    file_type: str,
    *,
    engine,
    analyze_native_pages: bool,
) -> None:
    """Never raises for engine failures — run_structure_with_cache already
    records them as a cached failure and returns structure_id=None; only
    truly unexpected DB errors propagate. Commits per page, matching
    page_ocr.py's ocr_needs_ocr_pages(), so a large document's progress
    survives a mid-run interruption.
    """
    rows = conn.execute(
        "SELECT id, page_number, text_source FROM pages "
        "WHERE extracted_doc_id = ? AND structure_result_id IS NULL "
        "ORDER BY page_number",
        (extracted_doc_id,),
    ).fetchall()

    for row in rows:
        if row["text_source"] == "native" and not analyze_native_pages:
            continue

        page_id, page_number = row["id"], row["page_number"]
        image_bytes = _page_image_bytes(source_path, file_type, page_number)

        result, structure_id = run_structure_with_cache(conn, image_bytes, engine)

        if result.success:
            conn.execute(
                "UPDATE pages SET structure_result_id = ?, structure_error = NULL WHERE id = ?",
                (structure_id, page_id),
            )
        else:
            conn.execute(
                "UPDATE pages SET structure_error = ? WHERE id = ?",
                (result.error_message, page_id),
            )
        conn.commit()
