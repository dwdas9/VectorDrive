"""Standalone image files (JPG/PNG) have no "extraction" step distinct from
OCR itself — the whole file always needs_ocr. This extractor just produces
the single-page shape the rest of the pipeline expects, so the same
needs_ocr-driven OCR stage (M5) handles image files and image-only PDF
pages uniformly.
"""
from __future__ import annotations

from pathlib import Path

from vectordrive.pipeline.extract.base import ExtractedDocument, ExtractedPage


class ImageExtractor:
    NAME = "image"
    VERSION = "1"

    def extract(self, path: Path) -> ExtractedDocument:
        page = ExtractedPage(page_number=1, text="", text_source="none", needs_ocr=True)
        return ExtractedDocument(
            extractor_name=self.NAME, extractor_version=self.VERSION, page_count=1, pages=[page]
        )
