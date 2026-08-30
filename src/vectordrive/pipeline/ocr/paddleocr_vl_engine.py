"""Tier 3 of the OCR pipeline: PaddleOCR-VL, for difficult scans, tables,
handwriting, complex layouts, and pages where RapidOCR's confidence was too
low.

Apple Silicon compatibility was verified live during development, not
assumed from docs: `paddlepaddle==3.3.1` installs a real
`macosx_11_0_arm64` wheel, `paddle.utils.run_check()` passes CPU inference,
and `paddleocr.PaddleOCRVL(vl_rec_backend="native")` — PaddleOCR-VL's
official PaddlePaddle-CPU-inference backend, requiring paddlepaddle>=3.2.1
— successfully loaded the real PaddleOCR-VL-1.6 (0.9B param) model and
recognized text from both synthetic fixture images (confirmed spacing was
notably better than RapidOCR's: "Synthetic PNG for OCR" vs RapidOCR's
"SyntheticPNGforOCR"). Cold init ~50s (downloads ~1.9GB to
~/.paddlex/official_models, outside the repo, cached thereafter); warm
predict ~7s on this M3 Max.

PaddleOCRVLBlock has no per-block confidence score (unlike RapidOCR) — the
tiered policy's confidence-threshold gate does not apply *to* this tier's
own output; OcrLine.confidence stays None here, honestly, rather than
faking a number.

If paddleocr/paddlepaddle aren't installed, or the pipeline fails to
initialize for any other reason (missing paddlex[ocr] extra, incompatible
hardware, etc.), this raises PaddleOcrVlUnavailableError with the *exact*
underlying error — requirement 11: never silently swallowed or replaced.
run_ocr_with_cache turns that into a clearly-recorded OcrResult failure.
"""
from __future__ import annotations

import io

from vectordrive.pipeline.ocr.base import BoundingBox, OcrConfig, OcrLine, OcrResult

_PIPELINE_VERSION = "1.6"
_BACKEND = "native"


class PaddleOcrVlUnavailableError(RuntimeError):
    """PaddleOCR-VL could not be used — message carries the exact blocker."""


def _import_paddleocr_vl():
    """Module-level indirection point so tests can monkeypatch import
    failures without needing paddleocr actually uninstalled.
    """
    from paddleocr import PaddleOCRVL

    return PaddleOCRVL


class PaddleOcrVlEngine:
    NAME = "paddleocr-vl"
    MODEL_VERSION = f"PaddleOCR-VL-{_PIPELINE_VERSION}-{_BACKEND}"

    def __init__(self):
        self._pipeline = None  # lazy: never load the model at construction time

    def _get_pipeline(self):
        if self._pipeline is None:
            try:
                paddle_ocr_vl_cls = _import_paddleocr_vl()
            except Exception as exc:
                raise PaddleOcrVlUnavailableError(
                    "PaddleOCR-VL is not installed/importable. Install the "
                    "optional extra: pip install -e '.[paddleocr-vl]' "
                    f"(paddlepaddle>=3.2.1, paddleocr, paddlex[ocr]). "
                    f"Original error: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                self._pipeline = paddle_ocr_vl_cls(vl_rec_backend=_BACKEND)
            except Exception as exc:
                raise PaddleOcrVlUnavailableError(
                    f"PaddleOCR-VL pipeline failed to initialize (backend={_BACKEND!r}). "
                    f"Original error: {type(exc).__name__}: {exc}"
                ) from exc
        return self._pipeline

    def run(self, image_bytes: bytes, config: OcrConfig) -> OcrResult:
        import numpy as np
        from PIL import Image

        pipeline = self._get_pipeline()
        array = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        results = list(pipeline.predict(array))

        if not results or not results[0]["parsing_res_list"]:
            return OcrResult(
                engine_name=self.NAME, model_version=self.MODEL_VERSION, lang=config.lang
            )

        blocks = results[0]["parsing_res_list"]
        lines: list[OcrLine] = []
        for index, block in enumerate(blocks):
            points = None
            polygon = getattr(block, "polygon_points", None)
            if polygon is not None and len(polygon) > 0:
                points = [(float(x), float(y)) for x, y in polygon]
            elif block.bbox:
                x0, y0, x1, y1 = block.bbox
                points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            lines.append(
                OcrLine(
                    text=block.content or "",
                    confidence=None,  # PaddleOCRVLBlock exposes no per-block score
                    bbox=BoundingBox(points=points) if points else None,
                    reading_order_index=index,
                )
            )

        return OcrResult(
            engine_name=self.NAME,
            model_version=self.MODEL_VERSION,
            lang=config.lang,
            success=True,
            text="\n".join(line.text for line in lines),
            mean_confidence=None,
            lines=lines,
        )
