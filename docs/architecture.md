# Architecture

## Pipeline

Every file goes through the same stage-cached pipeline. Each stage's cache
key includes exactly what upstream state would change its output and
nothing else: this is what makes "change embedding model → rebuild only
embeddings" and "change OCR engine → rerun only OCR-dependent stages" true
by construction, not special-case logic. This is enforced by dedicated
tests at every stage boundary.

```mermaid
flowchart LR
    F[File on disk] --> H[hash + normalize identity]
    H --> X[Extract\nPDF/DOCX/TXT/MD]
    X --> O[OCR\nimage-only pages]
    O --> C[Chunk\npage-aware sliding window]
    C --> E[Embed\nOllama qwen3-embedding:8b @ 1024 dimensions]
    E --> I[Search index\nFTS5 + sqlite-vec]
    I --> S[search / MCP tools]
    I --> R[read-only hybrid retrieval]
    R --> L[local Ollama chat]
    L --> G[streamed answer + source cards]
```

## Module boundaries

- **`pipeline/hashing.py`**: file identity (normalized path, content hash).
- **`pipeline/extract/`**: one `Extractor` per file type (`pdf.py`,
  `docx.py`, `text.py`, `image.py`, `spreadsheet.py`, `slides.py`) behind
  a shared interface, plus `cache.py` (content-addressed by
  `(content_hash, extractor_name, extractor_version)`, see below). See
  [Extraction and file coverage](#extraction-and-file-coverage) below for
  which extensions map to which extractor, and what happens to anything
  that doesn't.
- **`pipeline/ocr/`**: `base.py`'s `OcrEngine` interface, one engine per
  tier (`rapidocr_engine.py`, `paddleocr_vl_engine.py`,
  `tesseract_engine.py`), `tiered.py`'s escalation policy, `cache.py`
  keyed by `(page_image_hash, engine, model_version, lang, config)`.
- **`pipeline/chunk/`**: deterministic, page-aware chunking (a chunk
  never spans two pages) plus a cache keyed by a hash of all of a
  document's resolved page text, independent of chunker settings.
  `chunker.py` is the legacy flat character-window chunker (used when no
  canonical Markdown document is supplied); `structure_chunker.py` is the
  PP-StructureV3-block-aware chunker; `markdown_chunker.py` (Section 3)
  is the canonical-Markdown-aware chunker, the only one that ever
  populates a `Chunk`'s `embedding_text`/`embedding_input_hash` — see
  [Visible text vs. embedding input](#visible-text-vs-embedding-input)
  below.
- **`pipeline/embed/`**: `base.py`'s `EmbeddingProvider` interface,
  `ollama_provider.py` (the only implementation), `context.py` (Section
  3: builds the contextual `embedding_text` prefix), `cache.py`
  content-addressed by `input_hash` — the hash of whatever text was
  actually sent to the provider (see below).
- **`pipeline/indexer.py`**: the orchestrator that walks a folder, runs
  every stage in order per file, records `indexed`/`skipped`/`unsupported`/
  `failed`, and isolates one file's failure from the rest of the run.
- **`search/`**: `fts.py` (FTS5), `vector_index/` (`SqliteVecIndex`,
  `NumpyVectorIndex` fallback, both behind a `VectorIndex` protocol),
  `hybrid.py` (Reciprocal Rank Fusion), `index_builder.py` (populates a
  `VectorIndex` from the `chunks`+`embeddings` tables; never re-runs
  extraction/OCR/chunking/embedding).
- **`db/connection.py`**: schema application and additive-only migration.
- **`cli/`**: thin argparse wiring per subcommand; no pipeline/search
  logic of its own.
- **`mcp/`**: the read-only MCP server, with `readonly_db.py` (strictly
  read-only connection), `context.py` (long-lived per-process search
  context, built once), `path_safety.py` (path-identity validation), and
  `tools.py`/`server.py` for the three tools' logic and protocol wiring.
- **`services/search_service.py`**: the shared read-only FTS/vector/hybrid
  orchestration used by Search and Chat.
- **`services/{chat_service,ollama_chat}.py`**: bounded grounded-prompt
  construction and the loopback-only Ollama chat transport. The GUI's
  dedicated `chat_jobs.py` worker streams deltas through the existing event
  bus without sharing the mutating indexing queue.

## Extraction and file coverage

`pipeline/indexer.py`'s `_classify()` decides how a file is handled, in
two steps:

1. **Explicit extension map** (`_SUPPORTED_EXTENSIONS`): `.pdf`, `.docx`,
   `.txt`, `.md`/`.markdown`, `.jpg`/`.jpeg`, `.png`, `.xlsx`/`.xls`,
   `.pptx`, each routed to a dedicated extractor via `extract/registry.py`.
2. **Generic text-sniffing fallback** (`_looks_like_text()`): for any
   extension not in that map, read a small prefix of the file and check
   whether it decodes as UTF-8 with no null bytes and a high ratio of
   printable characters (the same idea the Unix `file` command uses). A
   match is indexed exactly like a `.txt` file (`file_type = "text"`),
   with no per-extension code needed — this is what covers `.json`,
   `.yaml`, `.csv`, `.xml`, `.html`, `.log`, `.ini`, `.toml`, and any
   future text-ish format automatically. A file that fails the sniff
   (a genuine binary: audio, video, an encrypted archive, a compiled
   binary) is recorded `unsupported`, exactly as before this existed.

A spreadsheet's sheets, or a slide deck's slides, become numbered
`ExtractedPage`s the same way a PDF's real pages do (`spreadsheet.py`,
`slides.py`) — this is why chunking, embedding, search, and MCP citations
needed zero changes to support these new types: everything downstream
only ever consumes "an extractor produced page text," never which
extractor produced it.

Every file `_iter_files()`'s walk actually yields ends up counted in
exactly one of `indexed`/`skipped`/`unsupported`/`failed` — `index_folder()`
checks `indexed+skipped+unsupported+failed == total` at the end of every
run and logs an error (loudly, not silently) if that ever fails to hold,
guarding against a future code change reopening a gap. That invariant
only covers files the walk could actually see, though: a directory
`os.walk` can't list at all (permission denied, a transient cloud-sync or
mount error) used to vanish with no exception, no count, and no way for
anything downstream to know it existed. `_iter_files()` now takes an
`on_error(path, exc)` callback for exactly this case — `index_folder()`
logs each one, records it in the same run's `errors_json` (tagged
`"kind": "walk_error"`), and reports the count separately as
`IndexRunSummary.walk_errors`, deliberately kept out of the
indexed/skipped/unsupported/failed arithmetic since an unlistable
directory's file count is unknowable, not zero.

An `unsupported` file is not invisible, though: every file's basename and
folder path are indexed into a separate filename index (`fts_files`,
`db/schema.sql`) via a database trigger that fires on every insert into
`files`, regardless of status. `keyword_search`/`hybrid_search` include a
filename-only match as a path-only citation even when the file has zero
extracted content (`search/hybrid.py`'s `_hydrate_file_only`), so even a
genuinely binary file remains discoverable by name in `search`/MCP
results, just without any content excerpt.

## Cache and content-addressing

Every cache key is content-addressed, never file-path-addressed:

| Stage | Cache key |
|---|---|
| Extraction | `(content_hash, extractor_name, extractor_version)` |
| OCR | `(page_image_hash, engine_name, model_version, lang, config_hash)` |
| Chunking | `(source_content_hash, chunker_name, chunker_version, config_hash)` |
| Embeddings | `(input_hash, provider, model, dimensions, config_hash)` |

This is why two files with identical content correctly share one
extraction/embedding entry. The cache is keyed by content hash, never by
file path or identity. `input_hash` is the hash of whatever text was
actually sent to the embedding provider for that chunk — see below.

## Visible text vs. embedding input

Section 3 introduced a deliberate split, present only on chunks produced
by the canonical Markdown-aware chunker (`pipeline/chunk/markdown_chunker.py`):

- **`Chunk.text`** / **`Chunk.chunk_hash`** stay exactly what they always
  were: the chunk's user-facing, searchable content and a hash of it.
  Nothing is ever prepended here. FTS (`fts_chunks`) indexes only this
  field — a filename, folder, or title never leaks into an excerpt,
  citation, Chat context, or keyword search result.
- **`Chunk.embedding_text`** / **`Chunk.embedding_input_hash`** are a
  *separate* string built only for embedding-provider input
  (`pipeline/embed/context.py`'s `build_embedding_text`): `Chunk.text`
  with a small deterministic prefix of trusted canonical provenance —
  exact source filename, root-relative folder (the parent of
  `source.relative_path`, never an absolute machine path), an explicit
  document title *only* when derived from a real ATX heading (never
  invented), media/file type, page number, and section heading when
  present. No document date is included: the canonical schema has no
  reliable explicit *document* date (`generation.generated_at` is
  pipeline processing time, not the source's own date), and inventing
  one from body text would be a hallucination risk this deliberately
  avoids.
- The embedding cache (`embeddings.input_hash`) and the vector-index
  rebuild (`index_builder.py`, joining on
  `COALESCE(chunks.embedding_input_hash, chunks.chunk_hash)`) both key on
  this contextual identity when present, falling back to the chunk's
  plain `chunk_hash` for legacy (flat/structure-aware) chunks. Two chunks
  with identical *visible* text under different filenames/folders
  therefore share the same `chunk_hash` (same visible content) but get
  different `embedding_input_hash` values and separate cached vectors —
  the embedding, not just the display text, is context-aware.
- `EmbeddingConfig.context_recipe_version`
  (`pipeline/embed/context.py`'s `CONTEXT_RECIPE_VERSION`) is folded into
  `EmbeddingConfig.config_hash()`, so the contextual-recipe and
  plain/legacy embedding spaces are structurally partitioned in both the
  embeddings cache and the vector index's `model_key`. This field is
  never sent to Ollama — `ollama_provider.py`'s request payload is built
  only from `model`/`dimensions`/`extra`.
- Search never adds document context to the query side: `embed_query`
  still applies only Qwen3-Embedding's documented retrieval instruction
  (`format_query_with_instruction`), unchanged.
- The canonical chunk cache identity (`chunk_runs.source_content_hash`
  for the canonical route) folds in exactly the trusted metadata that
  affects embedding input — filename, root-relative `relative_path`,
  media type, derived title, and the context recipe version — so a
  relink/rename/path change correctly busts the cache instead of
  silently serving stale contextual chunks. Absolute paths are never
  part of this identity.

**This is early/eager chunking with contextual embedding input, not late
chunking.** True late chunking (per-token contextual embeddings computed
from a single forward pass over the whole document, then pooled per
chunk) is explicitly deferred: Ollama's `/api/embed` returns one pooled
vector per input and exposes no token-level hidden states, so there is no
way to implement genuine late chunking against it today. See
`docs/adr/0002-embedding-context-vs-late-chunking.md`.

Because this changes the `chunks`/`embeddings` schema (new nullable
`chunks.embedding_text`/`embedding_input_hash` columns; `embeddings`'
`chunk_hash` column renamed to `input_hash`), and this database is a
disposable, fully-derived build artifact (see `db/schema.sql`'s
schema-drift note), any existing derived database must be rebuilt (delete
`vectordrive.db` or run a Full Rebuild) to pick this up — no in-place
migration is provided. Source documents and their `*.vectordrive.md`
sidecars are never touched by this.

## Data lifecycle

1. Source documents are read, never written.
2. Extracted/OCR'd text, chunks, and embeddings are stored in
   `VECTORDRIVE_HOME/vectordrive.db`: a local catalogue derived *from*
   your documents, not a copy *of* them (it doesn't store original file
   bytes, only extracted text and vectors).
3. `search`/MCP tool calls read from this catalogue. Grounded Chat selects at
   most four chunks and sends their bounded excerpts to the local Ollama
   instance alongside the question. Nothing is sent to a remote API.
4. `read_document`/`search_drive` (MCP) return only bounded excerpts of
   what's stored, never the complete document.
5. Chat transcripts and selected source text stay in process memory and are
   discarded when VectorDrive exits; only the chosen model name is persisted.

## Privacy boundaries

- Indexing is opt-in per folder (`index --path`). Nothing outside an
  explicitly indexed folder is ever touched.
- The MCP server is read-only at the connection level (`mode=ro` +
  `PRAGMA query_only=ON`), not just by application-level convention.
- `read_document` never opens an arbitrary file from disk: only
  previously-extracted text already in the database, for paths already
  recorded as successfully indexed.
- Chat accepts only HTTP(S) loopback Ollama endpoints, disables redirects and
  environment proxies, treats retrieved text as untrusted data, and exposes no
  model tools or filesystem actions.
