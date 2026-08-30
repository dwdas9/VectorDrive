"""The three MCP tools' pure logic, independent of the MCP SDK's
decorator/schema machinery (server.py wraps these) — so they're directly
unit-testable without spinning up a protocol session.

Reuses vectordrive.search.hybrid's keyword_search/vector_search/
hybrid_search unmodified (requirement: "reuse the existing search
implementation") — this module adds only MCP-specific concerns: input
validation, read-only-context wiring, structured citation shaping, and
error translation to ToolError.
"""
from __future__ import annotations

from pydantic import BaseModel

from mcp.server.mcpserver.exceptions import ToolError

from vectordrive.mcp.context import McpSearchContext
from vectordrive.mcp.path_safety import resolve_indexed_file
from vectordrive.search.hybrid import hybrid_search, keyword_search, vector_search
from vectordrive.services.processing_run_service import require_completed_index
from vectordrive.services.errors import IndexIncomplete

MAX_TOP_K = 50
_VALID_MODES = {"hybrid", "fts", "vector"}

# Bounded, non-configurable response caps (requirement: "strict
# response-size limit") — chosen to comfortably cover a few paragraphs of
# preview text without ever returning an entire large document in one call.
MAX_PREVIEW_CHARS = 4000
MAX_PAGE_CHARS = 20000


class SearchCitation(BaseModel):
    path: str
    page: int
    chunk_index: int
    source_type: str
    excerpt: str
    score: float
    section: str | None = None


class SearchDriveResult(BaseModel):
    query: str
    mode: str
    result_count: int
    results: list[SearchCitation]


class DocumentPage(BaseModel):
    page_number: int
    source_type: str
    text: str
    truncated: bool


class ReadDocumentResult(BaseModel):
    path: str
    total_pages: int
    truncated: bool
    pages: list[DocumentPage]


class IndexRunSummary(BaseModel):
    started_at: str
    finished_at: str | None
    files_indexed: int
    files_skipped: int
    files_unsupported: int
    files_failed: int


class IndexStatusResult(BaseModel):
    latest_run: IndexRunSummary | None
    file_status_counts: dict[str, int]
    embedding_model: str
    embedding_dimensions: int
    vector_backend: str


def _require_ready(ctx: McpSearchContext) -> None:
    if ctx.conn is None:
        raise ToolError(ctx.startup_error or "The VectorDrive database is not available.")


def _require_searchable(ctx: McpSearchContext) -> None:
    _require_ready(ctx)
    try:
        require_completed_index(ctx.conn)
    except IndexIncomplete as exc:
        raise ToolError(f"{exc} {exc.hint}") from exc


def search_drive(ctx: McpSearchContext, query: str, top_k: int = 10, mode: str = "hybrid") -> SearchDriveResult:
    _require_searchable(ctx)

    if not query or not query.strip():
        raise ToolError("query must not be empty")
    if mode not in _VALID_MODES:
        raise ToolError(f"invalid mode '{mode}' (choose from: {', '.join(sorted(_VALID_MODES))})")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ToolError("top_k must be a positive integer")
    if top_k > MAX_TOP_K:
        raise ToolError(f"top_k must be at most {MAX_TOP_K}")

    if mode == "fts":
        # No embed_provider is constructed and no HTTP request is made on
        # this path at all — FTS mode structurally never contacts Ollama.
        results = keyword_search(ctx.conn, query, top_k)
    else:
        embed_result = ctx.embed_provider.embed_query(query, ctx.embed_config)
        if not embed_result.success:
            raise ToolError(f"could not create a query embedding via Ollama: {embed_result.error_message}")

        try:
            if mode == "vector":
                results = vector_search(ctx.conn, ctx.vector_index, ctx.model_key, embed_result.vector, top_k)
            else:
                results = hybrid_search(
                    ctx.conn, ctx.vector_index, ctx.model_key, query, embed_result.vector, top_k
                )
        except Exception as exc:  # noqa: BLE001 - translate to a clear tool error, never a raw traceback
            raise ToolError(
                f"the vector index is unavailable or corrupt ({exc}); try mode='fts', "
                "or run `vectordrive index --path <folder>` first."
            ) from exc

    citations = [
        SearchCitation(
            path=r.path, page=r.page_number, chunk_index=r.chunk_index,
            source_type=r.source_type, excerpt=r.excerpt, score=r.score,
            section=r.section,
        )
        for r in results
    ]
    return SearchDriveResult(query=query, mode=mode, result_count=len(citations), results=citations)


def read_document(ctx: McpSearchContext, path: str, page: int | None = None) -> ReadDocumentResult:
    _require_ready(ctx)

    file_row = resolve_indexed_file(ctx.conn, path)

    extracted_doc = ctx.conn.execute(
        "SELECT id FROM extracted_docs WHERE file_id = ? AND content_hash = ? ORDER BY id DESC LIMIT 1",
        (file_row["id"], file_row["content_hash"]),
    ).fetchone()
    if extracted_doc is None:
        raise ToolError(
            f"corrupt index state: '{path}' is marked indexed but has no extracted content on record."
        )

    page_rows = ctx.conn.execute(
        "SELECT page_number, text, text_source FROM pages "
        "WHERE extracted_doc_id = ? AND text_source != 'none' ORDER BY page_number",
        (extracted_doc["id"],),
    ).fetchall()
    total_pages = len(page_rows)
    if total_pages == 0:
        raise ToolError(f"corrupt index state: '{path}' has no resolved page text on record.")

    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool):
            raise ToolError("page must be an integer")
        match = next((r for r in page_rows if r["page_number"] == page), None)
        if match is None:
            raise ToolError(f"page {page} not found for '{path}' (document has {total_pages} page(s))")
        text = match["text"] or ""
        truncated = len(text) > MAX_PAGE_CHARS
        return ReadDocumentResult(
            path=path, total_pages=total_pages, truncated=truncated,
            pages=[
                DocumentPage(
                    page_number=match["page_number"], source_type=match["text_source"],
                    text=text[:MAX_PAGE_CHARS], truncated=truncated,
                )
            ],
        )

    # No page requested: a bounded preview, never the complete document
    # (requirement: "return a bounded preview rather than the complete
    # document"). Always include at least the first page, even if it alone
    # exceeds the cap.
    pages: list[DocumentPage] = []
    budget = MAX_PREVIEW_CHARS
    for row in page_rows:
        text = row["text"] or ""
        if budget <= 0 and pages:
            break
        page_truncated = len(text) > budget
        included_text = text[:budget] if pages else text[:MAX_PREVIEW_CHARS]
        pages.append(
            DocumentPage(
                page_number=row["page_number"], source_type=row["text_source"],
                text=included_text, truncated=page_truncated,
            )
        )
        budget -= len(included_text)
        if page_truncated:
            break

    truncated = len(pages) < total_pages or any(p.truncated for p in pages)
    return ReadDocumentResult(path=path, total_pages=total_pages, truncated=truncated, pages=pages)


def index_status(ctx: McpSearchContext) -> IndexStatusResult:
    _require_ready(ctx)

    run_row = ctx.conn.execute("SELECT * FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    latest_run = (
        IndexRunSummary(
            started_at=run_row["started_at"], finished_at=run_row["finished_at"],
            files_indexed=run_row["files_indexed"], files_skipped=run_row["files_skipped"],
            files_unsupported=run_row["files_unsupported"], files_failed=run_row["files_failed"],
        )
        if run_row is not None
        else None
    )

    status_rows = ctx.conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status").fetchall()
    counts = {row["status"]: row["n"] for row in status_rows}

    return IndexStatusResult(
        latest_run=latest_run,
        file_status_counts=counts,
        embedding_model=ctx.embed_config.model,
        embedding_dimensions=ctx.embed_config.dimensions,
        vector_backend=ctx.vector_backend,
    )
