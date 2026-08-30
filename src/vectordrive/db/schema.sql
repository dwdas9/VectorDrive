-- VectorDrive core schema.
--
-- sqlite-vec's `vec_chunks` virtual table is NOT created here: its shape
-- depends on the active embedding model's dimensionality and is created by
-- the search/vector-index layer (M8), once it exists.

-- Schema drift detection (Full Coverage Pass, Section 1): the database is
-- a disposable build artifact, fully derived from indexed files — never
-- migrated in place. schema_hash is the sha256 of this file's own text,
-- written once when a brand-new database is created; connect() compares
-- it against a fresh hash of schema.sql on every subsequent open and
-- refuses to run on any mismatch (db/connection.py's
-- compute_schema_hash/check_schema_drift) rather than silently drifting
-- out of sync with it. This exists because the previous approach —
-- additive, backward-compatible migration — let exactly that happen: the
-- pages.text_source CHECK constraint gained 'merged' here without ever
-- being applied to the live database (SQLite can't ALTER a CHECK
-- constraint), silently breaking the OCR-merge feature for a whole
-- release. `version` is gone entirely, not just unused — a plain integer
-- needs the same manual bump discipline that failed last time; a hash of
-- the file's own text can't be forgotten to update.
CREATE TABLE IF NOT EXISTS schema_version (
    schema_hash TEXT
);
INSERT INTO schema_version (schema_hash)
    SELECT NULL WHERE NOT EXISTS (SELECT 1 FROM schema_version);

-- Root identity (Stage 3): a registered indexed folder's persistent id,
-- independent of its current physical path. No `status` column —
-- "missing" is derived live (Path(path).exists()), same philosophy as
-- FolderRow.exists today: the DB never persists a claim the filesystem
-- can contradict. last_relinked_at stays NULL until the first relink.
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_relinked_at TEXT
);

-- One row per indexed file. content_hash is the skip-unchanged key: a
-- re-index that sees the same hash for the same path leaves this row (and
-- everything downstream of it) untouched. path is stored normalized
-- (resolved, absolute) from M10.5 onward so the same file is recognized
-- as the same identity regardless of how it was spelled on the command
-- line (requirement 4). mtime_ns (M10.5) is the fast-path signal: paired
-- with size, it lets a re-index skip a file WITHOUT reading/hashing its
-- content when neither has changed (requirement 5) — mtime (seconds,
-- REAL) is kept only for backward compatibility/display, nanosecond
-- precision is what unchanged-detection actually compares. A pre-M10.5
-- database (created before this column existed) is migrated in place by
-- connection.py's _migrate(), never dropped/recreated (requirement 8).
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    mtime_ns INTEGER,
    content_hash TEXT NOT NULL,
    file_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('indexed', 'skipped', 'unsupported', 'failed', 'not_indexed')),
    error_message TEXT,
    first_indexed_at TEXT,
    last_indexed_at TEXT,
    -- G12 finding: status='indexed' only ever meant "the pipeline ran to
    -- completion without crashing" — it said nothing about whether OCR
    -- actually produced searchable text. A packaged-app image whose OCR
    -- engine failed to import (missing bundled dependency) still got
    -- marked plain "indexed" with zero chunks, indistinguishable from a
    -- genuinely successful file. ocr_warning is additive and nullable
    -- (never changes `status`'s existing meaning or the skip-unchanged
    -- fast path above, which keys off status='indexed') — NULL means no
    -- warning; non-NULL carries a human-readable reason (OCR failure
    -- message, or "no searchable text") for the GUI/CLI to surface
    -- instead of an unqualified "Indexed".
    ocr_warning TEXT,
    -- Stage 3: root identity support. NULL for legacy rows and files
    -- indexed via bare CLI (index_folder called directly, no root
    -- registration) — they keep behaving exactly as before. Backfilled
    -- lazily by roots_service.backfill_files_under_root(), called from
    -- run_index()/retry_failed(), not from migration (migration must not
    -- need config). relative_path is path relative to roots.path, the
    -- key relink's re-index-all-unchanged comparison uses.
    root_id INTEGER REFERENCES roots(id),
    relative_path TEXT,
    -- Phase 1 (Markdown generation) fields: track sidecar-file creation state
    -- markdown_status: one of 'ready', 'final', or 'failed'. 'ready' means
    -- a fresh markdown was generated and awaits potential Phase 2 updates.
    -- 'final' means the markdown is complete and stable. 'failed' means
    -- Markdown generation encountered an error. NULL = never attempted.
    markdown_status TEXT CHECK (markdown_status IN ('ready', 'final', 'failed') OR markdown_status IS NULL),
    -- markdown_path: absolute path to the generated *.vectordrive.md sidecar,
    -- NULL if not yet generated or generation failed.
    markdown_path TEXT,
    -- markdown_body_hash: sha256 of searchable_text(CanonicalDocument),
    -- for detecting whether generated markdown content changed across runs.
    -- NULL if not yet generated.
    markdown_body_hash TEXT,
    -- markdown_updated_at: ISO 8601 UTC timestamp of the last successful
    -- markdown generation, NULL if never generated.
    markdown_updated_at TEXT,
    -- markdown_error: safe (non-extracted-text) error message from
    -- Markdown generation failures, NULL if no error.
    markdown_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash);
-- idx_files_root_rel is NOT created here: on a pre-existing database
-- (created before root_id/relative_path existed) this executescript runs
-- BEFORE _migrate() has a chance to ALTER TABLE those columns in, so an
-- unconditional CREATE INDEX here would fail with "no such column" on
-- every old DB. _migrate() creates the same index, unconditionally and
-- idempotently, after the columns are guaranteed to exist (new DB or
-- old) — see db/connection.py.

-- Extraction-stage cache: one row per (content_hash, extractor, version).
-- Independent of OCR engine choice.
CREATE TABLE IF NOT EXISTS extracted_docs (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (content_hash, extractor_name, extractor_version)
);
CREATE INDEX IF NOT EXISTS idx_extracted_docs_file_id ON extracted_docs(file_id);

-- Per-page extraction output. needs_ocr is decided by page-composition
-- routing (native char count, raster area fraction, garbled-text ratio,
-- biased toward OCR when uncertain -- see pipeline/extract/pdf.py); text
-- holds whatever native text was found even when needs_ocr=1 (kept for
-- merging in, not discarded), and text_source stays 'none' until OCR (M5)
-- resolves it to 'ocr' (no usable native text existed) or 'merged' (native
-- text existed alongside a page still routed to OCR).
-- ocr_error records why OCR is still unresolved for this page (all
-- configured tiers failed) instead of silently leaving needs_ocr=1 with no
-- explanation.
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    extracted_doc_id INTEGER NOT NULL REFERENCES extracted_docs(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT,
    text_source TEXT NOT NULL CHECK (text_source IN ('native', 'ocr', 'merged', 'none')),
    needs_ocr INTEGER NOT NULL DEFAULT 0,
    page_image_hash TEXT,
    ocr_error TEXT,
    -- Stage 3/9: PP-StructureV3 support, inert until Stage 9's pipeline
    -- integration fills it in. structure_result_id IS NULL means no
    -- structure yet (true for all pre-existing data); the chunker falls
    -- back to flat chunking in that case. Old indexed files gain
    -- structure only when their content changes or a Full Rebuild runs
    -- — never via silent mass reprocessing.
    structure_result_id INTEGER REFERENCES structure_results(id),
    structure_error TEXT,
    -- Slice A: durable selected-OCR provenance. Records the exact
    -- ocr_results row whose text was selected for this page's resolved
    -- text_source ('ocr' or 'merged') — the tiered winner, not just "some
    -- engine tried". NULL for text_source in ('native', 'none') and for
    -- all pre-existing rows (this column did not exist before Slice A).
    -- Because this repository detects schema drift by hashing schema.sql
    -- rather than migrating in place (see the schema_hash note above), an
    -- existing derived DB will not gain this column automatically: it
    -- must be reset and re-indexed (delete the DB file, or run a Full
    -- Rebuild) before ocr_result_id/canonicalize.py can be used. No
    -- migration is provided for this column deliberately.
    ocr_result_id INTEGER REFERENCES ocr_results(id),
    -- Phase 1 page-level PDF router provenance (pipeline/extract/pdf_page_router.py).
    -- Populated only for PDF pages processed by Phase 1 "Create Markdown"; NULL
    -- for legacy direct-indexing rows, non-PDF rows, and every pre-existing row.
    -- The derived DB is not migrated in place (schema_hash policy above): an
    -- existing database must be reset and rebuilt to gain these columns.
    -- routing_native_text keeps the original extracted native/hidden text layer
    -- durably separate from the resolved page text (which OCR later overwrites).
    -- All values are JSON-safe scalars.
    routing_fingerprint TEXT,
    routing_action TEXT CHECK (
        routing_action IN ('native', 'native_plus_qwen_page', 'qwen_page')
        OR routing_action IS NULL
    ),
    routing_visible_chars INTEGER,
    routing_invisible_chars INTEGER,
    routing_max_image_coverage REAL,
    -- Summed per-image coverage is DIAGNOSTIC ONLY: repeated / overlapping
    -- image placements inflate it far past the true page footprint, so it
    -- must never independently route a page to OCR. routing_union_image_coverage
    -- is the exact union footprint of the clipped placed-image rectangles
    -- (0..1), or NULL when the rectangle set is pathological (>512 rects).
    routing_summed_image_coverage REAL,
    routing_union_image_coverage REAL,
    routing_image_instance_count INTEGER,
    routing_font_count INTEGER,
    routing_vector_drawing_count INTEGER,
    routing_inline_image_count INTEGER,
    routing_replacement_char_ratio REAL,
    routing_control_char_ratio REAL,
    routing_native_text TEXT,
    UNIQUE (extracted_doc_id, page_number)
);

-- OCR-stage cache (M5): content-addressed and deduped across files by
-- page_image_hash, independent of which file/page references it.
-- success=0 caches a *failure* deliberately (requirement: record OCR
-- failures clearly rather than silently skipping/retrying every run) —
-- error_message carries why. Changing engine/model/lang/config busts the
-- cache and gives a fresh attempt.
CREATE TABLE IF NOT EXISTS ocr_results (
    id INTEGER PRIMARY KEY,
    page_image_hash TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lang TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    confidence REAL,
    bboxes_json TEXT,
    reading_order_json TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (page_image_hash, engine_name, model_version, lang, config_hash)
);

-- Chunk-stage cache (M6): page-aware — page_start always equals page_end,
-- a chunk never spans two pages (per spec). source_type mirrors the
-- originating page's text_source ('native', 'ocr', or 'merged'), never
-- 'none' (pages with no resolved text are skipped, not chunked).
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    extracted_doc_id INTEGER NOT NULL REFERENCES extracted_docs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('native', 'ocr', 'merged')),
    text TEXT NOT NULL,
    chunk_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Stage 3/10: structure-aware chunking metadata, inert until Stage
    -- 10's structure-aware chunker fills it in. The flat chunker inserts
    -- NULLs. The FTS triggers below only reference `text` — untouched.
    section TEXT,
    block_type TEXT,
    bbox_json TEXT,
    reading_order INTEGER,
    -- Section 3: embedding-only contextual input, distinct from `text`/
    -- `chunk_hash` above (which remain the exact user-facing/searchable
    -- identity, never touched by this). embedding_text is `text` with a
    -- deterministic trusted-provenance prefix (filename/folder/title/
    -- type/page/section — see pipeline/embed/context.py) prepended for
    -- embedding provider input only; embedding_input_hash is its hash,
    -- used as the embeddings-cache/vector-index key. NULL for legacy
    -- chunkers (flat/structure-aware); only the canonical Markdown-aware
    -- chunker populates these — get_or_create_embeddings falls back to
    -- text/chunk_hash when NULL. The FTS triggers below reference only
    -- `text` — untouched by this addition.
    embedding_text TEXT,
    embedding_input_hash TEXT,
    UNIQUE (extracted_doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id);

-- Chunk-cache tracking: one row per document, recording which
-- (source_content_hash, chunker, config) key the chunks currently stored in
-- `chunks` for it were computed under. source_content_hash is a hash of all
-- of that document's resolved page text (native or OCR) — it changes if
-- extraction OR OCR output changes, correctly busting this cache, but is
-- independent of chunker/config, so a chunk-settings-only change never
-- touches extracted_docs/pages/ocr_results.
CREATE TABLE IF NOT EXISTS chunk_runs (
    extracted_doc_id INTEGER PRIMARY KEY REFERENCES extracted_docs(id) ON DELETE CASCADE,
    source_content_hash TEXT NOT NULL,
    chunker_name TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- FTS5 exact-search index, external-content over chunks (avoids storing
-- chunk text twice) with triggers to keep it in sync as chunks change.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    text,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_after_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO fts_chunks(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_after_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_after_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO fts_chunks(rowid, text) VALUES (new.id, new.text);
END;

-- FTS5 filename/folder lexical index: a *separate* index from fts_chunks,
-- deliberately not merged into chunk text (a filename search is a
-- structurally different signal — "the file is named this" vs. "the body
-- contains this" — and mixing them would flood results toward whichever
-- arbitrary chunk got the filename prefix).
--
-- v2 design (superseded the original path+relative_path design, which
-- indexed the full absolute machine path as searchable text). That
-- turned out to be a real, measured bug, not a theoretical one: the
-- regression-test target for the "readme status note" query
-- (Name_Correction_README.txt) ranked 56th of 80 filename candidates —
-- past the RRF candidate_pool cutoff — because dozens of unrelated
-- files' absolute paths (e.g. "/Users/<name>/Library/CloudStorage/...")
-- shared tokens with the query, diluting BM25 relevance for the one file
-- whose *name* actually matched. See benchmarks/retrieval/CHECKPOINT_*
-- for the measured before/after.
--
-- The corrected signal split:
--   basename    — strongest signal: what a user actually remembers a
--                 file being *called*.
--   folder_path — weaker, optional signal: the folder relative to a
--                 registered indexed root (Stage 3's `relative_path`
--                 minus the filename itself). Empty for files with no
--                 relative_path (indexed before roots existed, or via
--                 the bare CLI path with no root registration) — they
--                 rely on basename alone, never on absolute-path
--                 fallback text.
--   (absolute path is deliberately never indexed here — it stays a
--   plain, unindexed `files.path` column, metadata only.)
--
-- Not external-content this time: external-content FTS5 requires column
-- names to match real columns on the content table, and `files` has no
-- `basename`/`folder_path` columns (adding them would mean threading two
-- new values through every one of indexer.py's several INSERT/UPDATE
-- sites). Instead this is a small standalone FTS5 table, populated by
-- the triggers below via vd_basename()/vd_folder_path() — two Python
-- functions registered with `conn.create_function()` in
-- db/connection.py — computed from `files.path`/`files.relative_path`
-- at write time. Duplicates a small amount of text (filenames + folder
-- names only, not full paths or chunk-sized content) in exchange for
-- zero changes anywhere in the indexer.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(
    basename,
    folder_path
);

-- Plain DELETE/INSERT, not FTS5's special 'delete' command form: that
-- form is for contentless/external-content tables managing their own
-- sync against a backing table; fts_files is a default (internal-
-- content) FTS5 table, which stores its own copy and accepts ordinary
-- DELETE-by-rowid directly (verified live — the special-command form
-- raises "SQL logic error" against this table shape).
CREATE TRIGGER IF NOT EXISTS files_after_insert AFTER INSERT ON files BEGIN
    INSERT INTO fts_files(rowid, basename, folder_path)
    VALUES (new.id, vd_basename(new.path), vd_folder_path(new.relative_path));
END;

CREATE TRIGGER IF NOT EXISTS files_after_delete AFTER DELETE ON files BEGIN
    DELETE FROM fts_files WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS files_after_update AFTER UPDATE ON files BEGIN
    DELETE FROM fts_files WHERE rowid = old.id;
    INSERT INTO fts_files(rowid, basename, folder_path)
    VALUES (new.id, vd_basename(new.path), vd_folder_path(new.relative_path));
END;

-- Embedding-stage cache (M7; Section 3 renamed the key column): content-
-- addressed by input_hash — the hash of whatever text was actually SENT
-- to the embedding provider. For legacy (flat/structure-aware) chunks
-- that is `chunks.chunk_hash` (their `text`'s own hash, unchanged
-- behavior); for canonical Markdown-aware chunks it is
-- `chunks.embedding_input_hash` (the hash of `chunks.embedding_text`,
-- the trusted-provenance-prefixed contextual input — see
-- pipeline/embed/context.py). Callers pass whichever identity applies
-- (pipeline/embed/cache.py's get_or_create_embeddings resolves this per
-- chunk). Two chunks whose provider input is byte-identical, even across
-- different files, still share one cached vector — the same dedup
-- property as before, now scoped to the actual provider input rather
-- than always the visible chunk text. Independent of provider/model/
-- dimensions/config_hash, so changing any of those is a fresh key, never
-- touching extraction/OCR/chunking. success=0 caches a failure
-- deliberately (requirement 11: clear errors, not silent/retried-every-
-- run). Renamed from `chunk_hash`: this is a disposable, fully-derived
-- database (see the schema_hash note above) — no migration is provided;
-- an existing DB must be reset/rebuilt for this change, same policy as
-- Slice A's ocr_result_id addition.
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    input_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    vector_json TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (input_hash, provider, model, dimensions, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_input_hash ON embeddings(input_hash);

-- One row per `vectordrive index` run, for the `status` command (M10).
-- errors_json: JSON list of {"path": ..., "error": ...} for files that
-- failed during this run — per-file detail also lives in files.error_message,
-- but a run-level summary shouldn't require cross-referencing every row.
CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_indexed INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    files_unsupported INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT
);

-- Durable two-phase work ledger. Unlike index_runs (a historical summary),
-- these rows are live checkpoints: a process may stop after any item and a
-- later process can resume without guessing what was in flight. `item_key`
-- is a stable root-relative identity; fingerprints make stale checkpoints
-- detectable without storing document text. A startup recovery pass changes
-- orphaned `running` rows to `paused` before work is resumed explicitly.
CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY,
    phase TEXT NOT NULL CHECK (phase IN ('markdown', 'index')),
    root_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'completed', 'failed')),
    content_fingerprint TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT,
    paused_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_processing_runs_status ON processing_runs(status);

CREATE TABLE IF NOT EXISTS processing_run_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'completed', 'failed')),
    content_fingerprint TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT,
    paused_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    error_message TEXT,
    UNIQUE (run_id, item_key),
    UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_processing_run_items_next
    ON processing_run_items(run_id, status, ordinal);

-- PP-StructureV3 output cache (Stage 9/10): content-addressed by
-- page_image_hash like ocr_results, so identical page images across
-- files share one structure analysis. success=0 caches a failure
-- deliberately (same "record clearly, don't silently retry every run"
-- philosophy as ocr_results). markdown is reading-order page markdown
-- (tables rendered as GFM); blocks_json is a JSON list of block dicts
-- (see spec §6.7) driving structure-aware chunking in Stage 10.
CREATE TABLE IF NOT EXISTS structure_results (
    id INTEGER PRIMARY KEY,
    page_image_hash TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    markdown TEXT,
    blocks_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (page_image_hash, engine_name, model_version, config_hash)
);

-- OCR quality review (deterministic per-page manual-review routing):
-- one row per (page, file), overwritten (UNIQUE(page_id, file_id)) each
-- time indexing recomputes it, so it always reflects the page's current
-- resolved text. Keyed by the *pair*, not page_id alone, because
-- extracted_docs (and therefore pages) is content-addressed and shared
-- across any files with identical content (M14) — two duplicate files
-- must each get their own review row (their own file_id/path), not
-- silently clobber each other's, since the whole point of this table is
-- showing the user the *exact file path* a flagged page belongs to.
-- decision is one of 'auto_accepted', 'manual_review_recommended',
-- 'extraction_failed' (see pipeline/ocr/quality.py).
-- reason_codes_json/reasons_json/metrics_json/thresholds_json are JSON
-- payloads (stable machine-readable codes plus human-readable messages,
-- measured metrics, and the exact thresholds applied) so a decision is
-- always reproducible from what's stored, never only from reading
-- source. source_relative_path is duplicated from files.relative_path at
-- assessment time (denormalized) purely so the review queue can list
-- flagged pages without an extra join on every read.
CREATE TABLE IF NOT EXISTS page_quality_reviews (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    extracted_doc_id INTEGER NOT NULL REFERENCES extracted_docs(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (
        decision IN ('auto_accepted', 'manual_review_recommended', 'extraction_failed')
    ),
    reason_codes_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    engine_name TEXT,
    model_version TEXT,
    source_relative_path TEXT,
    assessed_at TEXT NOT NULL,
    UNIQUE (page_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_page_quality_reviews_decision ON page_quality_reviews(decision);
CREATE INDEX IF NOT EXISTS idx_page_quality_reviews_file_id ON page_quality_reviews(file_id);

-- Document-level rollup of the above (one row per file's current
-- extracted_doc), same overwrite-on-reassessment semantics.
CREATE TABLE IF NOT EXISTS document_quality_reviews (
    id INTEGER PRIMARY KEY,
    extracted_doc_id INTEGER NOT NULL REFERENCES extracted_docs(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (
        decision IN ('auto_accepted', 'manual_review_recommended', 'extraction_failed')
    ),
    reason_codes_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    flagged_page_numbers_json TEXT NOT NULL,
    source_relative_path TEXT,
    assessed_at TEXT NOT NULL,
    UNIQUE (file_id)
);
CREATE INDEX IF NOT EXISTS idx_document_quality_reviews_decision ON document_quality_reviews(decision);

-- Requirement 5: local handoff-package preparation log. Recorded only
-- when a user clicks one of the review queue's two explicit per-page
-- buttons ("Prepare for ChatGPT" / "Prepare for Claude" —
-- services/review_service.py, gui/web/views/review.js) — never written
-- automatically, and never itself a network call. manifest_path/
-- prompt_path/page_artifacts_json (a JSON array of local rendered/copied
-- page-image paths, one per selected page) all point at
-- VectorDrive-controlled storage under data_home, at the *published*
-- (not staging) directory (never beside the source — requirement 6).
-- provider is no longer free text: it is exactly 'chatgpt' or 'claude'
-- (services/review_service.py's _ALLOWED_PROVIDERS), matching the GUI's
-- two buttons — there is no third option and no user-typed value. This
-- table records only that local package preparation happened; it is
-- never evidence that any upload or provider contact occurred, because
-- none ever does in this slice.
CREATE TABLE IF NOT EXISTS review_handoffs (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    page_numbers_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    prompt_path TEXT NOT NULL,
    page_artifacts_json TEXT NOT NULL,
    source_relative_path TEXT,
    prepared_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_handoffs_file_id ON review_handoffs(file_id);

-- Review panel "Remove from VectorDrive": a projection of config.toml's
-- durable [[excluded_files]] list into the derived database, so an
-- exclusion is queryable inside the same transaction that purges a file's
-- derived rows. config.toml is the source of truth (it survives
-- `vectordrive reset` / a Full Rebuild); this table is rebuilt from it and
-- is safe to lose. Identity is root-relative first (root_path +
-- relative_path), with abs_path a normalized fallback. Unlike every other
-- schema change in this file, this one ships with an additive in-place
-- migration (db/connection.py's _apply_additive_migrations) so an existing
-- database gains the table without a reset — it is purely additive, has no
-- foreign keys, and removing it structurally would still be caught as
-- drift.
CREATE TABLE IF NOT EXISTS file_exclusions (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    reason TEXT,
    excluded_at TEXT NOT NULL,
    UNIQUE (root_path, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_file_exclusions_abs_path ON file_exclusions(abs_path);
