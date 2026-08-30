# VectorDrive Version 1 — Final Release Notes

**Delivery date:** August 30, 2026

**Source delivery:** Version 1

**Packaged application build:** 0.5.2

**Supported production platform:** macOS on Apple Silicon

## Release summary

Version 1 is the finalized production handover of VectorDrive. The repository
has been rebuilt as a clean, single-commit source delivery containing only the
code, runtime assets, build definition, and operational documentation required
to run and maintain the final application.

Development history, experimental scripts, benchmarks, internal checkpoints,
temporary work files, obsolete test cases, local data, and generated build
outputs are intentionally excluded. The receiving developer can begin with
the current production design without reconstructing earlier development
decisions or navigating discarded implementations.

## Product overview

VectorDrive is a local document-ingestion, OCR, vectorization, search, and
document-grounded chat application. It processes folders of business
documents into a private, locally searchable corpus and exposes that corpus
through a macOS desktop application, command-line interface, and read-only MCP
integration for Claude Desktop.

## Version 1 features

### Document ingestion and batch processing

- Recursive folder discovery and processing.
- Dedicated extraction for PDF, DOCX, TXT, Markdown, JPG, PNG, XLSX, XLS, and
  PPTX.
- Generic detection for common UTF-8 text-like formats.
- Incremental re-indexing with metadata and content-hash checks.
- Skip-unchanged caches for extraction, OCR, chunking, and embeddings.
- Full rebuild and verification workflows.
- Removed-file reconciliation so deleted source files do not remain in search.
- Batch progress, pause, resume, cancellation, and durable processing state.

### Qwen OCR and canonical Markdown

- Qwen3-VL Instruct OCR through loopback-only Ollama.
- Native-text-first routing so clean digital PDF pages avoid unnecessary OCR.
- Visual routing for scanned, image-dominant, garbled, and nearly empty pages.
- Preservation and merge of native text on mixed native/visual pages.
- Structured Markdown reconstruction for headings, lists, tables, labels,
  values, and document hierarchy.
- GitHub-Flavored Markdown table validation with one bounded corrective retry.
- Optional local `*.vectordrive.md` sidecars for manual review and audit.
- Explicit page boundaries, source provenance, OCR engine, model, and mode.
- Fail-closed behavior: unavailable models, invalid responses, corrupt PDFs,
  and zero-page extraction never become blank successful artifacts.

### Human review workflow

- Generated Markdown can be reviewed before vectorization.
- Review queue for pages needing manual attention.
- Resolved OCR text, engine/model provenance, decision state, and warnings.
- Copy-path and reveal-in-Finder actions.
- Persistent, non-destructive file exclusion from the VectorDrive corpus.
- Exclusion removes only derived state and the generated sidecar; it never
  deletes the original source file.

### Chunking and embeddings

- Markdown-aware chunking by page, section, block, list item, table row, and
  prose boundary.
- Table headers repeat when a large table must be split.
- Fenced code remains atomic and prose splits only at word boundaries.
- Contextual embedding input using trusted filename, relative folder, title,
  media type, page, and section fields.
- Clean displayed excerpts: embedding context is never injected into visible
  document text or citations.
- Qwen3-Embedding-8B at 1024 dimensions through local Ollama.
- Recipe- and input-hash-aware embedding reuse.

### Search and retrieval

- SQLite FTS5 keyword search.
- Filename and relative-path lexical search.
- sqlite-vec semantic similarity search with a NumPy fallback.
- Hybrid ranking that fuses keyword, filename, and vector channels.
- Page, section, path, source type, excerpt, and score in search results.
- Shared retrieval behavior across desktop search, chat, CLI, and MCP.

### Local document chat

- Streaming chat through an installed local Ollama chat model.
- Optional grounding in bounded VectorDrive search results.
- Source cards derived from actual retrieval results.
- Cancellation and new-chat controls.
- In-memory conversation history that clears when the application exits.
- No model tools, filesystem permissions, or document-modification access.

### Desktop, CLI, and MCP interfaces

- Desktop views for Overview, Folders, Search, Chat, Review, Settings, logs,
  help, and Claude Desktop integration.
- CLI commands for diagnostics, indexing, search, status, reset, GUI launch,
  and MCP launch.
- Read-only MCP tools: `search_drive`, `read_document`, and `index_status`.
- Read-only database access for MCP operations.

### Reliability, security, and operations

- Original source documents are never edited or deleted.
- Loopback-only Ollama endpoint validation, redirects disabled, and proxy
  environment variables ignored for document-bearing requests.
- Sanitized user-visible errors without source paths, page text, or raw parser
  details in protected failure messages.
- Per-file transactions and cleanup of stale derived rows after failure.
- Atomic configuration and artifact writes.
- Exclusive indexing lock to prevent concurrent database writers.
- Visible indexed, failed, unsupported, and skipped counts.
- Operational logs, health checks, resource usage, database-size, and
  reclaimable-space reporting.
- Additive schema migration and schema-drift protection.
- Backup-aware database handling.
- Safe **Reset VectorDrive data** action that removes only allow-listed derived
  state while preserving settings, registered folders, original documents,
  and retained Markdown.

### Performance characteristics

- Native extraction is preferred before OCR.
- OCR is applied only to pages that need it.
- Incremental scans skip unchanged files.
- Content-addressed caches reuse extraction, OCR, and embedding work.
- Embeddings are requested in configurable batches.
- 1024-dimensional production vectors reduce storage and query cost compared
  with unnecessarily wider embeddings.
- Long-running work runs outside the UI thread and reports bounded progress.

## Models and local dependencies

- **Embedding:** `qwen3-embedding:8b`, 1024 dimensions.
- **Visual OCR / Markdown:** `qwen3-vl:8b-instruct` by default, with a
  supported 4B Instruct option for lower-memory systems.
- **Chat:** any compatible chat-capable model already installed in local
  Ollama; `qwen2.5:3b` is an optional starting point.

VectorDrive never downloads, updates, or deletes Ollama models automatically.

## Output artifacts and storage

The application stores configuration and derived state under:

```text
~/Library/Application Support/VectorDrive
```

This includes the SQLite database, logs, caches, processing state, backups,
and review state. When enabled, `*.vectordrive.md` artifacts are written beside
their source documents so a developer or operator can inspect OCR before
indexing. `VECTORDRIVE_HOME` can redirect derived state to another local,
non-synchronized directory.

## Production verification

- 1,715 non-packaging tests passed.
- 11 tests skipped.
- One known macOS Cocoa-thread test was deliberately excluded.
- All 13 packaging checks passed.
- The clean macOS bundle passed strict deep signature verification.
- The packaged application passed a real isolated Cocoa launch.
- The derived-data reset workflow passed disposable-home visual acceptance.
- The accepted build was deployed and run locally without modifying live
  corpus data.

## Known constraints

- Validated on macOS with Apple Silicon only.
- The packaged application is ad-hoc signed and is not Apple-notarized; first
  launch requires macOS's right-click → **Open** override.
- Ollama and the embedding model must be running for indexing and semantic or
  hybrid search. Keyword search works against an existing index without
  Ollama.
- Changing the embedding model or dimensions requires a full index rebuild.
- The packaged app uses flat chunking when optional PP-StructureV3 components
  are unavailable.
- Chat conversations are intentionally not persisted after application exit.

## Delivery contents

```text
assets/                  Application icons
docs/                    User and operational documentation
scripts/build_app.py     macOS application builder
src/vectordrive/         Final production source code
LICENSE                  MIT License
pyproject.toml           Package metadata and dependencies
README.md                Main technical handover and quick start
RELEASE_NOTES.md         Version 1 release notes
```

## Release asset

```text
VectorDrive-v0.5.2-macOS-arm64.zip
```

SHA-256:

```text
75f1c27cacadc4228a9b1e008c9d026155d315d3a497bed8a94f37789112782e
```
