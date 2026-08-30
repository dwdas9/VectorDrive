"""Plain-text table and excerpt formatting shared by the CLI's
index/search/status commands. Deliberately dependency-free (requirement
10: readable plain-text output without adding a large UI dependency, e.g.
rich or tabulate) — just str.ljust() column alignment.
"""
from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render `headers` + `rows` as a simple space-aligned plain-text
    table with a '----' separator line under the header. Column widths are
    the max of the header and every cell in that column, so everything
    (including an empty table, which still shows the header) lines up.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def _format_row(cells: list[str]) -> str:
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [_format_row(headers), "  ".join("-" * w for w in widths)]
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def truncate_excerpt(text: str, width: int = 80) -> str:
    """Collapse all whitespace (newlines, tabs, repeated spaces) to single
    spaces — chunk text can span a whole page, and a table row must stay on
    one line — then truncate to exactly `width` chars, ending in '...' if
    anything was cut.
    """
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: width - 3] + "..."
