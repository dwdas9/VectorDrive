# User Guide

All commands below are copied from the actual CLI and verified against
`--help`. Paths in examples are placeholders (`/path/to/folder`); replace
with your own.

## `doctor`

```bash
vectordrive doctor
```

Run this after install and whenever something seems wrong. See
[Installation](installation.md#4-verify-the-install) for what it checks.

## Indexing a folder

```bash
vectordrive index --path /path/to/folder
```

```
usage: vectordrive index [-h] --path PATH [--rebuild | --verify]

  --path PATH  Folder to index.
  --rebuild    Force reprocessing of every file, ignoring the skip-unchanged cache.
  --verify     Recompute content hashes even when size/mtime look unchanged,
               reprocessing only files whose content actually changed.
```

Supported file types: `.pdf`, `.docx`, `.txt`, `.md`/`.markdown`, `.jpg`/
`.jpeg`, `.png`, `.xlsx`/`.xls`, `.pptx`. Any other file whose content
looks like text (`.json`, `.yaml`, `.csv`, `.xml`, `.html`, `.log`,
`.ini`, `.toml`, and more) is indexed too, no per-format code needed; see
[Architecture: extraction and file coverage](architecture.md#extraction-and-file-coverage)
for exactly how that's decided. Only a genuinely binary file (audio,
video, an encrypted archive, a compiled binary) is recorded as
`unsupported` and skipped, not treated as an error, though it still
stays discoverable by filename. A file VectorDrive can't process (corrupt,
password-encrypted) is recorded as `failed` with a clear error message,
without stopping the rest of the run.

### Multiple source folders, one index

Run `index --path` once per folder you want included; every folder
shares the same database (`VECTORDRIVE_HOME/vectordrive.db`), so search
and `status` cover everything indexed so far, regardless of which folder
each file came from:

```bash
vectordrive index --path /path/to/folder-one
vectordrive index --path /path/to/folder-two
vectordrive status   # now reports files from both folders
```

A file removed from disk between runs is detected and cleaned up
automatically on the next `index --path` covering that folder: its rows
are removed from every table (files, chunks, embeddings, FTS, vector
index) via deletion reconciliation. A file also covered by another
still-registered folder (overlapping/nested roots) is never deleted while
that other folder remains registered.

### Skip-unchanged behaviour

Re-running `index` on an already-indexed folder is fast: a file whose size
and modification time haven't changed since last time is skipped without
even being re-hashed, so a large already-indexed folder with nothing
changed re-runs in seconds with zero files reprocessed.

- **`--verify`** recomputes every file's content hash even when size/mtime
  look unchanged; catches a content change disguised behind identical
  metadata (e.g. a tool that rewrites a file but preserves its timestamp),
  but still only reprocesses files whose hash actually changed.
- **`--rebuild`** forces full reprocessing (extraction, OCR, chunking,
  embedding) of every file under the given path, ignoring all caching.
  Mutually exclusive with `--verify`.

Changing the embedding model or dimensions in `config.toml` does **not**
retroactively re-embed unchanged files on a plain `index` run. See
[Current Limitations](../README.md#current-limitations). Only
`--rebuild` re-embeds under the new config (also redoing extraction/OCR).

### Duplicate-content handling

Two different files with byte-identical content (an exact duplicate copy,
or two independently-empty files) both index successfully and share one
underlying extraction/embedding cache entry, since the cache is keyed by
content hash rather than file identity.

### Excluded files

Choosing **Remove from VectorDrive** on a page in the desktop app's Review
queue does three things and never touches the original document:

- records the file in `config.toml` as a `[[excluded_files]]` entry
  (root folder + path relative to it, with an absolute-path fallback), so
  future indexing and "Create Markdown" runs skip it;
- deletes only the generated `*.vectordrive.md` sidecar, if one exists;
- purges the file's derived rows (index, chunks, review, handoff data)
  from `vectordrive.db` in a single transaction.

The exclusion survives a full rebuild / `vectordrive reset` because it
lives in `config.toml`, not the derived database. To undo an exclusion,
delete its `[[excluded_files]]` block from `config.toml` and re-index.

### Folder processing controls

Select a folder to use its right-side details panel. The panel can be
collapsed and remembers that preference. While work is running, it shows the
active phase and exact processed/total count.

The primary action is **Index** before a folder has a completed index and
**Re-index** afterward. **Output Markdown files** chooses how that action
works:

- **On:** VectorDrive creates inspectable `*.vectordrive.md` sidecars first,
  then indexes those validated artifacts.
- **Off:** VectorDrive extracts and indexes the source files directly and
  does not create new Markdown sidecars.

The OCR model list is deliberately limited to supported Qwen3-VL 4B/8B
Instruct models that are already installed in local Ollama. Choosing a model
updates VectorDrive's OCR configuration for the next operation. The app never
downloads a model automatically.

### Unsupported and encrypted files

- **Unsupported** file types (anything outside the six extensions above)
  are counted separately in the summary table and never treated as
  failures.
- **Encrypted/password-protected PDFs** and **corrupt/unreadable PDFs**
  are recorded as `failed` (never crashing the run). In "Create Markdown"
  (Phase 1) the stored message begins with a stable label — `encrypted: …`
  or `corrupted: …` — and no sidecar is written for the file. VectorDrive
  has no password-prompt capability, so an `encrypted` file stays unindexed
  until you provide a decrypted copy. See
  [Troubleshooting: Encrypted / corrupt PDFs](troubleshooting.md#encrypted--corrupt-pdfs).

## Searching

```bash
vectordrive search "your query" --mode hybrid   # default
vectordrive search "your query" --mode fts       # SQLite FTS5 only, never contacts Ollama
vectordrive search "your query" --mode vector    # embeddings only
vectordrive search "your query" --top-k 20        # default 10
```

- **`fts`**: exact/keyword match via SQLite FTS5. Fast, no embedding
  needed, works with Ollama stopped.
- **`vector`**: semantic similarity via the embedding index. Needs Ollama
  reachable (creates one query embedding per search).
- **`hybrid`** (default): both, combined by Reciprocal Rank Fusion.
  Generally the best default: catches exact terms `fts` would miss context
  for, and semantic matches `fts` would miss entirely.

### How citations and page numbers work

Every result cites a **path**, **page**, **chunk index**, **source type**
(`native` text extraction vs. `ocr`), an **excerpt**, and a **score**. Page
numbers are the document's own page (DOCX/TXT/MD have no real pagination
and are reported as a single logical page 1. VectorDrive doesn't
fabricate page breaks for formats that don't have them). The excerpt is
the actual chunk text that matched, never the full document.

## Chatting with indexed documents

Open the desktop app and choose **Chat** in the sidebar. Select an installed
Ollama chat model, enter a message, and press Enter or **Send**. Raw `base`
and fill-in-the-middle completion models are intentionally omitted because
they do not reliably follow chat roles.
Responses stream into the conversation as the local model produces them.

**Use indexed documents** is enabled by default. For each document question,
VectorDrive runs the existing hybrid search and supplies at most four bounded
chunks to Ollama. A conversational greeting or thanks bypasses
retrieval and receives an ordinary chat response even while the option is on;
a mixed message such as “Hi, what does my report say?” still uses the index.
The source cards beneath the answer come directly from those retrieval
results (not from citation text invented by the model) and include the
file, page, section when available, excerpt, Open, and Reveal.
Turn grounding off to use the same tab as an ordinary local Ollama chat.

- **Stop** cooperatively cancels the current response.
- **New Chat** cancels any active response and clears the conversation.
- Enter sends; Shift+Enter inserts a newline.
- The last twelve user/assistant messages are used as conversational context.
- Conversations remain only in memory and are cleared when VectorDrive exits.

Document excerpts are treated as untrusted reference text. The model receives
no tools, filesystem access, MCP access, or ability to modify documents.

## `status`

```bash
vectordrive status
```

Shows the latest indexing run's summary, a per-status file count
(`indexed`/`skipped`/`unsupported`/`failed`), and the active embedding
model, dimensions, and vector backend (`sqlite_vec` or the NumPy
fallback).

Next: [Storage, Backup & Recovery](storage-and-backup.md) for where all of
this data lives, or [Claude Desktop & MCP](mcp-claude-desktop.md) to query
your index from Claude.
