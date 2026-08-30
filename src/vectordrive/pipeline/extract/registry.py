"""Maps a file_type string to the extractor that handles it."""
from __future__ import annotations

from vectordrive.pipeline.extract.base import Extractor
from vectordrive.pipeline.extract.docx import DocxExtractor
from vectordrive.pipeline.extract.email import EmailExtractor
from vectordrive.pipeline.extract.image import ImageExtractor
from vectordrive.pipeline.extract.pdf import PdfExtractor
from vectordrive.pipeline.extract.slides import SlideExtractor
from vectordrive.pipeline.extract.spreadsheet import SpreadsheetExtractor
from vectordrive.pipeline.extract.text import TextExtractor

_REGISTRY: dict[str, type[Extractor]] = {
    "pdf": PdfExtractor,
    "docx": DocxExtractor,
    "eml": EmailExtractor,
    "txt": TextExtractor,
    "md": TextExtractor,
    # "text": a file whose extension isn't otherwise recognized but whose
    # content sniffs as plain text (indexer.py's _looks_like_text) — the
    # generic fallback that covers .json/.yaml/.csv/.xml/.html/... without
    # a dedicated extractor per format.
    "text": TextExtractor,
    "jpg": ImageExtractor,
    "jpeg": ImageExtractor,
    "png": ImageExtractor,
    "xlsx": SpreadsheetExtractor,
    "xls": SpreadsheetExtractor,
    "pptx": SlideExtractor,
}


def get_extractor_for(file_type: str) -> Extractor:
    """Return a fresh extractor instance for `file_type` (e.g. "pdf")."""
    cls = _REGISTRY.get(file_type.lower())
    if cls is None:
        raise ValueError(
            f"No extractor registered for file_type={file_type!r}. "
            f"Supported: {sorted(_REGISTRY)}."
        )
    return cls()
