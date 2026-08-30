"""Tier 4 of the OCR pipeline: Tesseract, kept strictly optional and never
the primary engine (per requirement 5). Only ever reached by the tiered
policy (tiered.py) after tier2 (RapidOCR) and tier3 (PaddleOCR-VL, if
configured) have both failed.

`tesseract` (the binary) is not installed on this development machine, and
this engine is not wired into the default tier chain — it exists as a real,
working interface implementation for anyone who installs Tesseract, not a
stub. Both the binary and the `pytesseract` Python wrapper are checked
explicitly, with distinct, specific error messages for each — requirement
9: never silently skipped.
"""
from __future__ import annotations

import io
import shutil

from vectordrive.pipeline.ocr.base import BoundingBox, OcrConfig, OcrLine, OcrResult

_MIN_CONFIDENCE = 0  # pytesseract uses -1 for non-text regions; filter those out


class TesseractUnavailableError(RuntimeError):
    """Tesseract could not be used — message carries the exact blocker."""


class TesseractEngine:
    NAME = "tesseract"
    MODEL_VERSION = "system"

    def _require_binary(self) -> None:
        if shutil.which("tesseract") is None:
            raise TesseractUnavailableError(
                "tesseract binary not found on PATH. Install it (e.g. "
                "`brew install tesseract`) to use this optional fallback engine "
                "— per spec, Tesseract is never the primary OCR engine."
            )

    def _import_pytesseract(self):
        try:
            import pytesseract
        except ImportError as exc:
            raise TesseractUnavailableError(
                "pytesseract is not installed. Install the optional extra: "
                f"pip install -e '.[tesseract]'. Original error: {exc}"
            ) from exc
        return pytesseract

    def run(self, image_bytes: bytes, config: OcrConfig) -> OcrResult:
        self._require_binary()
        pytesseract = self._import_pytesseract()

        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(
            image, lang=config.lang, output_type=pytesseract.Output.DICT
        )

        lines: list[OcrLine] = []
        for i, text in enumerate(data["text"]):
            text = text.strip()
            confidence = float(data["conf"][i])
            if not text or confidence < _MIN_CONFIDENCE:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            lines.append(
                OcrLine(
                    text=text,
                    confidence=confidence / 100.0,
                    bbox=BoundingBox(points=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]),
                    reading_order_index=len(lines),
                )
            )

        if not lines:
            return OcrResult(
                engine_name=self.NAME, model_version=self.MODEL_VERSION, lang=config.lang
            )

        return OcrResult(
            engine_name=self.NAME,
            model_version=self.MODEL_VERSION,
            lang=config.lang,
            success=True,
            text=" ".join(line.text for line in lines),
            mean_confidence=sum(line.confidence for line in lines) / len(lines),
            lines=lines,
        )
