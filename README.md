# VectorDrive

> **Version 1 final delivery.** See [Version 1 Release Notes](RELEASE_NOTES.md).

VectorDrive is a production-ready, local-first document processing and search
application. It extracts text from ordinary and scanned documents, creates
reviewable Markdown, divides the content into retrieval-friendly chunks,
generates local embeddings, and exposes the result through desktop search,
document-grounded chat, a command-line interface, and a read-only MCP server.

The repository is intentionally a clean handover. It contains the final
application source, runtime assets, build script, and operational
documentation—without development history, experiments, benchmarks, internal
work notes, or obsolete test fixtures.

## Core capabilities

- Local processing: documents, OCR text, embeddings, queries, and chat context
  stay on the user's Mac.
- Native text extraction and visual OCR for PDFs and images.
- Qwen3-VL OCR that preserves visual context, headings, tables, and document
  hierarchy in Markdown.
- Optional local retention of generated `*.vectordrive.md` artifacts for
  inspection, audit, or reuse.
- Keyword, filename, semantic, and hybrid search.
- Qwen3-Embedding-8B vectors at 1024 dimensions through local Ollama.
- Document-grounded local chat with source cards and page-level citations.
- Read-only Claude Desktop integration through MCP.
- Batch folder processing with progress, pause, resume, cancellation, and
  skip-unchanged caching.
- Visible per-file failure states; one unreadable file does not stop a batch.
- Review and exclusion workflows that never delete original source documents.
- Health checks, operational logs, storage statistics, backup-aware database
  handling, and a safe derived-data reset.

## Supported inputs

VectorDrive has dedicated extraction support for PDF, DOCX, TXT, Markdown,
JPG, JPEG, PNG, XLSX, XLS, and PPTX. It also detects and indexes common UTF-8
text-like files such as CSV, JSON, YAML, XML, HTML, logs, INI, and TOML.
Unsupported binary files remain discoverable by filename. Password-protected,
corrupted, or unreadable files are reported clearly and do not interrupt the
rest of a folder.

## Processing architecture

```text
Folder discovery
    ↓
File classification and change detection
    ↓
Native extraction ── or ── Qwen3-VL visual OCR
    ↓
Canonical Markdown artifact
    ↓
Optional human review / local Markdown retention
    ↓
Markdown-aware chunking and contextual enrichment
    ↓
Qwen3-Embedding-8B (1024-dimensional vectors)
    ↓
SQLite FTS5 + sqlite-vec indexes
    ↓
Desktop Search / Chat / CLI / read-only MCP
```

The pipeline is incremental. Unchanged files are skipped, reusable extraction
and embedding results are cached, and removed files are reconciled out of the
derived index on the next run.

## OCR engine: Qwen

VectorDrive uses **Qwen3-VL Instruct** through Ollama as the visual OCR engine
for its canonical Markdown workflow. The default model is:

```text
qwen3-vl:8b-instruct
```

The application also accepts the supported local 4B variant when lower memory
usage is more important than maximum document understanding.

Qwen was selected because document OCR is not only character recognition. A
useful result must understand visual relationships: a value beside its label,
a table's rows and columns, a heading above the section it controls, and text
embedded in mixed image-and-native pages. Qwen's vision-language approach is
well suited to these cases and produces structured Markdown rather than a flat
stream of words.

In VectorDrive specifically, Qwen provides:

- strong preservation of complex layouts and multi-column context;
- useful reconstruction of tables as GitHub-Flavored Markdown;
- retention of heading and section hierarchy;
- context-aware reading of labels, values, captions, and nearby visual cues;
- one consistent output format for review and downstream chunking.

The OCR response is validated before it is accepted. Table syntax receives a
bounded corrective retry, failed OCR is never recorded as a blank success, and
native text from mixed pages is preserved and merged with visual OCR output.
For the direct-indexing workflow, the packaged app can use its bundled
RapidOCR path as a fast fallback; Qwen remains the primary engine for the
reviewable Markdown pipeline.

## Embedding model

VectorDrive uses **Qwen3-Embedding-8B** through local Ollama:

```text
qwen3-embedding:8b
```

Embeddings are generated at **1024 dimensions**. They represent the semantic
meaning of each document chunk so searches can find relevant content even when
the query and source use different words. VectorDrive combines this semantic
channel with SQLite FTS5 keyword search and filename/path matching in hybrid
mode.

Embedding input includes bounded document context such as filename, relative
folder, page, explicit title, media type, and section. This improves retrieval
without appearing in displayed excerpts or citations.

## Markdown output artifacts

When **Output Markdown files** is enabled for a folder, VectorDrive writes a
`*.vectordrive.md` file beside each processed source document. These files are
canonical intermediate artifacts: readable by a person, validated by the
application, and suitable for deterministic chunking.

- **Enabled:** create Markdown first, then index the validated Markdown.
- **Disabled:** extract and index the source directly without creating new
  Markdown sidecars.

VectorDrive never overwrites the original document. Resetting VectorDrive's
derived data preserves source documents and retained Markdown sidecars.

### Example Markdown structure

The following is a valid representative artifact produced by the shipped
renderer. Frontmatter is canonical JSON between Markdown delimiters; each page
is framed by matching machine-readable HTML comments.

```markdown
---
{"artifact_type":"vectordrive-derived-document","body_sha256":"8be5a237c2b4bd2cf6bbbf0fb77599dacd038dccb4cb5cd985044db84d33d4a9","generation":{"extractor_name":"phase1-pdf","extractor_version":"1","generated_at":"2026-08-30T00:00:00+00:00","pipeline_version":"1"},"lifecycle":{"content_status":"generated","extraction_policy":"if_source_changed","index_policy":"on_content_change"},"page_count":1,"schema_version":1,"source":{"byte_size":123456,"filename":"invoice-1042.pdf","media_type":"application/pdf","page_count":1,"relative_path":"Invoices/invoice-1042.pdf","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
---

<!-- vectordrive:page-start {"mean_confidence":null,"metadata":{},"mode":"ocr","ocr_engine":"qwen3-vl","ocr_language":"und","ocr_model":"qwen3-vl:8b-instruct","page_number":1,"structural_tags":["heading","table"],"text_length":110} -->

# Invoice 1042

## Line items

| Item | Quantity | Total |
|---|---:|---:|
| Replacement filter | 2 | $50.00 |
<!-- vectordrive:page-end {"mean_confidence":null,"metadata":{},"mode":"ocr","ocr_engine":"qwen3-vl","ocr_language":"und","ocr_model":"qwen3-vl:8b-instruct","page_number":1,"structural_tags":["heading","table"],"text_length":110} -->
```

Page boundaries and provenance are retained. ATX headings (`#`, `##`, and so
on) represent document hierarchy, tables use valid GitHub-Flavored Markdown,
lists remain lists, and native/OCR provenance is available to the index and
review workflow.

## Manual OCR review

The standard desktop workflow creates and indexes the Markdown in one job. A
developer can still inspect the retained artifacts after a run before relying
on the corpus. For a strict human approval gate before vectorization, use the
two production service phases separately: run
`markdown_generation_service.generate_markdown`, review or finalize the
generated sidecars, and only then run
`index_service.run_sidecar_index_for_root`.

The review procedure is:

1. Configure a folder and an installed Qwen3-VL model.
2. Run Phase 1 Markdown generation without Phase 2 indexing.
3. Open the generated `*.vectordrive.md` files in a Markdown editor.
4. Compare headings, tables, important values, page boundaries, and OCR
   provenance against the source documents.
5. Correct and mark an artifact final when the workflow requires a frozen,
   manually approved transcription.
6. Run Phase 2 sidecar indexing to chunk, embed, and publish the approved text
   into VectorDrive search.

The application's **Review** screen also surfaces pages needing attention,
shows the resolved OCR text and warnings, and can reveal the corresponding
source in Finder. A file can be excluded from VectorDrive without deleting the
source document.

## System requirements

### Supported production platform

- Apple Silicon Mac
- macOS 11 or later
- Python 3.12 from Homebrew for source-based operation
- Ollama running locally on its default loopback endpoint
- SSD storage for local models, the application, indexes, caches, and optional
  Markdown artifacts

Windows, Linux, and Intel Macs have not been validated for this delivery.

### Memory guidance

- **16 GB RAM:** suitable for the application and lighter local-model use;
  prefer the Qwen3-VL 4B OCR option and avoid loading several models together.
- **32 GB RAM or more:** recommended for the default 8B embedding and 8B OCR
  workflow, especially for larger batches.

Actual model memory depends on the Ollama model build and quantization. Large
document batches also require sufficient free disk space for Ollama models,
the VectorDrive database, OCR cache, embedding cache, and retained Markdown.

## Quick start: packaged Mac app

1. Open the repository's **Releases** page.
2. Download `VectorDrive-v0.5.2-macOS-arm64.zip` from the Version 1 release.
3. Extract it and move `VectorDrive.app` to **Applications**.
4. On first launch, Control-click or right-click the app, select **Open**, and
   confirm **Open** again. This delivery is ad-hoc signed but not yet
   Apple-notarized.
5. Install and open [Ollama](https://ollama.com/).
6. Install the embedding model:

   ```bash
   ollama pull qwen3-embedding:8b
   ```

7. For reviewable Qwen OCR, also install:

   ```bash
   ollama pull qwen3-vl:8b-instruct
   ```

8. Open VectorDrive, verify **Overview**, add a folder under **Folders**, and
   start indexing.

An optional small chat model can be installed separately:

```bash
ollama pull qwen2.5:3b
```

VectorDrive lists compatible models already installed in Ollama. It never
downloads or deletes a model automatically.

## Run from source

```bash
brew install python@3.12 ollama
git clone https://github.com/dwdas9/vectordrive_source.git
cd vectordrive_source

/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e ".[ocr,gui]"

ollama pull qwen3-embedding:8b
.venv/bin/vectordrive doctor
.venv/bin/vectordrive gui
```

Build the packaged application with:

```bash
.venv/bin/pip install pyinstaller typer
.venv/bin/python scripts/build_app.py
```

The build is written to `dist/VectorDrive.app`.

## Configuration

Configuration is stored at:

```text
~/Library/Application Support/VectorDrive/config.toml
```

The most useful settings are available from the desktop application.

| Setting | Default | Purpose |
|---|---|---|
| `ollama_endpoint` | `http://localhost:11434` | Local Ollama service. Non-loopback endpoints are rejected. |
| `embedding_model` | `qwen3-embedding:8b` | Model used for chunk and query embeddings. |
| `embedding_dims` | `1024` | Stored vector width. Changing it requires a full rebuild. |
| `ocr.phase1_markdown_vision_model` | `qwen3-vl:8b-instruct` | Qwen model used for visual Markdown OCR. |
| `indexed_folders` | `[]` | Source folders managed from the Folders screen. |
| `excluded_files` | `[]` | Persistent, non-destructive exclusions from the derived corpus. |

Set `VECTORDRIVE_HOME` before launch to use a different local data directory:

```bash
VECTORDRIVE_HOME=/path/to/local/vectordrive-data open /Applications/VectorDrive.app
```

Do not place `VECTORDRIVE_HOME` inside the source repository or in a
cloud-synchronized folder. Source documents may live in a cloud-backed folder
when their bytes are available offline.

## Search and integration

- **Keyword:** exact and lexical matching through SQLite FTS5.
- **Vector:** semantic matching through Qwen embeddings.
- **Hybrid:** keyword, filename/path, and vector results fused into one ranked
  result set.

The **Chat** screen uses an installed local Ollama chat model and supplies only
bounded retrieved excerpts. Source cards come from actual retrieval results.
Document content is treated as untrusted context and the chat model receives
no filesystem or modification tools.

For Claude Desktop, VectorDrive exposes three read-only MCP tools:
`search_drive`, `read_document`, and `index_status`. See
[Claude Desktop and MCP](docs/mcp-claude-desktop.md) for configuration.

## Reliability and data safety

- Original documents are never modified or deleted.
- Atomic writes protect configuration and derived artifacts.
- File-level transactions prevent partially indexed successes.
- Corrupt, encrypted, inaccessible, or OCR-failed files receive visible,
  sanitized failure states.
- Loopback-only Ollama validation prevents accidental remote document upload.
- Indexing uses an exclusive process lock to prevent concurrent writers.
- Pause and resume state is durable across application restarts.
- Database backups and schema checks protect local upgrades.
- **Reset VectorDrive data** removes only allow-listed derived state and is
  blocked while a job is running.

## Repository contents

```text
assets/                  Application icons
docs/                    User and operational documentation
scripts/build_app.py     macOS application builder
src/vectordrive/         Final application source
LICENSE                  MIT License
pyproject.toml           Package metadata and dependencies
README.md                Main handover and operating guide
RELEASE_NOTES.md         Version 1 delivery notes
```

## License

VectorDrive is provided under the [MIT License](LICENSE).
