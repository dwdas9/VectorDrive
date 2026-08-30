"""Phase 1 page-level PDF extractor / router.

This is used **only** by Phase 1 "Create Markdown" (see
``services/markdown_generation_service.py``). Legacy direct indexing keeps
using ``pipeline/extract/pdf.py``'s ``PdfExtractor`` unchanged.

Each PDF page is routed to one of three extraction actions using purely
*structural* evidence taken from PyMuPDF — never the producer/creator
metadata strings, which are logged for diagnostics but must never decide
routing:

- ``native``                -> trust the embedded text, no Qwen
- ``native_plus_qwen_page``  -> keep the embedded text verbatim *and* run a
  full-page Qwen transcription, both preserved byte-for-byte, separated
  only by fixed machine-readable HTML comments (no dedup / rewrite)
- ``qwen_page``             -> Qwen transcription is canonical; any hidden /
  watermark / OCR text layer already on the page is diagnostic only and is
  kept in ``routing_native_text`` (never in the searchable page text)

The structural fingerprints and their thresholds are kept deliberately
consistent with ``scripts/pdf_fingerprint_census.py`` (the research tool
this slice productionises). That module is a standalone research script and
is intentionally *not* imported here.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pymupdf as fitz  # see pipeline/extract/pdf.py for why not "import fitz"

from vectordrive.pipeline.extract.base import (
    DOCUMENT_FAILURE_CORRUPTED,
    DOCUMENT_FAILURE_ENCRYPTED,
    DocumentUnreadableError,
    ExtractedDocument,
    ExtractedPage,
)

logger = logging.getLogger(__name__)

_REPLACEMENT_CHAR = "�"
_ALLOWED_CONTROL_CHARS = "\t\n\r\x0c"

# Inline-image (BI ... ID ... EI) operator, word-boundary matched exactly as
# the census script does.
_OP_BI = re.compile(rb"(?<![A-Za-z])BI(?![A-Za-z])")

# --- routing actions -----------------------------------------------------
ACTION_NATIVE = "native"
ACTION_NATIVE_PLUS_QWEN = "native_plus_qwen_page"
ACTION_QWEN_PAGE = "qwen_page"

# Fingerprint -> action. Conservative: anything that is not a clean
# born-digital page, or a clean born-digital page with a bounded image
# region, is sent (at least in part) to Qwen.
_ACTION_FOR_FINGERPRINT = {
    "bitmap_glyph_candidate": ACTION_QWEN_PAGE,
    "garbled_text_layer": ACTION_QWEN_PAGE,
    "full_page_raster_with_hidden_text": ACTION_QWEN_PAGE,
    "full_page_raster_without_text": ACTION_QWEN_PAGE,
    "full_page_raster_with_text": ACTION_QWEN_PAGE,
    # Many bounded image regions that *together* blanket the page. Native
    # text is only trustworthy enough to keep alongside Qwen when it is
    # substantial, mostly-visible and non-garbled.
    "multi_image_full_coverage_with_trusted_text": ACTION_NATIVE_PLUS_QWEN,
    "multi_image_full_coverage": ACTION_QWEN_PAGE,
    "visible_text_low_raster": ACTION_NATIVE,
    "visible_text_with_image_region": ACTION_NATIVE_PLUS_QWEN,
    "hidden_text_layer_other": ACTION_QWEN_PAGE,
    "ambiguous": ACTION_QWEN_PAGE,
}

# A replacement-char or disallowed-control-char ratio at or above this makes
# the embedded text layer untrustworthy: it must never be routed ``native``.
_GARBLED_RATIO = 0.02


@dataclass(frozen=True)
class PageRoutingPlan:
    """The structural evidence and resolved routing for one PDF page."""

    page_number: int
    fingerprint: str
    action: str
    visible_chars: int
    invisible_chars: int
    max_image_coverage: float
    summed_image_coverage: float
    union_image_coverage: float | None
    image_instance_count: int
    font_count: int
    vector_drawing_count: int
    inline_image_count: int
    replacement_char_ratio: float
    control_char_ratio: float
    native_text: str

    def provenance_scalars(self) -> dict[str, object]:
        """JSON-safe scalar provenance for durable storage / page markers.

        Deliberately excludes ``native_text`` (kept in its own column, never
        surfaced as metadata).
        """
        return {
            "routing_fingerprint": self.fingerprint,
            "extraction_action": self.action,
            "routing_visible_chars": self.visible_chars,
            "routing_invisible_chars": self.invisible_chars,
            "routing_max_image_coverage": round(self.max_image_coverage, 6),
            "routing_summed_image_coverage": round(self.summed_image_coverage, 6),
            "routing_union_image_coverage": (
                None
                if self.union_image_coverage is None
                else round(self.union_image_coverage, 6)
            ),
            "routing_image_instance_count": self.image_instance_count,
            "routing_font_count": self.font_count,
            "routing_vector_drawing_count": self.vector_drawing_count,
            "routing_inline_image_count": self.inline_image_count,
            "routing_replacement_char_ratio": round(self.replacement_char_ratio, 6),
            "routing_control_char_ratio": round(self.control_char_ratio, 6),
        }


def _text_trace_counts(page: fitz.Page) -> tuple[int, int]:
    visible = 0
    invisible = 0
    for span in page.get_texttrace():
        char_count = len(span.get("chars", ()))
        if span.get("type") == 3:  # render mode 3 == invisible
            invisible += char_count
        else:
            visible += char_count
    return visible, invisible


def _clipped_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        clipped = fitz.Rect(bbox) & page.rect
        if clipped.width > 0 and clipped.height > 0:
            rects.append(clipped)
    return rects


def _rectangle_union_area(rects: Sequence[fitz.Rect]) -> float | None:
    """Exact union area via a vertical sweep line.

    Bounded exactly as ``scripts/pdf_fingerprint_census.py``: computed
    exactly for ordinary pages (<= 512 placed-image rectangles) and returned
    as ``None`` for pathological sets (e.g. bitmap-glyph pages with thousands
    of tiny rectangles), where the caller treats union coverage as unknown.
    """
    if not rects:
        return 0.0
    if len(rects) > 512:
        return None

    x_values = sorted({coord for rect in rects for coord in (rect.x0, rect.x1)})
    total = 0.0
    for left, right in zip(x_values, x_values[1:]):
        width = right - left
        if width <= 0:
            continue
        intervals = sorted(
            (rect.y0, rect.y1) for rect in rects if rect.x0 < right and rect.x1 > left
        )
        if not intervals:
            continue
        covered_y = 0.0
        start, end = intervals[0]
        for next_start, next_end in intervals[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                covered_y += end - start
                start, end = next_start, next_end
        covered_y += end - start
        total += width * covered_y
    return total


@dataclass(frozen=True)
class _ImageEvidence:
    max_coverage: float
    summed_coverage: float
    union_coverage: float | None
    instance_count: int


def _image_evidence(page: fitz.Page) -> _ImageEvidence:
    page_area = page.rect.get_area()
    rects = _clipped_image_rects(page)
    if page_area <= 0:
        return _ImageEvidence(0.0, 0.0, None, len(rects))
    if not rects:
        return _ImageEvidence(0.0, 0.0, 0.0, 0)
    coverages = [rect.get_area() / page_area for rect in rects]
    union_area = _rectangle_union_area(rects)
    union_coverage = (
        None if union_area is None else min(union_area / page_area, 1.0)
    )
    return _ImageEvidence(
        max_coverage=max(coverages),
        summed_coverage=min(sum(coverages), 1.0),
        union_coverage=union_coverage,
        instance_count=len(rects),
    )


def _noise_ratios(text: str) -> tuple[float, float]:
    if not text:
        return 0.0, 0.0
    replacement = text.count(_REPLACEMENT_CHAR) / len(text)
    controls = sum(
        1 for ch in text if ord(ch) < 0x20 and ch not in _ALLOWED_CONTROL_CHARS
    )
    return replacement, controls / len(text)


def _inline_image_count(doc: fitz.Document, page: fitz.Page) -> int:
    content = bytearray()
    for xref in page.get_contents():
        try:
            content.extend(doc.xref_stream(xref))
            content.extend(b"\n")
        except (RuntimeError, ValueError):
            continue
    return len(_OP_BI.findall(bytes(content)))


def _fingerprint(
    *,
    non_whitespace_chars: int,
    visible: int,
    invisible: int,
    max_image: float,
    summed_image: float,
    font_count: int,
    inline_images: int,
    union_image: float | None = None,
    image_instances: int = 0,
    replacement_ratio: float = 0.0,
    control_ratio: float = 0.0,
) -> str:
    """Conservative structural hint, kept consistent with the census script.

    Routing rules (see module docstring and the Codex-review corrections):

    - Summed image coverage is **diagnostic only**. It can never on its own
      send a page to ``qwen_page``; the census shows pages whose summed
      coverage is inflated by repeated / overlapping image placements while
      the true (union) footprint is small and the native text authoritative.
    - Exact *union* image coverage is the full-raster signal for multi-image
      pages. When it is unavailable (pathological rect set -> ``None``) only
      summed coverage remains: it is then used solely to *raise* the
      footprint estimate for preserving native text, never to discard it.
    - A garbled embedded text layer (replacement / disallowed-control ratio
      >= 2%) is never trusted as ``native``.
    """
    trace_total = visible + invisible
    invisible_ratio = (invisible / trace_total) if trace_total else 0.0
    garbled = replacement_ratio >= _GARBLED_RATIO or control_ratio >= _GARBLED_RATIO
    trustworthy_visible = visible >= 20 and invisible_ratio < 0.10 and not garbled

    # When the exact union is unknown, fall back to summed coverage *only*
    # as an upper-bound footprint estimate (never as an independent
    # qwen_page trigger — trustworthy text below still routes native_plus).
    if union_image is not None:
        effective_union = union_image
    elif summed_image >= 0.95:
        effective_union = summed_image
    else:
        effective_union = 0.0

    if inline_images >= 100 and font_count == 0 and non_whitespace_chars < 20:
        return "bitmap_glyph_candidate"

    if garbled and non_whitespace_chars >= 20:
        return "garbled_text_layer"

    if max_image >= 0.85:
        if invisible_ratio >= 0.50 and invisible >= 20:
            return "full_page_raster_with_hidden_text"
        if non_whitespace_chars < 20:
            return "full_page_raster_without_text"
        return "full_page_raster_with_text"

    if effective_union >= 0.95:
        if trustworthy_visible:
            return "multi_image_full_coverage_with_trusted_text"
        return "multi_image_full_coverage"

    if trustworthy_visible and (max_image >= 0.20 or effective_union >= 0.20):
        return "visible_text_with_image_region"

    if trustworthy_visible:
        return "visible_text_low_raster"

    if invisible_ratio >= 0.50 and invisible >= 20:
        return "hidden_text_layer_other"

    return "ambiguous"


def route_page(doc: fitz.Document, page: fitz.Page, page_number: int) -> PageRoutingPlan:
    # PyMuPDF exposes no verbatim *visible-only* plain-text extraction:
    # get_text("text") returns every span, including render-mode-3
    # (invisible) ones. get_texttrace() gives us the per-span render mode,
    # which is why invisible chars are counted separately below — but the
    # `native_text` string kept here can still contain invisible spans.
    # This slice does not attempt a lossy visible-only reconstruction; the
    # invisible-ratio guard in `_fingerprint` (trustworthy text requires
    # invisible_ratio < 0.10) plus the qwen_page fallback are the safety
    # boundary for hidden/OCR text layers.
    text = page.get_text("text", sort=True)
    non_whitespace_chars = sum(1 for ch in text if not ch.isspace())
    visible, invisible = _text_trace_counts(page)
    images = _image_evidence(page)
    font_count = len({font[0] for font in page.get_fonts(full=True)})
    vector_drawings = len(page.get_drawings())
    inline_images = _inline_image_count(doc, page)
    replacement_ratio, control_ratio = _noise_ratios(text)

    fingerprint = _fingerprint(
        non_whitespace_chars=non_whitespace_chars,
        visible=visible,
        invisible=invisible,
        max_image=images.max_coverage,
        summed_image=images.summed_coverage,
        union_image=images.union_coverage,
        image_instances=images.instance_count,
        font_count=font_count,
        inline_images=inline_images,
        replacement_ratio=replacement_ratio,
        control_ratio=control_ratio,
    )
    action = _ACTION_FOR_FINGERPRINT[fingerprint]

    return PageRoutingPlan(
        page_number=page_number,
        fingerprint=fingerprint,
        action=action,
        visible_chars=visible,
        invisible_chars=invisible,
        max_image_coverage=images.max_coverage,
        summed_image_coverage=images.summed_coverage,
        union_image_coverage=images.union_coverage,
        image_instance_count=images.instance_count,
        font_count=font_count,
        vector_drawing_count=vector_drawings,
        inline_image_count=inline_images,
        replacement_char_ratio=replacement_ratio,
        control_char_ratio=control_ratio,
        native_text=text,
    )


class Phase1PdfExtractor:
    """Phase-1-only PDF extractor that routes each page structurally.

    NAME/VERSION are deliberately distinct from ``PdfExtractor`` so Phase 1's
    ``extracted_docs``/``pages`` rows can never be confused with — or reused
    from — legacy indexing's, and so old Phase 1 cached rows produced before
    this router cannot masquerade as its output.
    """

    NAME = "pymupdf-page-router"
    VERSION = "1"

    def extract(self, path: Path) -> ExtractedDocument:
        # Every failure below is surfaced as DocumentUnreadableError with a
        # fixed, content-safe message: no path, no page text, and never the
        # raw MuPDF error string (which can embed the filename). The
        # underlying exception is chained (`from exc`) for local debugging
        # only — callers store `str(exc)` of the DocumentUnreadableError.
        try:
            doc = fitz.open(path)
        except Exception as exc:  # FileDataError, RuntimeError, ...
            raise DocumentUnreadableError(
                "PDF could not be opened: the file is malformed or truncated",
                label=DOCUMENT_FAILURE_CORRUPTED,
            ) from exc

        try:
            # Encrypted / password-protected: MuPDF opens the file but leaves
            # it locked (needs_pass stays True until authentication, which we
            # never attempt). Any page access from here would raise deep in
            # PyMuPDF — check explicitly instead of relying on that.
            if doc.needs_pass:
                raise DocumentUnreadableError(
                    "PDF is password-protected; VectorDrive never attempts passwords",
                    label=DOCUMENT_FAILURE_ENCRYPTED,
                )

            try:
                page_count = doc.page_count
            except Exception as exc:
                raise DocumentUnreadableError(
                    "PDF page structure is unreadable (corrupt object streams)",
                    label=DOCUMENT_FAILURE_CORRUPTED,
                ) from exc
            if page_count < 1:
                # MuPDF opened the file in repair mode and recovered no pages
                # (corrupt object streams, truncated body, empty page tree).
                raise DocumentUnreadableError(
                    "PDF contains no pages (corrupt or truncated document)",
                    label=DOCUMENT_FAILURE_CORRUPTED,
                )

            try:
                pages = [
                    self._route(doc, page, index, path)
                    for index, page in enumerate(doc, start=1)
                ]
            except DocumentUnreadableError:
                raise
            except Exception as exc:
                raise DocumentUnreadableError(
                    "PDF page contents could not be parsed (corrupt object streams)",
                    label=DOCUMENT_FAILURE_CORRUPTED,
                ) from exc

            if not pages:
                raise DocumentUnreadableError(
                    "PDF yielded no readable pages (corrupt or truncated document)",
                    label=DOCUMENT_FAILURE_CORRUPTED,
                )

            return ExtractedDocument(
                extractor_name=self.NAME,
                extractor_version=self.VERSION,
                page_count=len(pages),
                pages=pages,
            )
        finally:
            doc.close()

    def _route(
        self, doc: fitz.Document, page: fitz.Page, index: int, source_path: Path
    ) -> ExtractedPage:
        plan = route_page(doc, page, index)
        # source_path is the exact on-disk path (explicit operator
        # requirement). Page text is never logged — only structural scalars.
        logger.info(
            "event=pdf_page_routing source_path=%s page=%d fingerprint=%s action=%s "
            "visible=%d invisible=%d max_image=%.4f summed_image=%.4f "
            "union_image=%s image_instances=%d fonts=%d vectors=%d "
            "inline_images=%d replacement_ratio=%.4f control_ratio=%.4f",
            str(source_path),
            plan.page_number,
            plan.fingerprint,
            plan.action,
            plan.visible_chars,
            plan.invisible_chars,
            plan.max_image_coverage,
            plan.summed_image_coverage,
            "none" if plan.union_image_coverage is None
            else f"{plan.union_image_coverage:.4f}",
            plan.image_instance_count,
            plan.font_count,
            plan.vector_drawing_count,
            plan.inline_image_count,
            plan.replacement_char_ratio,
            plan.control_char_ratio,
        )

        common = dict(
            page_number=index,
            routing_fingerprint=plan.fingerprint,
            routing_action=plan.action,
            routing_visible_chars=plan.visible_chars,
            routing_invisible_chars=plan.invisible_chars,
            routing_max_image_coverage=plan.max_image_coverage,
            routing_summed_image_coverage=plan.summed_image_coverage,
            routing_union_image_coverage=plan.union_image_coverage,
            routing_image_instance_count=plan.image_instance_count,
            routing_font_count=plan.font_count,
            routing_vector_drawing_count=plan.vector_drawing_count,
            routing_inline_image_count=plan.inline_image_count,
            routing_replacement_char_ratio=plan.replacement_char_ratio,
            routing_control_char_ratio=plan.control_char_ratio,
            routing_native_text=plan.native_text,
        )

        if plan.action == ACTION_NATIVE:
            return ExtractedPage(
                text=plan.native_text, text_source="native", needs_ocr=False, **common
            )

        if plan.action == ACTION_NATIVE_PLUS_QWEN:
            # Native text is preserved (also mirrored in routing_native_text so
            # it survives OCR overwriting pages.text); page_ocr combines it
            # with the Qwen Markdown using fixed comment delimiters.
            return ExtractedPage(
                text=plan.native_text, text_source="none", needs_ocr=True, **common
            )

        # qwen_page: the existing text layer is diagnostic only. Keep it out
        # of pages.text entirely so it can never reach searchable text.
        return ExtractedPage(text="", text_source="none", needs_ocr=True, **common)
