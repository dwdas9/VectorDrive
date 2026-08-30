"""PP-StructureV3 layout analysis engine (structure stage, not an OCR
tier) — Stage 9.

Ground truth (verified empirically against real paddleocr==3.7.0 output
via throwaway scratch scripts against a synthetic fixture image, per
this stage's mandate — never against the committed suite; the real
model load is slow and downloads weights to ~/.paddlex/official_models,
outside the repo, cached thereafter, same as paddleocr_vl_engine.py):

- `pipeline.predict(array)` returns a list of `LayoutParsingResultV2`,
  dict-like objects. `result["parsing_res_list"]` is a list of
  `LayoutBlock` objects in prediction order, each with `.index`,
  `.label` (a lowercase engine label: 'doc_title', 'paragraph_title',
  'text', 'table', 'figure', ... — passed through as-is, already
  lowercase), `.bbox` ([x0, y0, x1, y1]), and `.content` — plain text
  for prose blocks, or a raw HTML `<table>...</table>` string when
  `.label == "table"`.
- LayoutBlock exposes an `.order_index` attribute, but it was observed
  `None` on some blocks in the same result that had it set on others —
  not reliable. Prediction-list order (enumerate() index) is what the
  plan specifies for `reading_order`, and is what's used here.
- No per-block confidence score is exposed (same honesty as
  PaddleOcrVlEngine: `confidence` stays None rather than faking a
  number).
- The result object's `.markdown` attribute (not a dict key — confirmed
  both exist independently) is a dict with a `"markdown_texts"` key
  holding the pipeline's own whole-page reading-order markdown
  (headings prefixed, tables inlined as raw HTML) — used directly for
  StructureResult.markdown rather than reconstructed from blocks.
"""
from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser

_PIPELINE_VERSION = "3"  # PP-StructureV3


class PPStructureUnavailableError(RuntimeError):
    """PP-StructureV3 could not be used — message carries the exact blocker."""


def _import_ppstructure():
    """Module-level indirection point so tests can monkeypatch import
    failures without needing paddleocr actually uninstalled — mirrors
    _import_paddleocr_vl.
    """
    from paddleocr import PPStructureV3

    return PPStructureV3


class _TableHTMLParser(HTMLParser):
    """Minimal stdlib HTML table parser — no new dependency. Collects
    <tr>/<td>/<th> cell text, collapsing internal whitespace.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None
        elif tag in ("td", "th") and self._current_cell is not None:
            text = " ".join("".join(self._current_cell).split())
            self._current_row.append(text)
            self._current_cell = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def _html_table_to_gfm(html: str) -> str:
    """Convert a PP-StructureV3 table block's HTML into a GFM pipe table.

    Ragged rows (fewer cells than the header) are padded with empty
    cells rather than dropped — a malformed table still produces a
    parseable (if incomplete) markdown table instead of raising.
    """
    parser = _TableHTMLParser()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        return ""

    def esc(cell: str) -> str:
        return cell.replace("|", "\\|")

    header = rows[0]
    lines = ["| " + " | ".join(esc(c) for c in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows[1:]:
        padded = (row + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(esc(c) for c in padded) + " |")
    return "\n".join(lines)


@dataclass
class StructureBlock:
    type: str  # engine label, lowercased
    text: str  # plain text; for tables: GFM markdown table
    bbox: list[float] | None
    reading_order: int
    confidence: float | None
    parent_index: int | None  # index of the governing heading block, or None


@dataclass
class StructureResult:
    engine_name: str
    model_version: str
    success: bool = True
    markdown: str = ""  # whole-page reading-order markdown
    blocks: list[StructureBlock] = field(default_factory=list)
    error_message: str | None = None


_HEADING_LABELS = ("title", "doc_title", "paragraph_title")


class PPStructureEngine:
    NAME = "pp-structurev3"
    MODEL_VERSION = f"PP-StructureV3-{_PIPELINE_VERSION}"

    def __init__(self) -> None:
        self._pipeline = None  # lazy: never load the model at construction time

    def _get_pipeline(self):
        if self._pipeline is None:
            try:
                pp_structure_cls = _import_ppstructure()
            except Exception as exc:
                if getattr(sys, "frozen", False):
                    raise PPStructureUnavailableError(
                        "PP-StructureV3 is not available in the packaged "
                        "VectorDrive.app because its PaddleOCR/PaddleX runtime is "
                        "intentionally excluded. The page will still be indexed "
                        "with flat chunking. To use structure analysis, run "
                        "VectorDrive from a source/pip installation with the "
                        "'paddleocr-vl' extra. "
                        f"Original error: {type(exc).__name__}: {exc}"
                    ) from exc
                raise PPStructureUnavailableError(
                    "PP-StructureV3 is not installed/importable. Install the "
                    "optional extra: pip install -e '.[paddleocr-vl]' "
                    "(paddlepaddle>=3.2.1, paddleocr, paddlex[ocr]). "
                    f"Original error: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                self._pipeline = pp_structure_cls()
            except Exception as exc:
                if getattr(sys, "frozen", False):
                    raise PPStructureUnavailableError(
                        "PP-StructureV3 could not initialize in the packaged "
                        "VectorDrive.app. The page will still be indexed with "
                        "flat chunking; use a source/pip installation with the "
                        "'paddleocr-vl' extra for supported structure analysis. "
                        f"Original error: {type(exc).__name__}: {exc}"
                    ) from exc
                raise PPStructureUnavailableError(
                    f"PP-StructureV3 pipeline failed to initialize. "
                    f"Original error: {type(exc).__name__}: {exc}"
                ) from exc
        return self._pipeline

    def run(self, image_bytes: bytes) -> StructureResult:
        import numpy as np
        from PIL import Image

        pipeline = self._get_pipeline()
        array = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        results = list(pipeline.predict(array))

        if not results:
            return StructureResult(engine_name=self.NAME, model_version=self.MODEL_VERSION)

        result = results[0]
        raw_blocks = result["parsing_res_list"]

        blocks: list[StructureBlock] = []
        current_heading_index: int | None = None
        for index, raw in enumerate(raw_blocks):
            label = (raw.label or "").lower()
            content = raw.content or ""
            if label == "table":
                text = _html_table_to_gfm(content)
            else:
                text = content

            is_heading = label in _HEADING_LABELS
            parent_index = None if is_heading else current_heading_index

            blocks.append(
                StructureBlock(
                    type=label,
                    text=text,
                    bbox=list(raw.bbox) if raw.bbox else None,
                    reading_order=index,
                    confidence=None,  # LayoutBlock exposes no per-block score
                    parent_index=parent_index,
                )
            )
            if is_heading:
                current_heading_index = index

        markdown = ""
        page_markdown = getattr(result, "markdown", None)
        if isinstance(page_markdown, dict):
            markdown = page_markdown.get("markdown_texts") or ""
        if not markdown:
            # Fallback: reconstruct from blocks in reading order.
            parts = []
            for b in blocks:
                if b.type in _HEADING_LABELS:
                    parts.append(f"# {b.text}" if b.type == "doc_title" else f"## {b.text}")
                else:
                    parts.append(b.text)
            markdown = "\n\n".join(p for p in parts if p)

        return StructureResult(
            engine_name=self.NAME,
            model_version=self.MODEL_VERSION,
            success=True,
            markdown=markdown,
            blocks=blocks,
        )
