"""Keyword search over filenames/folder paths — fts_files.

Indexing itself needs no code here: fts_files is kept in sync purely by
the INSERT/UPDATE/DELETE triggers in schema.sql, which derive its
`basename`/`folder_path` columns from `files.path`/`files.relative_path`
at write time — always in sync by construction, independent of
extraction/OCR/chunking having ever run for that file. This module is
purely the query side.

Scoring is deliberately NOT SQLite FTS5's bm25(): experimentally
diagnosed against a real corpus (benchmarks/retrieval/CHECKPOINT_Q32_
2026-08-16.md) that plain BM25 over a mixed query like "readme status
note" — one filename clue plus unrelated semantic words that happen to
be common short filenames elsewhere — fails two ways: (1) the OR-query's
own candidate ordering lets common tokens' many matches crowd out a rare
token's one real match, and (2) BM25's document-length normalization
penalizes a longer, more specific basename ("Name_Correction_README.txt")
even within its *own* single matching token's ranking, favoring shorter
unrelated basenames that happen to share that one token — confirmed
directly: within the "readme" token alone, the real target ranked last
(67th of 67) behind every plain "ReadMe.txt".

The fix: score each file as the sum, over every *distinct* query token it
matches, of that token's IDF (rarer token = more weight) times a
column weight (basename > folder_path) — with no per-file length
normalization at all, so a basename matching one token scores identically
to any other basename matching that same token, however many other words
either name happens to contain. Retrieval is per-token (not one combined
OR MATCH), so a file's score reflects every token it matches — including
tokens where it isn't the top match — union-deduplicated by file_id.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from vectordrive.search.fts import _TOKEN_RE

# basename is the strongest signal (what a user remembers calling a
# file); folder_path is weaker/optional (schema.sql explains why the
# absolute machine path isn't indexed at all).
BASENAME_WEIGHT = 3.0
FOLDER_PATH_WEIGHT = 1.0

# Guards pathological blowup only (e.g. a query token that happens to be
# extremely common across the whole corpus) — not a candidate_pool tuning
# knob, and high enough that it never binds for realistic filename/folder
# vocabulary. Deterministic truncation (ORDER BY rowid) if it ever does.
_PER_TOKEN_CANDIDATE_CAP = 500


@dataclass(frozen=True)
class FilenameHit:
    file_id: int
    score: float


def _idf(total_files: int, doc_freq: int) -> float:
    """Classic, always-positive IDF: rarer tokens contribute more. Not
    BM25's IDF (which can go negative for very common terms) — this is
    the simpler `log(1 + N/df)` form, chosen because negative
    contributions would let a common-token match actively *subtract*
    from an unrelated file's score, which isn't the property wanted here.
    """
    return math.log(1.0 + total_files / doc_freq)


def filename_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[FilenameHit]:
    tokens = sorted(set(t.lower() for t in _TOKEN_RE.findall(query)))
    if not tokens:
        return []

    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if total_files == 0:
        return []

    scores: dict[int, float] = {}
    for token in tokens:
        doc_freq = conn.execute("SELECT COUNT(*) FROM fts_files WHERE fts_files MATCH ?", (token,)).fetchone()[0]
        if doc_freq == 0:
            continue
        weight = _idf(total_files, doc_freq)

        basename_rows = conn.execute(
            "SELECT rowid FROM fts_files WHERE fts_files MATCH 'basename : ' || ? "
            "ORDER BY rowid LIMIT ?",
            (token, _PER_TOKEN_CANDIDATE_CAP),
        ).fetchall()
        for row in basename_rows:
            scores[row["rowid"]] = scores.get(row["rowid"], 0.0) + weight * BASENAME_WEIGHT

        folder_rows = conn.execute(
            "SELECT rowid FROM fts_files WHERE fts_files MATCH 'folder_path : ' || ? "
            "ORDER BY rowid LIMIT ?",
            (token, _PER_TOKEN_CANDIDATE_CAP),
        ).fetchall()
        for row in folder_rows:
            scores[row["rowid"]] = scores.get(row["rowid"], 0.0) + weight * FOLDER_PATH_WEIGHT

    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:top_k]
    return [FilenameHit(file_id=file_id, score=score) for file_id, score in ranked]
