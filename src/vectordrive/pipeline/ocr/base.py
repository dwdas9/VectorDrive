"""Shared types for the OCR stage: replaceable OcrEngine interface,
OcrConfig (part of the cache key), and OcrResult/OcrLine (what an engine
hands back — text, confidence, reading order and bounding boxes where
supported).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OcrConfig:
    """Engine-agnostic OCR configuration. Part of the OCR cache key
    (page_image_hash, engine_name, model_version, lang, config_hash) — any
    change here is a deliberate cache-busting change.
    """

    lang: str = "en"
    extra: dict[str, Any] = field(default_factory=dict)

    def config_hash(self) -> str:
        payload = json.dumps({"lang": self.lang, "extra": self.extra}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class BoundingBox:
    """Polygon points (pixel coordinates), engine-native shape preserved
    (RapidOCR: 4-point quad; PaddleOCR-VL: rect corners) rather than forcing
    a lossy common format.
    """

    points: list[tuple[float, float]]


@dataclass
class OcrLine:
    text: str
    confidence: float | None
    bbox: BoundingBox | None
    reading_order_index: int


@dataclass
class OcrResult:
    engine_name: str
    model_version: str
    lang: str
    success: bool = True
    text: str = ""
    mean_confidence: float | None = None
    lines: list[OcrLine] = field(default_factory=list)
    error_message: str | None = None
    # Slice A: identity of the ocr_results row this result came from (either
    # freshly inserted on a cache miss, or the row that was hit). None for
    # engine-produced results that have not yet gone through
    # run_ocr_with_cache — that function is the only thing that populates
    # this, on both the hit and miss path.
    result_id: int | None = None
    # Requirement 9 (E1 benchmark observability): whether this particular
    # OcrResult came from the DB cache (True) or a fresh engine.run() call
    # (False). Optional/default so every existing caller that constructs an
    # OcrResult directly (engines themselves, tests) keeps working
    # unchanged; only run_ocr_with_cache populates it non-None.
    cache_hit: bool | None = None


class OcrEngine(Protocol):
    """Any OCR tier (RapidOCR, PaddleOCR-VL, Tesseract, ...) implements
    this. NAME + MODEL_VERSION form part of the cache key alongside the
    page/image hash, lang and config.
    """

    NAME: str
    MODEL_VERSION: str

    def run(self, image_bytes: bytes, config: OcrConfig) -> OcrResult: ...
