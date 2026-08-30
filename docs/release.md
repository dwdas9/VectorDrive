# Release Procedure

Packaging, versioning, and release steps for the macOS `.app` build. See
[Developer Guide](developer-guide.md) for the day-to-day test workflow this
builds on.

## 1. Versioning

The single version source is `src/vectordrive/__init__.py`'s
`__version__`. `pyproject.toml` reads it dynamically
(`version = { attr = "vectordrive.__version__" }`); bump it in exactly one
place. It's reflected in `--version`, the GUI's About dialog, and the
packaged bundle's Finder metadata.

## 2. Build-machine prerequisites

These are needed only on the machine that *builds* the app, never on the
machine that *runs* it:

```bash
brew install openssl@3   # fixes a bundled-OpenSSL symbol conflict between
                          # cv2's vendored libcrypto/libssl and Python's _ssl
pip install typer         # needed only for PyInstaller's own submodule
                          # probe of mcp.cli, excluded from the runtime bundle
```

## 3. Build

```bash
.venv/bin/python scripts/build_app.py
```

Produces `dist/VectorDrive.app`. The build bundles RapidOCR's runtime deps
(`cv2`, `onnxruntime`, `--collect-all rapidocr`/`onnxruntime`) since Tier 2
OCR is always constructed; `pydantic`/`mcp` (`--collect-all`, excluding
`mcp.cli`) for the MCP server; and `sqlite_vec`'s data files
(`--collect-data`). PaddleOCR-VL/Tesseract deps stay excluded (both off by
default, lazily imported only when enabled). A post-build step swaps in the
Homebrew `openssl@3` build to resolve the `_ssl`/`cv2` conflict and
re-signs the bundle (ad-hoc signing).

## 4. Verify the build

```bash
.venv/bin/pytest -m packaging   # builds a real wheel, inspects contents
```

Then exercise the actual packaged binary directly, without opening the GUI
window: these headless self-test modes exist specifically so a build can
be verified without manual clicking:

```bash
# Index + search a folder, print a JSON report of what was found:
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest <folder> <unique-word> [more-words...]

# Confirm folder removal actually purges the index (not just the UI list):
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest-removal <folder-with-a-file> <unique-word>

# Confirm a removal survives a real process restart:
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest-search-only <unique-word>

# Confirm the packaged MCP Connect flow works end-to-end:
VECTORDRIVE_HOME=/tmp/vd-check dist/VectorDrive.app/Contents/MacOS/VectorDrive \
  --selftest-mcp-connect
```

Each prints a JSON report; check every key under `"checks"`/`"status"` is
the expected value rather than eyeballing exit codes.

Signature check:

```bash
codesign --verify --deep --strict dist/VectorDrive.app
```

## 5. Manual acceptance

Dev-mode testing does not exercise PyInstaller's frozen bundle layout or a
real WKWebView's CSP enforcement; several real bugs have only ever
surfaced by launching the actual packaged `.app` (see
[GUI: Content Security Policy](gui.md#content-security-policy)
for what to watch for). Per this project's standing policy, Claude does not
self-drive the GUI for acceptance testing; ask the user to manually launch
`dist/VectorDrive.app`, run an index, search, and (if relevant) Chat and
Claude Desktop Connect, before considering a release done.

## 6. Tag and record

1. Update `CHANGELOG.md` with the new version's entry.
2. Commit, tag (`git tag -a vX.Y.Z`), push branch and tag.
3. Update `docs/CURRENT_CHECKPOINT.md`'s release/status lines.

## Build failure modes

- **Blank white window / bridge never initializes:** see
  [GUI: Content Security Policy](gui.md#content-security-policy).
- **OCR silently produces no searchable text in the packaged app but not
  in dev mode:** almost always a packaging-exclude regression; confirm
  `cv2`/`onnxruntime` are still bundled (`--collect-all rapidocr`,
  `--collect-all onnxruntime` in `scripts/build_app.py`).
- **MCP tools listed but every call errors:** confirm `--collect-all
  pydantic` and `--collect-all mcp` are still present; pydantic v2's
  `BaseModel` re-export is dynamic and PyInstaller's static analysis can
  silently miss it.
