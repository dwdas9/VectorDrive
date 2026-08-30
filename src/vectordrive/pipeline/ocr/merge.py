"""Deterministic merge of a page's native (extracted) text with its OCR
text.

Requirement 1's real-corpus finding: a page whose native text is genuine
body content (not just a watermark) but still gets routed to OCR (raster
coverage, garbled text, ...) previously had its OCR result naively
concatenated onto the native text (``f"{native}\\n\\n{ocr}"``). Because a
full-page OCR pass re-transcribes *everything visible on the page*, that
duplicated most of the native text back into the resolved page text.

This module replaces that with a line-level, containment-based dedup: an
OCR line is dropped only when it is genuinely (near-)covered by the native
text, not merely if it is byte-identical to a native line — OCR noise
(dropped/substituted characters, different line-wrap points) is real and
must not defeat dedup. Genuinely additional OCR content (handwritten
annotations, a stamp, a signature block the native text layer never had)
survives and is appended after the native text.

Deliberately not a naive whole-string equality check (that would only
catch an OCR result byte-identical to the native text, which never
happens in practice) and deliberately not a "drop everything" heuristic
(that would silently discard real handwriting/annotations). Comparison is
word-token level, not character level: character-level sequence matching
on natural-language text produces spurious matches from ordinary shared
letters between two otherwise-unrelated short lines (e.g. "stub ocr text"
and "scanned by camscanner" share several individual letters), which
would under-count genuinely additional content. Matching whole tokens
avoids that while still tolerating OCR line-wrap differences (a native
paragraph re-flowed into several OCR lines still matches token-for-token
against the right slice of the native token stream). Pure stdlib
(``difflib``), fully deterministic — no randomness, no external calls.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# An OCR line counts as "covered" by the native text once this fraction of
# its own (normalized) word tokens are found as matching runs against the
# native text's token stream. Deliberately not 1.0 (would reject a line
# with even one OCR-misread word, e.g. "Ba1ance" for "Balance") and
# deliberately not low enough to suppress genuinely new short lines — see
# module docstring.
DEFAULT_CONTAINMENT_THRESHOLD = 0.72

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace and casefold, for tolerant comparison. Does not
    strip punctuation: exact currency/date punctuation matters for the
    financial/legal documents this pipeline indexes.
    """
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _tokens(normalized_text: str) -> list[str]:
    return normalized_text.split(" ") if normalized_text else []


def _is_punctuation_only(stripped_line: str) -> bool:
    """A line with no alphanumeric content at all -- pure punctuation,
    symbols, or stray marks (a ruled-line artifact, a lone dash, "...").

    This is the *only* thing dropped unconditionally, without ever going
    through containment scoring: unlike a short but genuinely alphanumeric
    line ("A", "1", "OK" — an initial, a page number, a checkbox mark), it
    can never be a piece of additional content worth preserving, and
    scoring it would be meaningless (it has no words to compare). A short
    *alphanumeric* line is never blanket-suppressed by length alone — it
    goes through the same containment scoring as any other line, so a
    genuinely unique short token survives, while one that duplicates
    native content is still suppressed like anything else (reviewer
    correction: the previous `len(line) < 3` blanket filter silently
    erased initials, page numbers, and short words like "OK").
    """
    return not any(ch.isalnum() for ch in stripped_line)


def _containment_ratio(native_tokens: list[str], line_tokens: list[str]) -> float:
    """What fraction of `line_tokens` are covered by matching runs against
    `native_tokens`, per difflib's matching blocks over the token
    sequences (not characters — see module docstring). 1.0 for an empty
    line (nothing to fail to cover).
    """
    if not line_tokens:
        return 1.0
    if not native_tokens:
        return 0.0
    matcher = difflib.SequenceMatcher(None, native_tokens, line_tokens, autojunk=False)
    matched_tokens = sum(block.size for block in matcher.get_matching_blocks())
    return matched_tokens / len(line_tokens)


def merge_native_with_structured_ocr_markdown(
    native_text: str,
    ocr_markdown: str,
    *,
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
) -> str:
    """Merge `native_text` with a validated, *structured* Markdown OCR
    transcription (Phase 1 / Qwen), without ever running it through the
    line-by-line dedup above.

    That dedup is line-oriented and treats blank lines and punctuation-only
    lines as disposable noise -- correct for plain-text OCR, but a real
    corpus finding showed it silently destroys GFM table delimiter rows
    (``|---|---|``) and the blank-line paragraph/table boundaries that make
    Markdown structure meaningful, even though the underlying OCR text was
    already validated Markdown. This helper never touches the OCR block's
    lines: it only decides whether the native text is already represented
    inside it.

    - If `native_text` is blank, or its normalized word tokens are already
      substantially covered by `ocr_markdown` (per the same word-token
      containment ratio used above), `ocr_markdown` is returned byte-for-byte
      unchanged.
    - Otherwise, `native_text` is treated as content the OCR transcription
      missed (e.g. a caption or stamp outside the model's transcribed
      region) and is prepended, followed by a blank-line separator, ahead of
      the untouched `ocr_markdown` block.

    Deterministic, stdlib-only (`difflib`), and never rewrites/repairs the
    Markdown itself.
    """
    native_text = native_text or ""
    ocr_markdown = ocr_markdown or ""

    if not native_text.strip():
        return ocr_markdown

    native_tokens = _tokens(_normalize(native_text))
    ocr_tokens = _tokens(_normalize(ocr_markdown))
    ratio = _containment_ratio(ocr_tokens, native_tokens)
    if ratio >= containment_threshold:
        return ocr_markdown

    return f"{native_text}\n\n{ocr_markdown}"


# Fixed machine-readable markers wrapping each source block for a Phase 1
# `native_plus_qwen_page` routed page. Chosen to not collide with the
# artifact's own `<!-- vectordrive:page-start/-end ... -->` frame markers
# (pipeline/markdown_artifact.py) and to survive that module's verbatim,
# length-delimited page-text round trip unchanged.
NATIVE_BLOCK_OPEN = "<!-- vectordrive:native-text-block -->"
NATIVE_BLOCK_CLOSE = "<!-- vectordrive:native-text-block-end -->"
QWEN_PAGE_BLOCK_OPEN = "<!-- vectordrive:qwen-page-block -->"
QWEN_PAGE_BLOCK_CLOSE = "<!-- vectordrive:qwen-page-block-end -->"


def combine_native_and_qwen_page_blocks(native_text: str, qwen_markdown: str) -> str:
    """Concatenate a page's native text and its full-page Qwen Markdown
    transcription for a `native_plus_qwen_page` routed page.

    Both blocks are inserted **byte-for-byte** — including GFM table
    delimiter rows and blank lines — wrapped only in the fixed HTML-comment
    markers above. No similarity dedup, rewriting, summarization or
    correction is performed: this is a deterministic string join and
    nothing else.
    """
    native_text = native_text or ""
    qwen_markdown = qwen_markdown or ""
    return (
        f"{NATIVE_BLOCK_OPEN}\n"
        f"{native_text}\n"
        f"{NATIVE_BLOCK_CLOSE}\n"
        f"\n"
        f"{QWEN_PAGE_BLOCK_OPEN}\n"
        f"{qwen_markdown}\n"
        f"{QWEN_PAGE_BLOCK_CLOSE}"
    )


@dataclass(frozen=True)
class MergeResult:
    text: str
    ocr_line_count: int
    additional_line_count: int
    suppressed_line_count: int  # duplicate-suppressed via containment scoring
    noise_line_count: int  # punctuation-only lines, dropped without scoring


def merge_native_and_ocr_text(
    native_text: str,
    ocr_text: str,
    *,
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
) -> MergeResult:
    """Merge `native_text` (already-trustworthy extracted text) with
    `ocr_text` (a full-page OCR transcription of the same page),
    suppressing OCR lines that duplicate native content and keeping OCR
    lines that are genuinely additional.

    If `native_text` is blank, `ocr_text` is returned unchanged (there is
    nothing to dedup against) — callers still decide the page's
    text_source ('ocr' vs 'merged') themselves based on whether native
    text existed at all, this function only resolves the text content.
    """
    native_text = native_text or ""
    ocr_text = ocr_text or ""

    if not native_text.strip():
        ocr_line_count = len([ln for ln in ocr_text.split("\n") if ln.strip()])
        return MergeResult(
            text=ocr_text,
            ocr_line_count=ocr_line_count,
            additional_line_count=ocr_line_count,
            suppressed_line_count=0,
            noise_line_count=0,
        )

    native_tokens = _tokens(_normalize(native_text))
    raw_lines = ocr_text.split("\n")

    additional: list[str] = []
    suppressed = 0
    noise = 0
    scored = 0
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue  # blank line: nothing to score, nothing to count
        if _is_punctuation_only(stripped):
            noise += 1
            continue
        scored += 1
        line_tokens = _tokens(_normalize(stripped))
        ratio = _containment_ratio(native_tokens, line_tokens)
        if ratio >= containment_threshold:
            suppressed += 1
        else:
            additional.append(stripped)

    if additional:
        merged_text = f"{native_text}\n\n" + "\n".join(additional)
    else:
        merged_text = native_text

    return MergeResult(
        text=merged_text,
        ocr_line_count=scored,
        additional_line_count=len(additional),
        suppressed_line_count=suppressed,
        noise_line_count=noise,
    )
