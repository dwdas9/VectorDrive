"""Deterministic per-page OCR quality assessment and manual-review routing
(requirements 2-4 of the OCR-quality-review slice).

Pure functions only: every input here is a technical signal already
resolved by extraction/OCR (mode, resolved text, OCR confidence/success,
line bounding boxes). This module never receives a filename, a file path,
or any other content-adjacent identity signal, and never inspects page
text for semantic meaning (e.g. it never looks for words like "passport")
— only structural/geometric properties of the *output* (character/word/
line counts, confidence, garbled-character ratio, bounding-box geometry,
and resolved-text-vs-reported-lines correspondence). This is deliberate:
requirement 4 forbids inferring "this looks like an identity document"
from private content or filename semantics, and the only way to guarantee
that is to never give this module the information it would take to do so.

Three decisions are represented (requirement 4):
  - ``DECISION_AUTO_ACCEPTED``: passed a deliberately conservative,
    multi-signal gate (requirement 3) — never granted on mean confidence
    alone, and never granted to a page whose orientation could not be
    verified at all.
  - ``DECISION_MANUAL_REVIEW``: eligible for human/manual review; the
    accompanying reasons say exactly why.
  - ``DECISION_EXTRACTION_FAILED``: OCR failed outright, or nothing was
    resolved for the page at all.

Every reason carries a stable machine-readable code (``ReasonEntry.code``)
plus a human-readable message (``ReasonEntry.message``) — requirement 4.

A note on orientation: bounding-box geometry (tall/narrow vs. wide/short
line boxes) detects a *text-layout* orientation risk in the resolved
output. It cannot guarantee the original source image's orientation —
many OCR engines auto-rotate internally before ever reporting geometry,
so a "clean" geometry read only proves the text layout the engine
produced looks right, not that no rotation ever happened upstream of it.
Missing or insufficient bounding-box geometry is treated as unverified,
never as "presumed fine" (see ``ORIENTATION_UNVERIFIED``).
"""
from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from vectordrive.pipeline.extract.pdf import PdfExtractor
from vectordrive.pipeline.ocr.base import OcrLine
from vectordrive.pipeline.ocr.merge import _normalize, _tokens

DECISION_AUTO_ACCEPTED = "auto_accepted"
DECISION_MANUAL_REVIEW = "manual_review_recommended"
DECISION_EXTRACTION_FAILED = "extraction_failed"

_VALID_MODES = frozenset({"native", "ocr", "merged", "failed", "skipped"})
_OCR_MODES = frozenset({"ocr", "merged"})

# -- Thresholds. All named and all included verbatim in the persisted
# `thresholds` payload (requirement 3) so a review decision is always
# reproducible from what's stored, not from reading this source file. --

#: A full-page/mixed OCR page needs at least this mean OCR confidence to
#: even be *considered* for auto-accept. Deliberately very high (not the
#: 0.92 this module shipped with first) — reviewer correction: auto-accept
#: must mean VERY VERY HIGH extraction, not "confident enough". Necessary
#: but never sufficient alone (requirement 3: multi-signal, not
#: confidence-only) — see CONFIDENCE_BELOW_AUTO_ACCEPT_BAR/
#: NO_CONFIDENCE_SIGNAL below, which also block auto-accept.
MIN_AUTO_ACCEPT_CONFIDENCE = 0.98

#: Below this mean confidence, a page is explicitly flagged LOW_CONFIDENCE
#: (a materially worse tier than merely missing the auto-accept bar) —
#: e.g. 0.95 does not clear MIN_AUTO_ACCEPT_CONFIDENCE but is not
#: LOW_CONFIDENCE either; it gets CONFIDENCE_BELOW_AUTO_ACCEPT_BAR.
LOW_CONFIDENCE_THRESHOLD = 0.80

#: General "some text resolved but nearly nothing" floor, in non-
#: whitespace characters — applies to native pages (which keep their own,
#: simpler logic) and is also the absolute floor for OCR-derived pages
#: below MIN_AUTO_ACCEPT_CHARS.
MIN_SPARSE_OUTPUT_CHARS = 20

#: OCR-derived pages need substantially more resolved content than the
#: bare "not sparse" floor above to be eligible for auto-accept — a scan
#: that resolved to just barely enough to clear MIN_SPARSE_OUTPUT_CHARS is
#: not "VERY VERY HIGH extraction". Reuses the SPARSE_OUTPUT reason code
#: (same underlying concept — too little resolved content — just a
#: stricter bar for OCR-derived pages) rather than inventing a parallel one.
MIN_AUTO_ACCEPT_CHARS = 40

#: OCR-derived pages need at least this many whitespace-separated word
#: tokens to auto-accept — guards against pages whose character count is
#: technically above MIN_AUTO_ACCEPT_CHARS but is really one long
#: non-prose token (a serial number, a hash) rather than real content.
MIN_AUTO_ACCEPT_WORDS = 8

#: OCR-derived pages need at least this many OCR-engine-reported lines to
#: auto-accept. A page whose engine reported only 0-1 lines has too little
#: independent structure to trust without a human glance, regardless of
#: how clean those lines individually look.
MIN_AUTO_ACCEPT_LINES = 2

#: How much the page's final *resolved* text must agree with the raw text
#: the OCR engine reported per line (word-token similarity ratio, 0-1).
#: A low ratio means the resolved text diverged materially from what the
#: engine actually reported — e.g. a merge/post-processing bug, or content
#: attributed to the wrong page — and must not auto-accept even if every
#: other signal looks clean.
MIN_TEXT_LINE_CORRESPONDENCE = 0.85

#: At or above this fraction of replacement/control characters in the
#: resolved text, output is garbled (reuses PdfExtractor's own
#: replacement-char detection rather than redefining "garbled").
MAX_AUTO_ACCEPT_GARBLED_RATIO = 0.01

#: A line's bounding box counts as "rotated" when its pixel height exceeds
#: its width by this multiple — a normal horizontal text line is wide and
#: short; a 90-degree-rotated line's box is the opposite.
ORIENTATION_BBOX_ASPECT_THRESHOLD = 1.4

#: Fraction of a page's (geometry-bearing) OCR lines that must look
#: rotated before the page as a whole is flagged as an orientation risk.
#: A handful of naturally tall glyphs (page numbers, stamps) must not
#: trip this; a majority of lines being tall/narrow is a real signal.
ORIENTATION_ROTATED_LINE_FRACTION = 0.35

#: Below this many geometry-bearing (usable bbox) lines, there isn't
#: enough signal to make *any* orientation claim, positive or negative —
#: this page's orientation is UNVERIFIED, which blocks auto-accept
#: (reviewer correction: "orientation unknown is not orientation safe").
MIN_LINES_FOR_ORIENTATION_CHECK = 3

# Reason codes that block the strict auto-accept gate. FULL_PAGE_SCAN and
# OCR_DERIVED_MIXED_PAGE are deliberately excluded (requirement 3: a full
# scan/mixed page must not be flagged merely for being OCR-derived when
# extraction is demonstrably excellent) — they are informational
# composition context, not a quality risk by themselves.
_BLOCKING_REASON_CODES = frozenset(
    {
        "SPARSE_OUTPUT",
        "LOW_WORD_COUNT",
        "LOW_LINE_COUNT",
        "NO_CONFIDENCE_SIGNAL",
        "LOW_CONFIDENCE",
        "CONFIDENCE_BELOW_AUTO_ACCEPT_BAR",
        "GARBLED_OUTPUT",
        "TEXT_LINE_MISMATCH",
        "ORIENTATION_RISK",
        "ORIENTATION_UNVERIFIED",
    }
)

JsonScalar = str | int | float | bool | None


@dataclass(frozen=True)
class ReasonEntry:
    code: str
    message: str


@dataclass(frozen=True)
class PageQualityAssessment:
    decision: str
    reasons: tuple[ReasonEntry, ...]
    metrics: Mapping[str, JsonScalar]
    thresholds: Mapping[str, JsonScalar]

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)


@dataclass(frozen=True)
class DocumentQualityAssessment:
    decision: str
    reasons: tuple[ReasonEntry, ...]
    metrics: Mapping[str, JsonScalar]
    flagged_page_numbers: tuple[int, ...]

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)


def _bbox_is_rotated(bbox) -> bool | None:
    """None means "no usable geometry" (not "not rotated") — engines that
    don't report bounding boxes must never be silently treated as
    orientation-safe.
    """
    points = getattr(bbox, "points", None) if bbox is not None else None
    if not points or len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width <= 0:
        return None
    return height > width * ORIENTATION_BBOX_ASPECT_THRESHOLD


def _orientation_signals(ocr_lines: Sequence[OcrLine]) -> tuple[bool, bool, int]:
    """Returns (verified, risk, geometry_line_count).

    `verified` is False whenever there isn't enough usable bbox geometry
    to make any claim at all — in that case `risk` is always False too
    (there is nothing to base a risk claim on), and the caller is
    responsible for treating "not verified" as its own blocking signal
    (ORIENTATION_UNVERIFIED), never as "presumed fine".
    """
    flags = [f for f in (_bbox_is_rotated(line.bbox) for line in ocr_lines) if f is not None]
    geometry_line_count = len(flags)
    if geometry_line_count < MIN_LINES_FOR_ORIENTATION_CHECK:
        return False, False, geometry_line_count
    rotated_fraction = sum(1 for f in flags if f) / geometry_line_count
    return True, rotated_fraction >= ORIENTATION_ROTATED_LINE_FRACTION, geometry_line_count


def _text_line_correspondence(resolved_text: str, ocr_lines: Sequence[OcrLine]) -> float | None:
    """Word-token similarity ratio between `resolved_text` and the
    concatenation of what the OCR engine actually reported per line.
    None when there are no lines to compare against at all (that case is
    covered separately by LOW_LINE_COUNT/ORIENTATION_UNVERIFIED).
    """
    if not ocr_lines:
        return None
    concatenated = " ".join(line.text for line in ocr_lines if line.text)
    resolved_tokens = _tokens(_normalize(resolved_text))
    line_tokens = _tokens(_normalize(concatenated))
    if not resolved_tokens and not line_tokens:
        return 1.0
    if not resolved_tokens or not line_tokens:
        return 0.0
    matcher = difflib.SequenceMatcher(None, resolved_tokens, line_tokens, autojunk=False)
    return matcher.ratio()


def _thresholds_payload() -> dict[str, JsonScalar]:
    return {
        "min_auto_accept_confidence": MIN_AUTO_ACCEPT_CONFIDENCE,
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "min_sparse_output_chars": MIN_SPARSE_OUTPUT_CHARS,
        "min_auto_accept_chars": MIN_AUTO_ACCEPT_CHARS,
        "min_auto_accept_words": MIN_AUTO_ACCEPT_WORDS,
        "min_auto_accept_lines": MIN_AUTO_ACCEPT_LINES,
        "min_text_line_correspondence": MIN_TEXT_LINE_CORRESPONDENCE,
        "max_auto_accept_garbled_ratio": MAX_AUTO_ACCEPT_GARBLED_RATIO,
        "orientation_bbox_aspect_threshold": ORIENTATION_BBOX_ASPECT_THRESHOLD,
        "orientation_rotated_line_fraction": ORIENTATION_ROTATED_LINE_FRACTION,
        "min_lines_for_orientation_check": MIN_LINES_FOR_ORIENTATION_CHECK,
    }


def assess_page_quality(
    *,
    mode: str,
    resolved_text: str,
    ocr_success: bool | None,
    ocr_error: str | None,
    mean_confidence: float | None,
    ocr_lines: Sequence[OcrLine] = (),
    engine_name: str | None = None,
    model_version: str | None = None,
) -> PageQualityAssessment:
    """Assess one page. `mode` is the resolved text_source mode
    ('native'/'ocr'/'merged'/'failed'/'skipped' — see
    pipeline/canonicalize.py's mode mapping, which this mirrors).

    Never receives a filename or file path (requirement 4) — callers must
    not pass one, and none of the logic here would know what to do with
    it if they did.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown page mode {mode!r}; expected one of {sorted(_VALID_MODES)}")

    thresholds = _thresholds_payload()
    text = resolved_text or ""
    char_count = len(text.strip())
    word_count = len(text.split())
    garbled_ratio = PdfExtractor._garbled_ratio(text) if text else 0.0
    orientation_verified, orientation_risk, geometry_line_count = (
        _orientation_signals(ocr_lines) if mode in _OCR_MODES else (False, False, 0)
    )
    text_line_correspondence = (
        _text_line_correspondence(text, ocr_lines) if mode in _OCR_MODES else None
    )

    metrics: dict[str, JsonScalar] = {
        "mode": mode,
        "char_count": char_count,
        "word_count": word_count,
        "mean_confidence": mean_confidence,
        "garbled_ratio": round(garbled_ratio, 4),
        "line_count": len(ocr_lines),
        "orientation_verified": orientation_verified,
        "orientation_risk": orientation_risk,
        "orientation_geometry_line_count": geometry_line_count,
        "text_line_correspondence": (
            round(text_line_correspondence, 4) if text_line_correspondence is not None else None
        ),
        "engine_name": engine_name,
        "model_version": model_version,
    }
    frozen_metrics = MappingProxyType(metrics)
    frozen_thresholds = MappingProxyType(thresholds)

    # -- extraction_failed: OCR outright failed, or nothing usable exists. --
    if mode == "failed" or (mode in _OCR_MODES and ocr_success is False):
        reasons = (
            ReasonEntry("OCR_ENGINE_FAILED", f"OCR failed: {ocr_error or 'unknown error'}"),
        )
        return PageQualityAssessment(DECISION_EXTRACTION_FAILED, reasons, frozen_metrics, frozen_thresholds)

    if mode == "skipped" or char_count == 0:
        reasons = (
            ReasonEntry("EMPTY_EXTRACTION", "No text was resolved for this page."),
        )
        return PageQualityAssessment(DECISION_EXTRACTION_FAILED, reasons, frozen_metrics, frozen_thresholds)

    reasons_list: list[ReasonEntry] = []
    sparse_threshold = MIN_AUTO_ACCEPT_CHARS if mode in _OCR_MODES else MIN_SPARSE_OUTPUT_CHARS

    if char_count < sparse_threshold:
        reasons_list.append(
            ReasonEntry(
                "SPARSE_OUTPUT",
                f"Only {char_count} characters were extracted; the minimum for this page type "
                f"is {sparse_threshold}.",
            )
        )

    if mode in _OCR_MODES:
        # Informational composition context, never blocking by itself
        # (requirement 3). mode='ocr' has no native text at all (a genuine
        # full-page scan/standalone image); mode='merged' has native text
        # too, so this may be a region/mixed-page OCR result rather than a
        # whole-page scan — the two are not the same claim and must not
        # share one label (reviewer correction).
        if mode == "ocr":
            reasons_list.append(
                ReasonEntry(
                    "FULL_PAGE_SCAN",
                    "This page has no native text layer at all; its text was derived entirely "
                    "from OCR of a rendered image (a full-page scan or standalone image).",
                )
            )
        else:  # mode == "merged"
            reasons_list.append(
                ReasonEntry(
                    "OCR_DERIVED_MIXED_PAGE",
                    "This page combines native text with additional OCR-derived content "
                    "(region or mixed-page OCR) — not necessarily a full-page scan.",
                )
            )

        if word_count < MIN_AUTO_ACCEPT_WORDS:
            reasons_list.append(
                ReasonEntry(
                    "LOW_WORD_COUNT",
                    f"Only {word_count} word(s) were extracted; the minimum to auto-accept is "
                    f"{MIN_AUTO_ACCEPT_WORDS}.",
                )
            )
        if len(ocr_lines) < MIN_AUTO_ACCEPT_LINES:
            reasons_list.append(
                ReasonEntry(
                    "LOW_LINE_COUNT",
                    f"Only {len(ocr_lines)} OCR line(s) were reported; the minimum to "
                    f"auto-accept is {MIN_AUTO_ACCEPT_LINES}.",
                )
            )

        if mean_confidence is None:
            reasons_list.append(
                ReasonEntry(
                    "NO_CONFIDENCE_SIGNAL",
                    "The OCR engine did not report a confidence score for this page.",
                )
            )
        elif mean_confidence < LOW_CONFIDENCE_THRESHOLD:
            reasons_list.append(
                ReasonEntry(
                    "LOW_CONFIDENCE",
                    f"Mean OCR confidence {mean_confidence:.2f} is below "
                    f"{LOW_CONFIDENCE_THRESHOLD}.",
                )
            )
        elif mean_confidence < MIN_AUTO_ACCEPT_CONFIDENCE:
            reasons_list.append(
                ReasonEntry(
                    "CONFIDENCE_BELOW_AUTO_ACCEPT_BAR",
                    f"Mean OCR confidence {mean_confidence:.2f} does not clear the strict "
                    f"auto-accept bar of {MIN_AUTO_ACCEPT_CONFIDENCE} (a 0.95-confidence scan "
                    "is not auto-acceptable on confidence alone).",
                )
            )

        if garbled_ratio >= MAX_AUTO_ACCEPT_GARBLED_RATIO:
            reasons_list.append(
                ReasonEntry(
                    "GARBLED_OUTPUT",
                    f"{garbled_ratio:.1%} of the resolved text is replacement/control characters.",
                )
            )

        if text_line_correspondence is not None and text_line_correspondence < MIN_TEXT_LINE_CORRESPONDENCE:
            reasons_list.append(
                ReasonEntry(
                    "TEXT_LINE_MISMATCH",
                    f"Resolved page text only {text_line_correspondence:.0%} corresponds to the "
                    "OCR engine's reported per-line text; the two may have diverged during "
                    "merge/post-processing.",
                )
            )

        if not orientation_verified:
            reasons_list.append(
                ReasonEntry(
                    "ORIENTATION_UNVERIFIED",
                    f"Only {geometry_line_count} OCR line(s) had usable bounding-box geometry "
                    f"(minimum to verify orientation is {MIN_LINES_FOR_ORIENTATION_CHECK}) — "
                    "orientation cannot be confirmed, so this is not presumed safe. Note: bbox "
                    "geometry detects text-layout risk in the resolved output, not the original "
                    "source image's orientation (an engine may auto-rotate before reporting it).",
                )
            )
        elif orientation_risk:
            reasons_list.append(
                ReasonEntry(
                    "ORIENTATION_RISK",
                    "A majority of OCR line bounding boxes are taller than wide, suggesting "
                    "rotated or misoriented page content. Note: this reflects the resolved "
                    "output's text layout, not a direct read of the original source image.",
                )
            )

    # Deliberately conservative multi-signal gate (requirement 3): every
    # reason collected above must be clean — confidence alone, however
    # high, is never sufficient. For mode='native' the only signal that
    # can ever fire is SPARSE_OUTPUT (native pages keep their own,
    # simpler logic — no OCR-specific checks apply to them at all).
    has_blocking_reason = any(r.code in _BLOCKING_REASON_CODES for r in reasons_list)
    decision = DECISION_MANUAL_REVIEW if has_blocking_reason else DECISION_AUTO_ACCEPTED

    return PageQualityAssessment(decision, tuple(reasons_list), frozen_metrics, frozen_thresholds)


def assess_document_quality(
    page_assessments: Mapping[int, PageQualityAssessment],
) -> DocumentQualityAssessment:
    """Roll up a document's per-page assessments (keyed by 1-based page
    number) into one document-level decision:

    - auto_accepted: every page auto-accepted.
    - extraction_failed: every page extraction_failed (nothing usable
      anywhere in the document).
    - manual_review_recommended: anything in between.
    """
    if not page_assessments:
        reasons = (ReasonEntry("NO_PAGES", "Document has no pages to assess."),)
        return DocumentQualityAssessment(
            DECISION_EXTRACTION_FAILED, reasons, MappingProxyType({"page_count": 0}), ()
        )

    counts = Counter(a.decision for a in page_assessments.values())
    page_count = len(page_assessments)

    if counts.get(DECISION_AUTO_ACCEPTED, 0) == page_count:
        decision = DECISION_AUTO_ACCEPTED
    elif counts.get(DECISION_EXTRACTION_FAILED, 0) == page_count:
        decision = DECISION_EXTRACTION_FAILED
    else:
        decision = DECISION_MANUAL_REVIEW

    flagged = tuple(
        sorted(
            page_number
            for page_number, assessment in page_assessments.items()
            if assessment.decision != DECISION_AUTO_ACCEPTED
        )
    )

    seen_codes: dict[str, ReasonEntry] = {}
    for assessment in page_assessments.values():
        for reason in assessment.reasons:
            seen_codes.setdefault(reason.code, reason)
    reasons = tuple(seen_codes.values())

    metrics: dict[str, JsonScalar] = {
        "page_count": page_count,
        "auto_accepted_count": counts.get(DECISION_AUTO_ACCEPTED, 0),
        "manual_review_recommended_count": counts.get(DECISION_MANUAL_REVIEW, 0),
        "extraction_failed_count": counts.get(DECISION_EXTRACTION_FAILED, 0),
    }

    return DocumentQualityAssessment(decision, reasons, MappingProxyType(metrics), flagged)
