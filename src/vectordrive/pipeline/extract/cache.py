"""Extraction-stage cache: wraps any Extractor with a DB-backed cache keyed
by (content_hash, extractor_name, extractor_version) — independent of OCR
engine choice, so OCR-engine changes never invalidate this stage.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from vectordrive.pipeline.extract.base import Extractor, ExtractedDocument, ExtractedPage


def get_cached_extraction(
    conn: sqlite3.Connection, content_hash: str, extractor_name: str, extractor_version: str
) -> ExtractedDocument | None:
    """Return the cached ExtractedDocument for this key, or None on a miss."""
    row = conn.execute(
        "SELECT id, page_count FROM extracted_docs "
        "WHERE content_hash = ? AND extractor_name = ? AND extractor_version = ?",
        (content_hash, extractor_name, extractor_version),
    ).fetchone()
    if row is None:
        return None

    page_rows = conn.execute(
        "SELECT page_number, text, text_source, needs_ocr, "
        "routing_fingerprint, routing_action, routing_visible_chars, "
        "routing_invisible_chars, routing_max_image_coverage, "
        "routing_summed_image_coverage, routing_union_image_coverage, "
        "routing_image_instance_count, routing_font_count, "
        "routing_vector_drawing_count, routing_inline_image_count, "
        "routing_replacement_char_ratio, routing_control_char_ratio, "
        "routing_native_text FROM pages "
        "WHERE extracted_doc_id = ? ORDER BY page_number",
        (row["id"],),
    ).fetchall()
    pages = [
        ExtractedPage(
            page_number=p["page_number"],
            text=p["text"] or "",
            text_source=p["text_source"],
            needs_ocr=bool(p["needs_ocr"]),
            routing_fingerprint=p["routing_fingerprint"],
            routing_action=p["routing_action"],
            routing_visible_chars=p["routing_visible_chars"],
            routing_invisible_chars=p["routing_invisible_chars"],
            routing_max_image_coverage=p["routing_max_image_coverage"],
            routing_summed_image_coverage=p["routing_summed_image_coverage"],
            routing_union_image_coverage=p["routing_union_image_coverage"],
            routing_image_instance_count=p["routing_image_instance_count"],
            routing_font_count=p["routing_font_count"],
            routing_vector_drawing_count=p["routing_vector_drawing_count"],
            routing_inline_image_count=p["routing_inline_image_count"],
            routing_replacement_char_ratio=p["routing_replacement_char_ratio"],
            routing_control_char_ratio=p["routing_control_char_ratio"],
            routing_native_text=p["routing_native_text"],
        )
        for p in page_rows
    ]
    return ExtractedDocument(
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        page_count=row["page_count"],
        pages=pages,
    )


def store_extraction(
    conn: sqlite3.Connection, file_id: int, content_hash: str, doc: ExtractedDocument
) -> int:
    """Persist a freshly-computed ExtractedDocument. Returns extracted_docs.id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO extracted_docs "
        "(file_id, content_hash, extractor_name, extractor_version, page_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, content_hash, doc.extractor_name, doc.extractor_version, doc.page_count, now),
    )
    doc_id = cur.lastrowid
    for page in doc.pages:
        conn.execute(
            "INSERT INTO pages (extracted_doc_id, page_number, text, text_source, needs_ocr, "
            "routing_fingerprint, routing_action, routing_visible_chars, routing_invisible_chars, "
            "routing_max_image_coverage, routing_summed_image_coverage, "
            "routing_union_image_coverage, routing_image_instance_count, routing_font_count, "
            "routing_vector_drawing_count, routing_inline_image_count, "
            "routing_replacement_char_ratio, routing_control_char_ratio, routing_native_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id, page.page_number, page.text, page.text_source, int(page.needs_ocr),
                page.routing_fingerprint, page.routing_action, page.routing_visible_chars,
                page.routing_invisible_chars, page.routing_max_image_coverage,
                page.routing_summed_image_coverage, page.routing_union_image_coverage,
                page.routing_image_instance_count, page.routing_font_count,
                page.routing_vector_drawing_count, page.routing_inline_image_count,
                page.routing_replacement_char_ratio, page.routing_control_char_ratio,
                page.routing_native_text,
            ),
        )
    conn.commit()
    return doc_id


def extract_with_cache(
    conn: sqlite3.Connection,
    file_id: int,
    content_hash: str,
    path: Path,
    extractor: Extractor,
    *,
    extractor_version_override: str | None = None,
) -> tuple[ExtractedDocument, bool]:
    """Get-or-compute an ExtractedDocument. Returns (document, cache_hit).

    `extractor_version_override`, when given, replaces `extractor.VERSION`
    as the cache key's version component (and as the `extractor_version`
    recorded on the stored/returned document), while `extractor.extract()`
    itself still runs unmodified. This opens a separate cache namespace/row
    under the same `(content_hash, extractor_name)` — used by Phase 1
    Markdown generation to isolate its extracted_docs/pages state from
    legacy indexing's, which keeps calling this with no override and so
    keeps resolving to exactly the row `extractor.VERSION` alone would
    (see `services/markdown_generation_service.py` and `docs/ocr.md`).
    Because a miss under the override always INSERTs a new row rather than
    updating an existing one, this can never mutate a row a plain
    (no-override) call would resolve to. Omitting it (the default)
    reproduces prior behavior byte-for-byte.
    """
    version = extractor.VERSION if extractor_version_override is None else extractor_version_override
    cached = get_cached_extraction(conn, content_hash, extractor.NAME, version)
    if cached is not None:
        return cached, True

    doc = extractor.extract(path)
    if extractor_version_override is not None:
        doc = replace(doc, extractor_version=extractor_version_override)
    store_extraction(conn, file_id, content_hash, doc)
    return doc, False
