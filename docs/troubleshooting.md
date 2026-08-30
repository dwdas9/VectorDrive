# Troubleshooting

## Ollama unavailable

- `fts` search always works without Ollama.
- `vector`/`hybrid` search and indexing's embedding stage need it. A
  clear error names the problem (`Could not connect to Ollama at
  http://localhost:11434 (is it running?)`), not a raw traceback.
- Fix: `ollama serve`, then retry.
- If Ollama goes down *during* an `index` run: extraction/OCR/chunking
  still succeed for a file and it's marked `indexed` (so `fts` search
  still finds it), but its embeddings fail and are recorded with a clear
  error, so it silently won't appear in `vector`/`hybrid` results until you
  re-index with Ollama reachable. `status` doesn't currently flag this
  case specially; check `doctor` before an indexing run if vector search
  matters to you.

### Chat cannot list models or generate a response

- Start Ollama with `ollama serve`, then use **Retry** in the Chat tab.
- Chat shows only installed models suitable for role-based conversation;
  embedding-only, raw `base`, and fill-in-the-middle completion models are
  intentionally hidden. Install a chat model with, for example,
  `ollama pull qwen2.5:3b`.
- Chat is deliberately restricted to `http://localhost`, `127.0.0.1`, or
  `::1`. A remote/LAN Ollama endpoint is rejected so indexed excerpts cannot
  leave this computer through a configuration mistake.
- Grounded chat needs the embedding model as well as the selected completion
  model. If hybrid retrieval fails, check `qwen3-embedding:8b` or temporarily
  turn off **Use indexed documents**.
- Ollama may load the embedding and completion models one after the other.
  On a memory-constrained Mac, choose a smaller completion model; Ollama may
  evict one model while loading the other, which increases first-token delay.

## Embedding model missing or mismatched

- `doctor` reports `ollama_reachable` as `[FAIL]`/`[WARN]` if
  `qwen3-embedding:8b` isn't in `ollama list`; run
  `ollama pull qwen3-embedding:8b`.
- If you change `embedding_model`/`embedding_dims` in `config.toml` after
  indexing, searching against the new config against chunks embedded
  under the old one gives you nothing new. See
  [Current Limitations](../README.md#current-limitations). You need
  `--rebuild` to get embeddings under the new config, which also redoes
  extraction/OCR.
- A vector index built under one embedding config, queried under a
  mismatched one (e.g. wrong dimensions), fails with a clear
  `dimension mismatch` error rather than returning wrong results silently.

## Encrypted / corrupt PDFs

Recorded as `failed`, never silently skipped, never crashing the rest of
the run. "Create Markdown" (Phase 1) tags each with a stable label at the
front of the stored message, and writes **no sidecar** for the file:

- **`encrypted: PDF is password-protected; VectorDrive never attempts
  passwords`** — a user-password-protected PDF. Provide a decrypted copy to
  index it.
- **`corrupted: …`** — a malformed or truncated PDF, or one whose object
  streams are damaged enough that MuPDF recovers zero pages. A supported
  file with zero extracted pages is always a failure, never an empty
  "successful" sidecar.

The stored message is sanitized: no filename, no document text, no raw
library error string. Legacy `vectordrive index` (direct, no sidecar)
records the same files as `failed` with the underlying PyMuPDF error in its
`Errors` table.

## Unsupported formats

Anything outside `.pdf`/`.docx`/`.txt`/`.md`/`.markdown`/`.jpg`/`.jpeg`/
`.png` is counted as `unsupported`, not an error. This is not
configurable.

## Google Drive files not downloaded locally ("online-only" placeholders)

Google Drive's desktop client can leave files as cloud-only placeholders
that aren't actually present as real bytes on disk. VectorDrive reads
whatever the filesystem gives it. A placeholder that hasn't been
materialized locally will fail extraction with a file-read error. Make
sure "available offline" / a full local sync is set for the folder you
intend to index before running `index --path`.

## Corrupt database

A database file that exists but isn't valid SQLite (distinct from a
*missing* database) is caught and reported with a plain error, both by the CLI
(`error: the database at ... appears corrupt: ...`, exit code 1) and by
the MCP server (the server still starts and lists its tools; every tool
call gets a clear error instead of the whole process crashing). Recovery:
restore from a backup (see [Storage & Backup](storage-and-backup.md)), or
delete the database and re-index (source documents are never affected;
the database is a derived catalogue, not your data).

## Packaged app: MCP tools listed but never respond, or Connect fails {#packaged-app-mcp-connect}

Root causes and fixes for this are packaging-configuration issues, not
runtime bugs. See [Release: build failure modes](release.md#build-failure-modes).
To verify a build is healthy without opening Claude Desktop:

```bash
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest-mcp-connect
```

This exercises the exact Connect flow the GUI button uses and prints a
JSON report. To confirm tool calls actually work, spawn
`dist/VectorDrive.app/Contents/MacOS/VectorDrive --mcp` (with
`VECTORDRIVE_HOME` set) as an MCP client would. See
`mcp.client.stdio.stdio_client` / `mcp.client.session.ClientSession`, the
same library `services/mcp_probe.py`'s `deep_probe()` uses.

## Removed folder still shows up in search

Removing a folder purges every index row exclusively owned by it
(cascaded via FK + trigger) and refreshes the vector index immediately.
See [GUI: folder-removal semantics](gui.md#folder-removal-semantics). If
you see stale results after a removal, confirm you removed the folder
through the Folders screen (not just by editing `config.toml` directly),
then verify without clicking through the GUI:

```bash
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest-removal <folder-with-a-file> <unique-word-in-that-file>
```

Prints a JSON report; every key under `"checks"` should be `true`. Follow
with `--selftest-search-only <unique-word>` against the same
`VECTORDRIVE_HOME` (a fresh process launch) to confirm the removal
survives a restart.

## Packaged app: OCR silently fails / file shows "Indexed" despite no searchable text {#packaged-app-ocr-silently-fails}

If indexing an image or scanned PDF reports an unqualified `Indexed`
status with no searchable text, that's a packaging-configuration issue.
See [Release: build failure modes](release.md#build-failure-modes). A
correctly-built app still records a populated `ocr_warning` (visible via
`status`, the Folders screen, and Overview's warning count) for any
*specific* file that legitimately fails OCR (corrupt image, all OCR tiers
exhausted). That's expected, not a bug.

To verify a build is healthy without clicking through the GUI:

```bash
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest <folder-with-an-image> <unique-word-in-that-image>
```

Prints a JSON report; check `index_job.status == "done"` and that the
token shows up in `searches.<token>.fts.data.results`.

## Packaged app shows a blank white window, or a modal is unstyled/uncentered

Both symptoms trace to the webview's Content Security Policy. See
[GUI: webview/CSP gotchas](gui.md#content-security-policy) for what's
required and why. Rebuild with `scripts/build_app.py` from a current
checkout.

## Path and permission problems

`index --path` requires a real, readable directory. A missing or
inaccessible path fails immediately with a clear `not a folder: ...`
message before any processing starts.

## Slow OCR / indexing

- The bulk of indexing time is OCR + embedding network round-trips, not
  extraction. Re-running `index` on an unchanged folder is fast: unchanged
  files are skipped by a size/mtime check without even being re-hashed.
- If PaddleOCR-VL (Tier 3) is enabled, expect ~16.5s cold / ~6.5s warm
  *per escalated page*. See [OCR](ocr.md). Leaving it disabled (the
  default) avoids this entirely; RapidOCR alone is the fast path.
- `embedding_batch_size` in `config.toml` controls how many chunks are
  sent to Ollama per HTTP request; raising it reduces request count at
  the cost of larger individual requests.

## Embedding failures

Recorded per-chunk in the database with a clear `error_message`
(`success = 0` in the `embeddings` table); never silently dropped or
retried forever. See "Ollama unavailable" above for the most common
cause.

## Safe recovery without damaging source documents

VectorDrive never writes to a source document under any failure mode
described above. Every recovery path above only touches
`VECTORDRIVE_HOME` (the database, cache, config), never the files you
indexed. If in doubt, re-run `vectordrive index --path ... --verify`. It
only reads and hashes your source files, never writes to them.
