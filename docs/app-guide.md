# VectorDrive App Guide

A concise end-to-end guide for the packaged `VectorDrive.app`: install,
launch, configure, understand OCR and failure labels, and recover from the
common problems. Each section links to the fuller reference doc.

## 1. Install

Full steps: [Installation](installation.md). In short, on macOS / Apple
Silicon:

1. `brew install python@3.12 ollama`
2. `ollama serve` (once), then `ollama pull qwen3-embedding:8b`
   (and, optionally, a chat model such as `ollama pull qwen2.5:3b`).
3. Build the desktop app (see [Release procedure](release.md)):
   ```bash
   .venv/bin/python scripts/build_app.py
   ```
   This produces `dist/VectorDrive.app`.

`vectordrive doctor` (or the app's **Overview** screen) verifies SQLite
loadable extensions, the sqlite-vec backend, Ollama reachability, and the
embedding model.

## 2. Launch the packaged app

- **Finder:** double-click `dist/VectorDrive.app` (or copy it to
  `/Applications` first). On the first launch macOS Gatekeeper may need
  right-click → **Open** once, because the bundle is ad-hoc signed, not
  notarised.
- **Terminal (same bundle, useful for logs):**
  ```bash
  open dist/VectorDrive.app
  # or, to see stdout/stderr:
  dist/VectorDrive.app/Contents/MacOS/VectorDrive
  ```
- **Custom data location:** set `VECTORDRIVE_HOME` before launching to keep
  the database and config somewhere other than the default
  `~/Library/Application Support/VectorDrive`:
  ```bash
  VECTORDRIVE_HOME=/path/to/vectordrive-data open dist/VectorDrive.app
  ```

Ollama must be running for indexing and for vector/hybrid search; `fts`
search works without it. Closing the window exits the app (any running job
is cancelled first).

Headless self-test modes (no GUI window) are documented in
[Release procedure §4](release.md#4-verify-the-build) — use them to confirm
a build works without clicking through the UI.

## 3. Configure

Configuration lives in `VECTORDRIVE_HOME/config.toml` (default
`~/Library/Application Support/VectorDrive/config.toml`). The desktop app's
**Settings / Advanced** screen edits the common values; you can also edit
the file directly while the app is closed.

| Key | Default | Meaning |
|---|---|---|
| `indexed_folders` | `[]` | Folders included in the index. Manage these from the **Folders** screen, not by hand. |
| `ollama_endpoint` | `http://localhost:11434` | Local Ollama base URL. **Loopback only** (`localhost` / `127.0.0.1` / `::1`); a remote host is rejected so document text can't leave the machine. |
| `embedding_model` | `qwen3-embedding:8b` | Embedding model (must already be in `ollama list`). |
| `embedding_dims` | `1024` | Embedding width. Changing model/dims needs a Full Rebuild to re-embed existing files. |
| `embedding_batch_size` | see defaults | Chunks per embedding request; raise to cut request count. |
| `[ocr].phase1_markdown_vision_model` | `qwen3-vl:8b-instruct` | Vision model used for "Create Markdown" (Phase 1) OCR. |
| `[ocr].tier3_paddleocr_vl_enabled` | `true` | PaddleOCR-VL escalation for legacy indexing. Excluded from the packaged app (source install only). |
| `[ocr].tier4_tesseract_enabled` | `false` | Tesseract last-resort tier (source install only). |
| `[structure].enabled` | `false` | PP-StructureV3 layout analysis (source install only). |

See [Storage & Backup](storage-and-backup.md) for `VECTORDRIVE_HOME`
placement rules (never a synced cloud folder) and [OCR](ocr.md) for the
tier details.

## 4. OCR behavior

- **Native text is trusted first.** A PDF page with a real, clean embedded
  text layer is never sent to OCR. A page is routed to OCR when raster
  images dominate it, its text layer is garbled, or it has almost no text —
  biased toward OCR when uncertain.
- **"Create Markdown" (Phase 1)** uses a local **Qwen3-VL** vision model
  via Ollama to transcribe each OCR page to Markdown, and validates GFM
  table syntax with one bounded corrective retry.
- **Legacy `vectordrive index`** uses the tiered RapidOCR → PaddleOCR-VL →
  Tesseract stack (escalating only on low RapidOCR confidence).
- **Nothing leaves the machine.** The OCR endpoint is loopback-only, proxy
  env vars are ignored, and redirects are refused.
- **A page that genuinely can't be transcribed fails visibly.** Phase 1
  keeps a diagnostic sidecar for review and marks the file failed; it never
  produces an empty "successful" sidecar. Legacy indexing records a
  per-file `ocr_warning`.

## 5. Failure labels

VectorDrive never edits your source files and never lets one bad file stop
a run. Each file ends a run in exactly one state:

| State | Meaning |
|---|---|
| `indexed` / `ready` | Processed successfully. |
| `skipped` | Unchanged since the last run. |
| `unsupported` | Not a supported type and not text-like (audio, video, an archive, a binary). Not an error; still findable by filename. |
| `failed` | A supported file VectorDrive could not process. The stored message begins with a stable label. |

Failure labels on a `failed` file (shown in the message / Errors list):

- **`encrypted: …`** — the PDF is password-protected. VectorDrive never
  attempts passwords. Provide a decrypted copy to index it.
- **`corrupted: …`** — the PDF is malformed, truncated, or has no readable
  pages (including a document MuPDF can only open in repair mode with zero
  pages). No empty sidecar is created for it.

The stored message is sanitized: it never contains the filename, document
text, or a raw library error string.

## 6. Troubleshooting

Full list: [Troubleshooting](troubleshooting.md). Most common:

| Symptom | Fix |
|---|---|
| Vector/hybrid search or indexing errors mentioning Ollama | `ollama serve`, then retry. `fts` mode still works offline. |
| `doctor` flags the embedding model | `ollama pull qwen3-embedding:8b`. |
| A scanned PDF or image `failed` with `corrupted:` / `encrypted:` | See §5. Provide a clean/decrypted copy. |
| File `failed` reading from Google Drive | Set the folder to "available offline" so real bytes are on disk. |
| Blank white app window, or unstyled modal | Webview CSP / stale build — rebuild with `scripts/build_app.py`. See [GUI: CSP](gui.md#content-security-policy). |
| Packaged MCP tools listed but never respond | Packaging-exclude regression — see [Release: build failure modes](release.md#build-failure-modes). |
| Database "appears corrupt" | Restore a backup, or delete `VECTORDRIVE_HOME/vectordrive.db` and re-index. Source files are untouched. |
