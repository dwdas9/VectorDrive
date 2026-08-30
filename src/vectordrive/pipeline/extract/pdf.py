"""Tier 1 of the OCR pipeline: use PyMuPDF's embedded text directly when a
PDF page's *composition* says the native text is trustworthy, and flag the
rest needs_ocr for the tiered OCR pipeline (M5) to pick up.

VERSION 1 routed on native character count alone (`len(text) >= 20`). That
missed pages where a short watermark ("Scanned by CamScanner") or footer
sits on top of an otherwise raster-only page: 20+ characters of real text
made the page look native even though the actual page content was an
unrecognized image. VERSION 2 routes on page composition instead: raster
image coverage and a garbled-text ratio both override a native character
count that would otherwise look "good enough" alone. When the signals
disagree, this deliberately biases toward OCRing — at this pipeline's scale
a wasted OCR pass is cheap, a silently-skipped document is not.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz  # PyMuPDF's modern import name — 'import fitz' is the
# deprecated compat shim, which prints a "warning: ..." line to STDOUT (not
# via the warnings module, so no filter suppresses it) on newer pymupdf
# releases (observed on 1.28.2; not on this dev venv's pinned 1.28.0) — found
# during M12's clean-install validation, and fatal for the MCP server's
# stdio protocol if it happened during `vectordrive mcp` startup.

from vectordrive.pipeline.extract.base import ExtractedDocument, ExtractedPage

# The Unicode replacement character: PyMuPDF's own signal that a glyph in
# the page's font couldn't be mapped to a real character. Native text that's
# mostly this isn't text, it's noise, and needs OCR like an image page.
_REPLACEMENT_CHAR = "�"
# C0 control characters are never legitimate page content either, except
# the common whitespace ones every real PDF text stream contains.
_ALLOWED_CONTROL_CHARS = "\t\n\r\x0c"


class PdfExtractor:
    NAME = "pymupdf"
    VERSION = "2"  # composition-based routing (was a bare char-count check)

    # -- Page composition thresholds. Deliberately biased toward OCR when
    # uncertain: wasted OCR compute is free at this corpus's size; a missed
    # document is permanent. --

    # Below this many non-whitespace characters, native text alone is never
    # "the whole page" — it reads as a watermark/footer/stamp at best.
    MIN_NATIVE_CHARS = 20

    # A page whose raster images cover at least this fraction of the page
    # area is raster-dominant regardless of how much native text also sits
    # on it. This is precisely what a bare character count missed: a
    # >=20-char watermark over a full-page scan looked identical to a real
    # text page under VERSION 1.
    RASTER_AREA_THRESHOLD = 0.30

    # A page whose native text is at least this fraction replacement/control
    # characters has a broken text layer — OCR the rendered page instead of
    # trusting it.
    GARBLED_RATIO_THRESHOLD = 0.02

    def extract(self, path: Path) -> ExtractedDocument:
        doc = fitz.open(path)
        try:
            pages = [self._extract_page(page, index) for index, page in enumerate(doc, start=1)]
            return ExtractedDocument(
                extractor_name=self.NAME,
                extractor_version=self.VERSION,
                page_count=doc.page_count,
                pages=pages,
            )
        finally:
            doc.close()

    def _extract_page(self, page: fitz.Page, index: int) -> ExtractedPage:
        text = page.get_text()
        raster_area_fraction = self._raster_area_fraction(page)
        garbled_ratio = self._garbled_ratio(text)
        has_enough_native_text = len(text.strip()) >= self.MIN_NATIVE_CHARS

        needs_ocr = (
            raster_area_fraction >= self.RASTER_AREA_THRESHOLD
            or garbled_ratio >= self.GARBLED_RATIO_THRESHOLD
            or not has_enough_native_text
        )

        if needs_ocr:
            # Keep whatever native text exists rather than discarding it —
            # OCR (M5) merges it in. text_source stays 'none' until OCR
            # resolves it to 'ocr' or 'merged' depending on whether this
            # text ends up non-empty.
            return ExtractedPage(page_number=index, text=text, text_source="none", needs_ocr=True)
        return ExtractedPage(page_number=index, text=text, text_source="native", needs_ocr=False)

    @staticmethod
    def _raster_area_fraction(page: fitz.Page) -> float:
        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return 0.0
        total = 0.0
        for image in page.get_images(full=True):
            xref = image[0]
            for rect in page.get_image_rects(xref):
                clipped = rect & page.rect  # an image can extend past the page
                total += clipped.width * clipped.height
        return min(total / page_area, 1.0)

    @staticmethod
    def _garbled_ratio(text: str) -> float:
        if not text:
            return 0.0
        garbled = sum(1 for ch in text if PdfExtractor._is_garbled_char(ch))
        return garbled / len(text)

    @staticmethod
    def _is_garbled_char(ch: str) -> bool:
        if ch == _REPLACEMENT_CHAR:
            return True
        return ord(ch) < 0x20 and ch not in _ALLOWED_CONTROL_CHARS
