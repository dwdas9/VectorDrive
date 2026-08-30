"""OCR-stage cache: wraps any OcrEngine with a DB-backed cache keyed by
(page_image_hash, engine_name, model_version, lang, config_hash).

Content-addressed and deduped across files — two different files with an
identical scanned page (same bytes) share one ocr_results row. Deterministic
failures are cached too (success=0, error_message set), while transient
connection/time-out failures are evicted on the next explicit attempt so a
temporary Ollama outage cannot permanently poison an otherwise valid page.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from vectordrive.pipeline.hashing import sha256_bytes
from vectordrive.pipeline.ocr.base import BoundingBox, OcrConfig, OcrEngine, OcrLine, OcrResult


_TRANSIENT_FAILURE_MARKERS = (
    "connecterror",
    "connection refused",
    "connection reset",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


def is_transient_ocr_failure(error_message: str | None) -> bool:
    """Infrastructure failures may recover and must never poison OCR cache."""
    message = (error_message or "").casefold()
    return any(marker in message for marker in _TRANSIENT_FAILURE_MARKERS)


def _lines_to_json(lines: list[OcrLine]) -> tuple[str, str]:
    bboxes = [
        {
            "text": line.text,
            "confidence": line.confidence,
            "bbox": line.bbox.points if line.bbox else None,
        }
        for line in lines
    ]
    reading_order = [line.reading_order_index for line in lines]
    return json.dumps(bboxes), json.dumps(reading_order)


def _json_to_lines(bboxes_json: str | None, reading_order_json: str | None) -> list[OcrLine]:
    if not bboxes_json:
        return []
    bboxes = json.loads(bboxes_json)
    order = json.loads(reading_order_json) if reading_order_json else list(range(len(bboxes)))
    return [
        OcrLine(
            text=b["text"],
            confidence=b.get("confidence"),
            bbox=BoundingBox(points=[tuple(p) for p in b["bbox"]]) if b.get("bbox") else None,
            reading_order_index=idx,
        )
        for idx, b in zip(order, bboxes)
    ]


def get_cached_ocr_result(
    conn: sqlite3.Connection,
    page_image_hash: str,
    engine_name: str,
    model_version: str,
    lang: str,
    config_hash: str,
) -> OcrResult | None:
    row = conn.execute(
        "SELECT * FROM ocr_results WHERE page_image_hash = ? AND engine_name = ? "
        "AND model_version = ? AND lang = ? AND config_hash = ?",
        (page_image_hash, engine_name, model_version, lang, config_hash),
    ).fetchone()
    if row is None:
        return None
    return OcrResult(
        engine_name=engine_name,
        model_version=model_version,
        lang=lang,
        success=bool(row["success"]),
        text=row["text"] or "",
        mean_confidence=row["confidence"],
        lines=_json_to_lines(row["bboxes_json"], row["reading_order_json"]),
        error_message=row["error_message"],
        result_id=row["id"],
    )


def store_ocr_result(
    conn: sqlite3.Connection, page_image_hash: str, config_hash: str, result: OcrResult
) -> int:
    bboxes_json, reading_order_json = _lines_to_json(result.lines)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO ocr_results "
        "(page_image_hash, engine_name, model_version, lang, config_hash, text, confidence, "
        " bboxes_json, reading_order_json, success, error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            page_image_hash,
            result.engine_name,
            result.model_version,
            result.lang,
            config_hash,
            result.text,
            result.mean_confidence,
            bboxes_json,
            reading_order_json,
            int(result.success),
            result.error_message,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def run_ocr_with_cache(
    conn: sqlite3.Connection, image_bytes: bytes, engine: OcrEngine, config: OcrConfig
) -> tuple[OcrResult, bool]:
    """Get-or-compute an OcrResult for `image_bytes` under `engine`+`config`.

    Returns (result, cache_hit). A failing engine.run() is caught here and
    turned into a clearly-recorded OcrResult(success=False, error_message=...)
    rather than propagating — the caller (tiered policy / page orchestrator)
    decides what a failure means for the page, but it's never silent.
    """
    page_image_hash = sha256_bytes(image_bytes)
    config_hash = config.config_hash()

    cached = get_cached_ocr_result(
        conn, page_image_hash, engine.NAME, engine.MODEL_VERSION, config.lang, config_hash
    )
    if cached is not None:
        if cached.success or not is_transient_ocr_failure(cached.error_message):
            cached.cache_hit = True
            return cached, True
        # Keep the failure row long enough for diagnosis, but evict it when
        # the same page is explicitly attempted again. This also repairs
        # transient failures cached by earlier VectorDrive releases.
        conn.execute("DELETE FROM ocr_results WHERE id = ?", (cached.result_id,))
        conn.commit()

    try:
        result = engine.run(image_bytes, config)
    except Exception as exc:
        result = OcrResult(
            engine_name=engine.NAME,
            model_version=engine.MODEL_VERSION,
            lang=config.lang,
            success=False,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    result_id = store_ocr_result(conn, page_image_hash, config_hash, result)
    result.result_id = result_id
    result.cache_hit = False
    return result, False
