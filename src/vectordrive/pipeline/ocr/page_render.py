"""Renders a PDF page to PNG bytes so image-only pages can be fed to the
OCR stage. The rendered bytes (not the PDF file's own hash) are what get
hashed for the OCR cache key, so changing DPI correctly busts the cache.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz  # see pipeline/extract/pdf.py's import comment: 'import
# fitz' is deprecated and prints to stdout on newer pymupdf releases.

DEFAULT_DPI = 200


def render_page_to_image(path: Path, page_number: int, dpi: int = DEFAULT_DPI) -> bytes:
    """Render `page_number` (1-indexed) of the PDF at `path` to PNG bytes."""
    doc = fitz.open(path)
    try:
        if not (1 <= page_number <= doc.page_count):
            raise ValueError(
                f"page_number={page_number} out of range for {path} "
                f"({doc.page_count} pages)"
            )
        page = doc[page_number - 1]
        pixmap = page.get_pixmap(dpi=dpi)
        return pixmap.tobytes("png")
    finally:
        doc.close()
