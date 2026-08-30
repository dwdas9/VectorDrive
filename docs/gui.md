# VectorDrive GUI

A local desktop interface for VectorDrive: Folders, Search, local Chat,
Overview, Logs, and Claude Desktop, plus Settings/Advanced for configuration
and maintenance. Built with pywebview (WKWebView on macOS) and packaged with
PyInstaller (onedir); see [Architecture](architecture.md) for how the GUI
layer sits over the same services the CLI uses.

## Screens

- **Overview**: health checks, index stats, next-step guidance.
- **Folders**: native folder picker, preview scan, add/remove, background
  indexing with live progress, cancel, Relink for a moved/renamed root.
- **Search**: FTS/vector/hybrid mode switch, ranked results, Open/Reveal.
- **Chat**: see below.
- **Claude Desktop**: detect/quick-test/deep-test the MCP server,
  connect/disconnect with a config diff preview, backup/restore.
- **Settings/Advanced**: file logging, integrity check, database
  backup/restore, VACUUM, WAL checkpoint, log viewer.

All Claude Desktop config operations use temp paths in tests and never touch
a user's real config outside of an explicit Connect/Disconnect action.

## Local Chat tab

Chat is a vanilla webview screen like the rest of the GUI; JavaScript never
contacts Ollama directly and the CSP remains `connect-src 'none'`. The Python
bridge discovers chat-suitable models, runs the existing hybrid retriever,
and streams Ollama NDJSON through a dedicated worker onto the shared event
bus, keeping the UI responsive and avoiding queuing chat behind indexing.

Grounded turns retrieve at most four chunks and display their source cards
independently of model-authored citation markers. Clearly conversational
greetings and thanks skip retrieval; a greeting combined with a document
question remains grounded. Raw base/FIM completion models are omitted from
the picker because they do not reliably follow chat roles. All user,
document, and model content is rendered as plain text. Transcripts and
transient stream events stay only in process memory; only the selected
model is saved in GUI state.

## Toolkit decision

pywebview + bundled vanilla HTML/CSS/JS, packaged with PyInstaller (onedir);
no PySide6 fallback. Confirmed on macOS/Apple Silicon: window loads local
HTML/CSS/JS with no remote fetch, JS↔Python round-trips work with dict
arguments, background jobs don't block the UI, and sqlite-vec's `vec0.dylib`
loads correctly from a frozen bundle (requires `--collect-data sqlite_vec`
in the PyInstaller build. See `scripts/build_app.py`).

## Content Security Policy

`index.html` loads via a `file://` URL (not pywebview's `html=<string>`
parameter, which leaves relative asset paths like `<link
href="styles.css">` unresolved); still local file access, no HTTP
server. The CSP is `connect-src 'none'` (no network egress at all) with
two narrow, deliberate exceptions:

- **`script-src 'unsafe-eval'`**: pywebview's native
  `window.pywebview.api` bridge injection and `evaluate_js()` calls go
  through WKWebView's `evaluateJavaScript`, which its CSP enforcement
  treats as `eval()`. Removing this stops the bridge from initializing at
  all. It does not weaken `connect-src 'none'`, `frame-src 'none'`, or
  `object-src 'none'`: the properties that actually matter for "no
  remote content."
- **`style-src 'unsafe-inline'`**: the app's only mechanism for inline
  layout/positioning is `components.js`'s `el()` helper setting
  `style=""` directly. Without this, those attributes are silently
  dropped (present in the DOM, never applied as CSS, no console error);
  breaking spacing, modal centering, and anything else set from JS.

## Shutdown

Closing the window performs a hard `os._exit()`, not a graceful run-loop
stop. pywebview's Cocoa backend's `NSApp.stop_()` is not reliably
observed on the next run-loop pass. `gui/app.py` cancels any active job
first (bounded `CLOSE_CANCEL_TIMEOUT_S`, showing a "Finishing up…" toast),
then calls `os._exit()` unconditionally. This is safe because every write
path uses SQLite WAL mode, whose crash-safety guarantees an
abruptly-killed writer leaves a recoverable, never corrupt, database.

## Folder-removal semantics

Removing a folder deletes its registration and every index row
(`files`/`extracted_docs`/`pages`/`chunks`/`chunk_runs`, cascaded via FK +
trigger; `fts_chunks`, trigger-synced) belonging **exclusively** to it; a
file also covered by another still-registered (overlapping/nested) folder
is preserved. sqlite-vec's `vec_chunks` (no FK/trigger tie to `chunks`) is
explicitly refreshed immediately after the purge, so vector/hybrid search
agree with FTS right away, not just after the next full index run. The DB
purge commits (or rolls back) *before* the folder is deregistered from
`config.toml`, so a failure partway through never leaves an
orphaned-index-but-deregistered state. Source documents on disk are never
touched.

## Packaging

`scripts/build_app.py` builds `VectorDrive.app` via PyInstaller. RapidOCR's
runtime deps (`cv2`, `onnxruntime`) are bundled unconditionally (Tier 2 OCR
is always constructed); PaddleOCR-VL/Tesseract deps stay excluded (both
off by default, lazily imported). See [Release procedure](release.md) for
the full build/sign/verify steps and known build-machine prerequisites.

## Accessibility

Landmarks, ARIA roles, focus management, `prefers-reduced-motion`,
`aria-live` regions, dialog focus trapping, and status pills that never
rely on color alone.
