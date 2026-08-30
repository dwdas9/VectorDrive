"""Shared types for the extraction stage: file -> ExtractedDocument.

Every extractor implementation (pdf.py, docx.py, text.py, ...) exposes the
same small surface: NAME, VERSION class attributes (together they form half
of the extraction-cache key; the other half is the file's content_hash) and
an extract(path) -> ExtractedDocument method.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


DOCUMENT_FAILURE_ENCRYPTED = "encrypted"
DOCUMENT_FAILURE_CORRUPTED = "corrupted"


class DocumentUnreadableError(RuntimeError):
    """A supported file could not be turned into at least one extractable page.

    Raised by an extractor (or by a Phase 1 defense-in-depth guard) for cases
    like an encrypted / password-protected PDF, or a PDF whose object streams
    are corrupt enough that MuPDF only opens it in repair mode with zero
    pages. A supported document with zero extracted pages must never become a
    successful, searchable sidecar — this is the explicit, fail-closed signal
    that replaces that silent empty success.

    ``label`` is a stable, machine-readable failure category —
    ``"encrypted"`` (``DOCUMENT_FAILURE_ENCRYPTED``) for a password-protected
    document VectorDrive will not attempt to unlock, or ``"corrupted"``
    (``DOCUMENT_FAILURE_CORRUPTED``) for a malformed / truncated / zero-page
    document. ``labelled_message()`` prefixes it onto the human message
    (``"encrypted: ..."`` / ``"corrupted: ..."``); Phase 1 stores that string
    in ``files.markdown_error`` and persists no sidecar.

    The message is authored to be content-safe: it never contains the file
    path, any page text, or the raw underlying library error string (a
    PyMuPDF error can embed the filename).
    """

    def __init__(self, message: str, *, label: str) -> None:
        super().__init__(message)
        self.label = label

    def labelled_message(self) -> str:
        return f"{self.label}: {self}"


@dataclass
class ExtractedPage:
    """One page's worth of extraction output.

    text_source is 'native' when the extractor itself produced usable text
    (PyMuPDF's embedded-text path, or the whole-document text extractors),
    or 'none' when the page's text isn't resolved yet and needs_ocr is True.
    `text` may still be non-empty in the 'none' case — page-composition
    routing (pipeline/extract/pdf.py) keeps whatever native text it found
    (e.g. a watermark) even on a page it's routing to OCR, so OCR can merge
    it in rather than discarding it. OCR (M5) resolves 'none' to 'ocr' (no
    native text existed) or 'merged' (native text existed alongside it).
    """

    page_number: int  # 1-indexed
    text: str
    text_source: str  # "native" | "ocr" | "merged" | "none"
    needs_ocr: bool = False

    # Phase 1 page-level PDF router provenance (pipeline/extract/pdf_page_router.py).
    # Only the Phase-1 PDF extractor populates these; every other extractor
    # leaves them None and they persist as NULL (legacy/back-compatible).
    routing_fingerprint: str | None = None
    routing_action: str | None = None
    routing_visible_chars: int | None = None
    routing_invisible_chars: int | None = None
    routing_max_image_coverage: float | None = None
    routing_summed_image_coverage: float | None = None
    # Exact union footprint of clipped placed-image rectangles (0..1), or
    # None when the rect set is pathological (>512). Summed coverage above
    # is diagnostic only and never independently forces OCR.
    routing_union_image_coverage: float | None = None
    routing_image_instance_count: int | None = None
    routing_font_count: int | None = None
    routing_vector_drawing_count: int | None = None
    routing_inline_image_count: int | None = None
    routing_replacement_char_ratio: float | None = None
    routing_control_char_ratio: float | None = None
    # Original extracted native/hidden text, kept durably separate from the
    # resolved page text (which OCR later overwrites).
    routing_native_text: str | None = None


@dataclass
class ExtractedDocument:
    extractor_name: str
    extractor_version: str
    page_count: int
    pages: list[ExtractedPage] = field(default_factory=list)


class Extractor(Protocol):
    NAME: str
    VERSION: str

    def extract(self, path: Path) -> ExtractedDocument: ...
