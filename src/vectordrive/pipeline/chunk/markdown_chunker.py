"""Canonical Markdown-aware chunker (Section 3): turns one page's already-
resolved canonical Markdown text into Chunk objects that respect Markdown
block structure (headings, paragraphs, lists, fenced code, GFM tables)
instead of the legacy flat chunker's plain character-window slicing.

Deterministic and stdlib-only. A chunk never spans two pages -- this
module is always called per page, one PageForChunking at a time, by
chunk/cache.py's canonical route; page boundaries are never crossed here.

Ordinary prose that must be split (too long to fit `chunk_size`) is split
at word boundaries only, via `_split_prose`, never mid-word. A GFM table
that must be split is split only between data rows, repeating the header
+ delimiter row in every piece (`_split_table`); a single indivisible
oversize row still produces an oversize chunk rather than corrupting the
table (same policy as the existing structure-aware chunker's
`_table_pieces`, duplicated in spirit here since this module chunks flat
Markdown text, not PP-StructureV3 blocks).
"""
from __future__ import annotations

import hashlib
import re

from vectordrive.pipeline.chunk.base import Chunk, ChunkerConfig, PageForChunking
from vectordrive.pipeline.embed.context import build_embedding_text, compute_embedding_input_hash

CANONICAL_MARKDOWN_CHUNKER_NAME = "canonical-markdown-aware"
CANONICAL_MARKDOWN_CHUNKER_VERSION = "1"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DELIM_CELL_RE = re.compile(r"^:?-+:?$")


def _is_table_delimiter_row(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if not any(cells):
        return False
    return all(_TABLE_DELIM_CELL_RE.match(c) for c in cells)


def _parse_blocks(text: str) -> list[tuple[str, str]]:
    """Split one page's Markdown text into an ordered list of (kind, raw
    text) blocks: 'heading', 'code', 'table', 'list', or 'para'. A minimal
    but deterministic parser -- enough to keep meaningful blocks intact,
    not a full CommonMark implementation.
    """
    lines = text.split("\n")
    blocks: list[tuple[str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            blocks.append(("heading", line.rstrip()))
            i += 1
            continue

        fence_match = _FENCE_RE.match(line.strip())
        if fence_match:
            fence = line.strip()[:3]
            buf = [line]
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            if i < n:  # closing fence found
                buf.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(buf)))
            continue

        if _TABLE_ROW_RE.match(line) and i + 1 < n and _is_table_delimiter_row(lines[i + 1]):
            buf = [line, lines[i + 1]]
            i += 2
            while i < n and _TABLE_ROW_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(("table", "\n".join(buf)))
            continue

        if _LIST_RE.match(line):
            buf = [line]
            i += 1
            while i < n and lines[i].strip() and not _HEADING_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(("list", "\n".join(buf).rstrip()))
            continue

        # Plain paragraph: consecutive non-blank lines that don't start a
        # different block kind.
        buf = [line]
        i += 1
        while (
            i < n
            and lines[i].strip()
            and not _HEADING_RE.match(lines[i])
            and not _FENCE_RE.match(lines[i].strip())
            and not _LIST_RE.match(lines[i])
            and not (_TABLE_ROW_RE.match(lines[i]) and i + 1 < n and _is_table_delimiter_row(lines[i + 1]))
        ):
            buf.append(lines[i])
            i += 1
        blocks.append(("para", "\n".join(buf).rstrip()))
    return blocks


def derive_document_title(page_texts: list[str]) -> str | None:
    """The first explicit ATX heading found across all pages, in order --
    never invented. Returns None if the document has no heading.
    """
    for text in page_texts:
        for kind, block_text in _parse_blocks(text):
            if kind == "heading":
                match = _HEADING_RE.match(block_text)
                if match:
                    return match.group(2).strip()
    return None


def _split_prose(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split long prose at word boundaries only -- never mid-word.
    Bounded overlap between consecutive pieces, expressed in characters.
    A single token (word) longer than `chunk_size` is indivisible and
    still emitted alone, oversize, rather than corrupted.
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    tokens = re.findall(r"\S+|\s+", text)
    n = len(tokens)
    pieces: list[str] = []
    start = 0
    while start < n:
        length = 0
        i = start
        while i < n and length + len(tokens[i]) <= chunk_size:
            length += len(tokens[i])
            i += 1
        if i == start:  # first token alone already exceeds chunk_size
            i = start + 1
        piece = "".join(tokens[start:i]).strip()
        if piece:
            pieces.append(piece)
        if i >= n:
            break
        # Back up by ~chunk_overlap characters' worth of whole tokens for
        # the next piece's start, guaranteeing forward progress.
        back_len = 0
        j = i
        while j > start and back_len < chunk_overlap:
            j -= 1
            back_len += len(tokens[j])
        start = j if j > start else i
    return pieces


def _split_table(table_text: str, chunk_size: int) -> list[str]:
    """Split a GFM table only between data rows, repeating the header +
    delimiter row (the table's first two lines) in every piece. A single
    indivisible oversize data row still produces an oversize chunk.
    """
    if len(table_text) <= chunk_size:
        return [table_text]

    rows = table_text.splitlines()
    header = rows[0:2]
    pieces: list[str] = []
    buf = list(header)
    for row in rows[2:]:
        if len("\n".join(buf + [row])) > chunk_size and len(buf) > 2:
            pieces.append("\n".join(buf))
            buf = list(header)
        buf.append(row)
    pieces.append("\n".join(buf))
    return pieces


def _split_list(list_text: str, chunk_size: int) -> list[str]:
    """Split an oversize list only between complete list items -- never
    with generic word splitting across item markers. A single indivisible
    oversize item (its marker line plus any continuation lines) still
    produces an oversize chunk rather than being corrupted.
    """
    lines = list_text.split("\n")
    items: list[list[str]] = []
    for line in lines:
        if _LIST_RE.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
        else:  # pragma: no cover - defensive; a list always starts with an item
            items.append([line])

    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for item_lines in items:
        item_text = "\n".join(item_lines)
        added_len = len(item_text) + (1 if buf else 0)
        if buf and buf_len + added_len > chunk_size:
            pieces.append("\n".join(buf))
            buf = []
            added_len = len(item_text)
        buf.append(item_text)
        buf_len += added_len
    if buf:
        pieces.append("\n".join(buf))
    return pieces


def _pack_blocks(
    blocks: list[tuple[str, str]], chunk_size: int, chunk_overlap: int
) -> list[tuple[str | None, str | None, str]]:
    """Turn parsed blocks into (section, block_type, text) pieces ready to
    become Chunks -- packing compatible blocks up to `chunk_size` where
    practical, preferring block boundaries, and never cutting a table row
    or ordinary text mid-word.

    A heading updates `section` for everything that follows it and rides
    along verbatim as a prefix on the very next content block (it is
    never emitted as a standalone chunk of its own) -- so heading text is
    preserved in `Chunk.text`, not dropped. Consecutive headings all ride
    along together (none is overwritten/lost); a trailing heading with no
    following content block still becomes its own searchable unit so a
    heading-only page never drops its heading entirely.
    """
    units: list[tuple[str | None, str, str]] = []
    current_section: str | None = None
    pending_heading_lines: list[str] = []
    for kind, text in blocks:
        if kind == "heading":
            match = _HEADING_RE.match(text)
            current_section = match.group(2).strip() if match else current_section
            pending_heading_lines.append(text)
            continue
        block_text = text
        if pending_heading_lines:
            block_text = "\n\n".join([*pending_heading_lines, text])
            pending_heading_lines = []
        units.append((current_section, kind, block_text))
    if pending_heading_lines:
        units.append((current_section, "heading", "\n\n".join(pending_heading_lines)))

    out: list[tuple[str | None, str | None, str]] = []
    buffer: list[tuple[str | None, str, str]] = []
    buffer_len = 0

    def flush() -> None:
        nonlocal buffer, buffer_len
        if buffer:
            section = buffer[0][0]
            combined = "\n\n".join(u[2] for u in buffer)
            out.append((section, None, combined))
            buffer = []
            buffer_len = 0

    for section, kind, block_text in units:
        # A change of section must never be folded into the still-buffered
        # chunk from the previous section -- flush first, or the combined
        # chunk would inherit `buffer[0]`'s (wrong, earlier) section.
        if buffer and buffer[0][0] != section:
            flush()

        if kind == "table":
            flush()
            for piece in _split_table(block_text, chunk_size):
                out.append((section, "table", piece))
            continue

        if kind == "code":
            # Fenced code must stay atomic -- never run through the
            # word-boundary prose splitter, which would corrupt fence
            # syntax/structure. An oversize code block is emitted whole.
            flush()
            out.append((section, kind, block_text))
            continue

        if kind == "list" and len(block_text) > chunk_size:
            flush()
            for piece in _split_list(block_text, chunk_size):
                out.append((section, kind, piece))
            continue

        if len(block_text) > chunk_size:
            flush()
            for piece in _split_prose(block_text, chunk_size, chunk_overlap):
                out.append((section, kind, piece))
            continue

        added_len = len(block_text) + (2 if buffer else 0)
        if buffer and buffer_len + added_len > chunk_size:
            flush()
            added_len = len(block_text)
        buffer.append((section, kind, block_text))
        buffer_len += added_len

    flush()
    return out


def chunk_canonical_markdown_page(
    page: PageForChunking,
    config: ChunkerConfig,
    start_index: int,
    *,
    filename: str,
    folder: str,
    media_type: str,
    title: str | None,
) -> list[Chunk]:
    """Chunk one page's canonical Markdown text, page-aware (never spans
    another page) and Markdown-block-aware. `filename`/`folder`/
    `media_type`/`title` are trusted canonical provenance (never an
    absolute path) used only to build each chunk's `embedding_text` --
    `Chunk.text` never carries them.
    """
    if not page.text or not page.text.strip():
        return []

    blocks = _parse_blocks(page.text)
    packed = _pack_blocks(blocks, config.chunk_size, config.chunk_overlap)

    chunks: list[Chunk] = []
    index = start_index
    for section, block_type, piece in packed:
        piece_text = piece.strip()
        if not piece_text:
            continue
        embedding_text = build_embedding_text(
            piece_text,
            filename=filename,
            folder=folder,
            media_type=media_type,
            page_number=page.page_number,
            title=title,
            section=section,
        )
        chunks.append(
            Chunk(
                chunk_index=index,
                page_start=page.page_number,
                page_end=page.page_number,
                source_type=page.text_source,
                text=piece_text,
                chunk_hash=hashlib.sha256(piece_text.encode("utf-8")).hexdigest(),
                section=section,
                block_type=block_type,
                embedding_text=embedding_text,
                embedding_input_hash=compute_embedding_input_hash(embedding_text),
            )
        )
        index += 1
    return chunks


def chunk_canonical_markdown_document(
    pages: list[PageForChunking],
    config: ChunkerConfig,
    *,
    filename: str,
    folder: str,
    media_type: str,
    title: str | None,
) -> list[Chunk]:
    """Chunk every page's canonical Markdown text, in page order. Pages
    with no resolved text are skipped, never chunked (matching the flat
    chunker's `chunk_document` behavior).
    """
    chunks: list[Chunk] = []
    index = 0
    for page in pages:
        page_chunks = chunk_canonical_markdown_page(
            page, config, index, filename=filename, folder=folder, media_type=media_type, title=title
        )
        chunks.extend(page_chunks)
        index += len(page_chunks)
    return chunks
