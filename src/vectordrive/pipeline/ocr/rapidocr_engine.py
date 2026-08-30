"""Tier 2 of the OCR pipeline: RapidOCR, running PP-OCRv5 (mobile det+rec)
on the onnxruntime backend — verified with a real arm64 wheel
(onnxruntime-1.28.0-cp312-cp312-macosx_14_0_arm64.whl) and a live round-trip
against the synthetic fixtures during development. Primary/default engine
for JPG, PNG and image-only PDF pages.
"""
from __future__ import annotations

from vectordrive.pipeline.ocr.base import BoundingBox, OcrConfig, OcrLine, OcrResult

_OCR_VERSION = "PP-OCRv5"
_MODEL_TYPE = "mobile"
_ENGINE_TYPE = "onnxruntime"


class RapidOcrEngine:
    NAME = "rapidocr"
    MODEL_VERSION = f"{_OCR_VERSION}-{_MODEL_TYPE}-{_ENGINE_TYPE}"

    def __init__(self):
        self._engine = None  # lazy: don't load ONNX models until first use

    def _get_engine(self):
        if self._engine is None:
            from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

            self._engine = RapidOCR(
                params={
                    "Det.engine_type": EngineType.ONNXRUNTIME,
                    "Det.ocr_version": OCRVersion.PPOCRV5,
                    "Det.model_type": ModelType.MOBILE,
                    "Rec.engine_type": EngineType.ONNXRUNTIME,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE,
                }
            )
        return self._engine

    def run(self, image_bytes: bytes, config: OcrConfig) -> OcrResult:
        engine = self._get_engine()
        result = engine(image_bytes)

        if not result.txts:
            # No text detected is a valid outcome (e.g. a blank page), not
            # an engine failure.
            return OcrResult(
                engine_name=self.NAME, model_version=self.MODEL_VERSION, lang=config.lang
            )

        lines = [
            OcrLine(
                text=text,
                confidence=float(score),
                bbox=BoundingBox(points=[(float(x), float(y)) for x, y in box.tolist()]),
                reading_order_index=index,
            )
            for index, (box, text, score) in enumerate(
                zip(result.boxes, result.txts, result.scores)
            )
        ]
        return OcrResult(
            engine_name=self.NAME,
            model_version=self.MODEL_VERSION,
            lang=config.lang,
            success=True,
            text="\n".join(result.txts),
            mean_confidence=sum(result.scores) / len(result.scores),
            lines=lines,
        )
