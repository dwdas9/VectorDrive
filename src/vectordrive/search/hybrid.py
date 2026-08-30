"""Deterministic score-magnitude-preserving fusion, and the three search
entry points: keyword_search (FTS5 body text + fts_files filename,
combined via document-level normalized_score_fusion), vector_search
(VectorIndex only, unaffected by filename search — it's a purely lexical
signal, and unaffected by document-level fusion since it's a single
channel), and hybrid_search (body text + vector + filename, all three
combined via document-level normalized_score_fusion).

Fusion is keyed by file_id, not chunk_id. Before this, keyword_search and
hybrid_search fused raw chunk_ids (from body-FTS/vector) alongside
synthetic -file_id sentinels (from filename search) — structurally
unrelated ids for the same physical document, so a file matching via
filename AND via its own body text never accumulated evidence, and a
file with many matching chunks could consume many result slots by
itself. Now: each channel's candidates are collapsed to one entry per
file (its best-scoring chunk decides that file's score within the
channel), those per-channel file scores are fused by file_id via
normalized_score_fusion, and each winning file is hydrated with the most
useful real chunk available across channels, falling back to a direct DB
lookup, and only falling back to a path-only citation when a file
genuinely has no indexed chunks at all.

2026-08-23: fusion switched from rank-only reciprocal_rank_fusion to
normalized_score_fusion (per-channel min-max score normalization) after
diagnosing a real-corpus failure: a file that was the sole, decisive
match in exactly one channel (filename IDF score 35.75, no runner-up)
ranked 8th behind files with only mediocre presence spread across two
channels, because RRF discards each channel's own score magnitude and
keeps only rank position. reciprocal_rank_fusion itself is unchanged and
still tested — it's just no longer what these three functions fuse with.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from vectordrive.search.filename import filename_search
from vectordrive.search.fts import FtsHit, search_fts
from vectordrive.search.vector_index.base import VectorHit, VectorIndex

# Same candidate_pool convention hybrid_search already used for FTS/vector
# before this change — kept as the default for keyword_search's internal
# candidate pool too now that it fuses two lists instead of returning one
# directly, and reused as filename_search's pool size everywhere it's
# fused in (spec: "match the existing candidate_pool convention").
DEFAULT_CANDIDATE_POOL = 50


@dataclass(frozen=True)
class SearchResult:
    # For a document with a real supporting chunk, chunk_id is that
    # chunks.id (positive). For a file with genuinely no indexed chunks
    # at all (extraction/OCR/chunking hasn't run yet), chunk_id is the
    # synthetic value -file_id — files.id is always >= 1, so this can
    # never collide with a real chunk_id, and downstream consumers can
    # tell the two apart via source_type == "filename". This sentinel is
    # purely a citation-identity detail now: document-level fusion itself
    # is keyed by file_id, not by this field.
    chunk_id: int
    path: str
    page_number: int
    chunk_index: int
    source_type: str
    excerpt: str
    score: float
    section: str | None = None
    block_type: str | None = None


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Combine any number of best-first ID rankings into one fused ranking.

    RRF score for an id = sum over rankings it appears in of 1/(k + rank + 1)
    (rank is 0-indexed). Deterministic: identical input always produces
    identical output, including tie-breaking (equal scores sorted by id
    ascending) — requirement 6. Generic over what the ids mean.

    Kept as a tested, generic utility, but no longer what keyword_search/
    hybrid_search fuse with — see normalized_score_fusion below for why:
    rank-only fusion discards each channel's own score magnitude, so a
    channel a candidate wins decisively (e.g. the only filename match,
    IDF score 35.75 vs. no runner-up) is worth exactly the same
    1/(k+rank+1) as a channel it barely won by a hair. That structurally
    lets a candidate with mediocre mid-pool scores in two channels
    outscore a candidate that dominates one channel outright — diagnosed
    live against the corpus (2026-08-23): "Bata Receipt Jurong.pdf", the
    sole and unambiguous filename match for its query, ranked 8th at a
    fused score matching exactly one channel's floor contribution, behind
    several files whose combined two-channel presence had nothing near
    that decisiveness in either channel.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def normalized_score_fusion(
    channel_scores: list[dict[int, float]], weights: list[float] | None = None
) -> list[tuple[int, float]]:
    """Combine any number of id->score channels into one fused ranking,
    preserving each channel's own score magnitude instead of collapsing it
    to rank position (see reciprocal_rank_fusion's docstring for the
    failure mode this replaces it to fix).

    Each channel's scores are min-max normalized to [0, 1] across that
    channel's own candidates before summing (weighted, default equal) —
    this keeps a channel's *relative* spread (how decisively its top
    candidate beat the rest of its own pool) while making differently
    scaled channels (bm25, cosine/L2, IDF-sum) comparable enough to add.
    A channel with one candidate, or every candidate tied, normalizes its
    top score to 1.0 (nothing to divide by — full credit, not zero). A
    candidate absent from a channel contributes 0 from it. Deterministic,
    same tie-break convention as reciprocal_rank_fusion (equal scores
    sorted by id ascending).
    """
    if weights is None:
        weights = [1.0] * len(channel_scores)
    if len(weights) != len(channel_scores):
        raise ValueError(
            f"weights ({len(weights)}) must have the same length as channel_scores ({len(channel_scores)})"
        )

    totals: dict[int, float] = {}
    for scores, weight in zip(channel_scores, weights):
        if not scores:
            continue
        lo = min(scores.values())
        hi = max(scores.values())
        spread = hi - lo
        for item_id, raw in scores.items():
            normalized = 1.0 if spread == 0 else (raw - lo) / spread
            totals[item_id] = totals.get(item_id, 0.0) + weight * normalized
    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))


def _hydrate_chunk(conn: sqlite3.Connection, chunk_id: int, score: float) -> SearchResult | None:
    row = conn.execute(
        "SELECT files.path AS path, chunks.page_start AS page_number, "
        "chunks.chunk_index AS chunk_index, chunks.source_type AS source_type, "
        "chunks.text AS text, chunks.section AS section, chunks.block_type AS block_type "
        "FROM chunks JOIN files ON chunks.file_id = files.id WHERE chunks.id = ?",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None  # stale id (e.g. chunk removed since the ranking was built) — skip, don't crash
    return SearchResult(
        chunk_id=chunk_id,
        path=row["path"],
        page_number=row["page_number"],
        chunk_index=row["chunk_index"],
        source_type=row["source_type"],
        excerpt=row["text"],
        score=score,
        section=row["section"],
        block_type=row["block_type"],
    )


def _hydrate_file_only(conn: sqlite3.Connection, file_id: int, score: float) -> SearchResult | None:
    """The safe fallback (spec: "define a safe fallback for filename-only
    files with no usable chunks") — a citation that points at the whole
    file rather than any chunk of it, because the file genuinely has none
    yet. page_number=0 and chunk_index=-1 are sentinels meaning "no
    specific chunk" (page_number=0 already reads as falsy/absent in the
    GUI, which only shows a "p. N" badge when page_number is truthy).
    """
    row = conn.execute("SELECT path, relative_path FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return None  # stale id (file deleted since the ranking was built) — skip, don't crash
    return SearchResult(
        chunk_id=-file_id,
        path=row["path"],
        page_number=0,
        chunk_index=-1,
        source_type="filename",
        excerpt=row["relative_path"] or row["path"],
        score=score,
    )


def _chunk_to_file_map(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, int]:
    """Batched chunk_id -> file_id lookup (one query, not one per chunk)."""
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(f"SELECT id, file_id FROM chunks WHERE id IN ({placeholders})", chunk_ids).fetchall()
    return {row["id"]: row["file_id"] for row in rows}


def _collapse_to_file_scores(
    chunk_hits: list[tuple[int, float]], chunk_to_file: dict[int, int]
) -> tuple[dict[int, float], dict[int, int]]:
    """Collapse a best-first (chunk_id, score) channel into per-file
    scores: a file's score is its best (first-occurring, since
    `chunk_hits` is already best-first) chunk's score within this channel
    — same "best hit wins" rule as before, now carrying the actual score
    forward instead of discarding it down to rank position, so fusion can
    weigh how decisively a channel was won, not just that it was won.

    Returns (file_id -> score in this channel, file_id -> that file's
    best chunk_id in this channel) — the second value still feeds
    supporting-evidence selection exactly as before.
    """
    file_scores: dict[int, float] = {}
    best_chunk_for_file: dict[int, int] = {}
    for chunk_id, score in chunk_hits:
        file_id = chunk_to_file.get(chunk_id)
        if file_id is None:
            continue  # stale chunk id, no longer in chunks table
        if file_id not in file_scores:
            file_scores[file_id] = score
            best_chunk_for_file[file_id] = chunk_id
    return file_scores, best_chunk_for_file


def _select_supporting_chunk(conn: sqlite3.Connection, file_id: int, candidates: list[int | None]) -> int | None:
    """Pick which chunk to show as a winning document's evidence.
    `candidates` is a priority-ordered list of "this file's best chunk in
    channel X, if it had one" (e.g. [best-in-FTS, best-in-vector]) — the
    first non-None entry wins, so a real body/vector match is always
    preferred over guessing. If no channel supplied one (the file was
    promoted purely by filename evidence, or by a channel outside this
    priority list), fall back to fetching *any* real chunk of this file
    directly — "if a document wins primarily from filename evidence but
    has indexed chunks, select a useful actual text chunk/preview" (spec)
    — its first chunk by chunk_index, a stable and reasonable preview.
    Returns None only if the file has no chunks at all.
    """
    for chunk_id in candidates:
        if chunk_id is not None:
            return chunk_id
    row = conn.execute("SELECT id FROM chunks WHERE file_id = ? ORDER BY chunk_index LIMIT 1", (file_id,)).fetchone()
    return row["id"] if row else None


def _hydrate_documents(
    conn: sqlite3.Connection,
    ranked_file_ids: list[tuple[int, float]],
    chunk_candidates_by_file: dict[int, list[int | None]],
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for file_id, score in ranked_file_ids:
        chunk_id = _select_supporting_chunk(conn, file_id, chunk_candidates_by_file.get(file_id, []))
        result = _hydrate_chunk(conn, chunk_id, score) if chunk_id is not None else None
        if result is None:
            result = _hydrate_file_only(conn, file_id, score)
        if result is not None:
            results.append(result)
    return results


def keyword_search(
    conn: sqlite3.Connection, query: str, top_k: int = 10, candidate_pool: int = DEFAULT_CANDIDATE_POOL
) -> list[SearchResult]:
    """Body-text FTS ranking + filename ranking, collapsed to one entry
    per file within each channel, then fused via document-level
    normalized_score_fusion keyed by file_id (magnitude-preserving, not
    rank-only RRF — see that function's docstring). Each signal is
    queried out to `candidate_pool` candidates first (same convention
    hybrid_search uses) so a strong filename match isn't crowded out by a
    wide body-text candidate set, or vice versa, before fusion narrows
    down to `top_k`.
    """
    fts_hits = search_fts(conn, query, candidate_pool)
    file_hits = filename_search(conn, query, candidate_pool)

    chunk_to_file = _chunk_to_file_map(conn, [h.chunk_id for h in fts_hits])
    fts_file_scores, fts_best_chunk = _collapse_to_file_scores(
        [(h.chunk_id, h.score) for h in fts_hits], chunk_to_file
    )
    filename_file_scores = {h.file_id: h.score for h in file_hits}

    fused = normalized_score_fusion([fts_file_scores, filename_file_scores])
    chunk_candidates_by_file = {file_id: [fts_best_chunk.get(file_id)] for file_id in fts_file_scores}
    return _hydrate_documents(conn, fused[:top_k], chunk_candidates_by_file)


def vector_search(
    conn: sqlite3.Connection,
    index: VectorIndex,
    model_key: str,
    query_vector: list[float],
    top_k: int = 10,
) -> list[SearchResult]:
    """Single-channel embedding search — unchanged by document-level
    fusion (spec: "preserve vector behaviour unless changing it is
    genuinely required"). There's no cross-channel evidence to
    accumulate here, so it still returns one result per chunk hit exactly
    as before.
    """
    hits: list[VectorHit] = index.search(model_key, query_vector, top_k)
    results = [_hydrate_chunk(conn, hit.chunk_id, hit.score) for hit in hits]
    return [r for r in results if r is not None]


def hybrid_search(
    conn: sqlite3.Connection,
    index: VectorIndex,
    model_key: str,
    query: str,
    query_vector: list[float],
    top_k: int = 10,
    candidate_pool: int = DEFAULT_CANDIDATE_POOL,
    channel_weights: list[float] | None = None,
) -> list[SearchResult]:
    """FTS5 body-text ranking + vector ranking + filename ranking, each
    collapsed to one entry per file within its own channel, fused via
    document-level normalized_score_fusion keyed by file_id
    (magnitude-preserving, not rank-only RRF — requirement 7: a duplicate
    document is now structurally impossible, the same way a duplicate id
    always was), and hydrated into full SearchResult citations
    (requirement 8) using the best real chunk available from either
    channel, or a direct DB lookup, or the path-only fallback.

    `channel_weights` (default equal, [fts, vector, filename] order) lets
    a caller bias fusion toward/away from one channel; magnitude
    preservation alone is what fixes the diagnosed bug, so equal weights
    are the right default — this is an escape hatch, not required tuning.
    """
    fts_hits: list[FtsHit] = search_fts(conn, query, candidate_pool)
    vec_hits: list[VectorHit] = index.search(model_key, query_vector, candidate_pool)
    file_hits = filename_search(conn, query, candidate_pool)

    fts_chunk_ids = [h.chunk_id for h in fts_hits]
    vec_chunk_ids = [h.chunk_id for h in vec_hits]
    chunk_to_file = _chunk_to_file_map(conn, fts_chunk_ids + vec_chunk_ids)

    fts_file_scores, fts_best_chunk = _collapse_to_file_scores(
        [(h.chunk_id, h.score) for h in fts_hits], chunk_to_file
    )
    vec_file_scores, vec_best_chunk = _collapse_to_file_scores(
        [(h.chunk_id, h.score) for h in vec_hits], chunk_to_file
    )
    filename_file_scores = {h.file_id: h.score for h in file_hits}

    fused = normalized_score_fusion(
        [fts_file_scores, vec_file_scores, filename_file_scores], weights=channel_weights
    )

    all_files = set(fts_file_scores) | set(vec_file_scores)
    # Priority: a literal keyword match is preferred as the shown excerpt
    # over a semantic-only vector match when a file has both — simple,
    # deterministic, and doesn't require comparing incomparable score
    # scales (BM25-derived vs. vector distance) across channels.
    chunk_candidates_by_file = {
        file_id: [fts_best_chunk.get(file_id), vec_best_chunk.get(file_id)] for file_id in all_files
    }
    return _hydrate_documents(conn, fused[:top_k], chunk_candidates_by_file)
