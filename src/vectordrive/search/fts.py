"""FTS5 keyword search over chunk text.

Indexing itself needs no code here: fts_chunks is external-content over
`chunks` with INSERT/UPDATE/DELETE triggers already defined in schema.sql
(M2) — it's always in sync with the chunks table by construction. This
module is purely the query side.

bm25() sign convention verified live before writing this: SQLite FTS5's
bm25() returns *negative* values, more negative = better match (ORDER BY
bm25(...) ASC gives best-first). We negate it so search_fts()'s score
follows the same higher-is-better convention as VectorHit.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class FtsHit:
    chunk_id: int
    score: float


def _build_match_query(text: str) -> str | None:
    """Turn free-form user text into a safe FTS5 MATCH query: each
    alphanumeric token is double-quoted (so hyphens, colons, asterisks,
    unbalanced quotes etc. in the *input* can never be interpreted as FTS5
    operators/syntax) and joined with OR, so a chunk matching *any* query
    term is a candidate — ranked by bm25 relevance, not filtered out.
    Returns None for a query with no usable tokens.
    """
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def search_fts(conn: sqlite3.Connection, query: str, top_k: int) -> list[FtsHit]:
    match_query = _build_match_query(query)
    if match_query is None:
        return []
    rows = conn.execute(
        "SELECT rowid, bm25(fts_chunks) AS raw_score FROM fts_chunks "
        "WHERE fts_chunks MATCH ? ORDER BY raw_score LIMIT ?",
        (match_query, top_k),
    ).fetchall()
    return [FtsHit(chunk_id=row["rowid"], score=-row["raw_score"]) for row in rows]
