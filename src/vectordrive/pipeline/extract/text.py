"""Plain-text and Markdown extraction: read the whole file as UTF-8, treated
as a single logical page — both formats already are the "extracted text",
there's no OCR-relevant distinction between the two at this stage.
"""
from __future__ import annotations

from pathlib import Path

from vectordrive.pipeline.extract.base import ExtractedDocument, ExtractedPage


class TextExtractor:
    NAME = "text"
    VERSION = "1"

    def extract(self, path: Path) -> ExtractedDocument:
        text = Path(path).read_text(encoding="utf-8")
        page = ExtractedPage(page_number=1, text=text, text_source="native", needs_ocr=False)
        return ExtractedDocument(
            extractor_name=self.NAME, extractor_version=self.VERSION, page_count=1, pages=[page]
        )
