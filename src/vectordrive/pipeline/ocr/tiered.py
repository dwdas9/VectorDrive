"""Tiered OCR escalation policy.

tier2 (RapidOCR, always on) runs first. If it fails outright, or succeeds
with mean_confidence below `confidence_threshold`, escalate to tier3
(PaddleOCR-VL) when configured — this is the practical, v0.1-simple proxy
for "difficult scans, tables, handwriting, complex layouts": we can't
reliably auto-classify those without more ML, so a confidence gate is the
signal, and any page that needs it anyway (image-only, always low
native-text confidence) naturally routes through here. If tier3 also fails
(or isn't configured), fall back to tier4 (Tesseract) when configured —
kept strictly last-resort per requirement 5, never tried first. Each tier
is still individually cached via run_ocr_with_cache, so re-running an
already-resolved page never re-invokes any engine.
"""
from __future__ import annotations

import sqlite3

from vectordrive.pipeline.ocr.base import OcrConfig, OcrEngine, OcrResult
from vectordrive.pipeline.ocr.cache import run_ocr_with_cache

DEFAULT_CONFIDENCE_THRESHOLD = 0.70


def _is_good_enough(result: OcrResult, threshold: float) -> bool:
    return result.success and result.mean_confidence is not None and result.mean_confidence >= threshold


def run_tiered_ocr(
    conn: sqlite3.Connection,
    image_bytes: bytes,
    *,
    tier2_engine: OcrEngine,
    tier3_engine: OcrEngine | None = None,
    tier4_engine: OcrEngine | None = None,
    lang: str = "en",
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    tier2_config: OcrConfig | None = None,
    tier3_config: OcrConfig | None = None,
    tier4_config: OcrConfig | None = None,
) -> OcrResult:
    result2, _ = run_ocr_with_cache(
        conn, image_bytes, tier2_engine, tier2_config or OcrConfig(lang=lang)
    )
    if _is_good_enough(result2, confidence_threshold):
        return result2

    # A successful-but-low-confidence result is still usable text — prefer
    # it over a bare failure if every escalation attempt below also fails.
    best_success = result2 if result2.success else None
    last_result = result2

    if tier3_engine is not None:
        result3, _ = run_ocr_with_cache(
            conn, image_bytes, tier3_engine, tier3_config or OcrConfig(lang=lang)
        )
        last_result = result3
        if result3.success:
            return result3

    if tier4_engine is not None:
        result4, _ = run_ocr_with_cache(
            conn, image_bytes, tier4_engine, tier4_config or OcrConfig(lang=lang)
        )
        last_result = result4
        if result4.success:
            return result4

    return best_success if best_success is not None else last_result
