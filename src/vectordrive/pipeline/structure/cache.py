"""Structure-analysis cache: mirrors ocr/cache.py exactly, one layer up
the pipeline — wraps a PPStructureEngine-shaped object with a DB-backed
cache keyed by (page_image_hash, engine_name, model_version, config_hash).

Content-addressed and deduped across files, same as ocr_results: two
different files with an identical scanned page (same bytes) share one
structure_results row. Failures are cached too (success=0,
error_message set) — a known-bad page/config combination is recorded
clearly rather than silently retried on every run; changing the engine,
model, or config busts the cache and gives a fresh attempt.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from vectordrive.pipeline.hashing import sha256_bytes
from vectordrive.pipeline.structure.ppstructure_engine import StructureBlock, StructureResult


def _blocks_to_json(blocks: list[StructureBlock]) -> str:
    # Wire format per VECTORDRIVE_IMPLEMENTATION_PLAN.md §6.7.
    return json.dumps(
        [
            {
                "type": b.type,
                "text": b.text,
                "bbox": b.bbox,
                "reading_order": b.reading_order,
                "confidence": b.confidence,
                "parent_index": b.parent_index,
            }
            for b in blocks
        ]
    )


def _json_to_blocks(blocks_json: str | None) -> list[StructureBlock]:
    if not blocks_json:
        return []
    return [
        StructureBlock(
            type=b["type"],
            text=b["text"],
            bbox=b.get("bbox"),
            reading_order=b["reading_order"],
            confidence=b.get("confidence"),
            parent_index=b.get("parent_index"),
        )
        for b in json.loads(blocks_json)
    ]


def get_cached_structure_result(
    conn: sqlite3.Connection,
    page_image_hash: str,
    engine_name: str,
    model_version: str,
    config_hash: str,
) -> tuple[StructureResult, int] | None:
    row = conn.execute(
        "SELECT * FROM structure_results WHERE page_image_hash = ? AND engine_name = ? "
        "AND model_version = ? AND config_hash = ?",
        (page_image_hash, engine_name, model_version, config_hash),
    ).fetchone()
    if row is None:
        return None
    result = StructureResult(
        engine_name=engine_name,
        model_version=model_version,
        success=bool(row["success"]),
        markdown=row["markdown"] or "",
        blocks=_json_to_blocks(row["blocks_json"]),
        error_message=row["error_message"],
    )
    return result, row["id"]


def store_structure_result(
    conn: sqlite3.Connection, page_image_hash: str, config_hash: str, result: StructureResult
) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute(
            "INSERT INTO structure_results "
            "(page_image_hash, engine_name, model_version, config_hash, success, "
            " error_message, markdown, blocks_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                page_image_hash,
                result.engine_name,
                result.model_version,
                config_hash,
                int(result.success),
                result.error_message,
                result.markdown,
                _blocks_to_json(result.blocks),
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error:
        conn.rollback()
        return None


def run_structure_with_cache(
    conn: sqlite3.Connection,
    image_bytes: bytes,
    engine,
    config_hash: str = "v1",
) -> tuple[StructureResult, int | None]:
    """Get-or-compute a StructureResult for `image_bytes` under `engine`.

    Returns (result, structure_results.id — None only if the insert
    itself failed). A failing engine.run() (including
    PPStructureUnavailableError) is caught here and turned into a
    clearly-recorded StructureResult(success=False, error_message=...)
    rather than propagating — the caller (analyze_document_structure)
    decides what a failure means for the page, but it's never silent.
    """
    page_image_hash = sha256_bytes(image_bytes)

    cached = get_cached_structure_result(conn, page_image_hash, engine.NAME, engine.MODEL_VERSION, config_hash)
    if cached is not None:
        return cached

    try:
        result = engine.run(image_bytes)
    except Exception as exc:
        result = StructureResult(
            engine_name=engine.NAME,
            model_version=engine.MODEL_VERSION,
            success=False,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    structure_result_id = store_structure_result(conn, page_image_hash, config_hash, result)
    return result, structure_result_id
