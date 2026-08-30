# Installation and Setup

**Validated platform:** macOS on Apple Silicon, Python 3.12, via Homebrew's
`python@3.12`.

**Not yet validated: Windows and Linux.** VectorDrive uses no macOS-only
APIs (SQLite, sqlite-vec, PyMuPDF, python-docx, RapidOCR, and Ollama's HTTP
API are all cross-platform), but no install or run has been verified there.
Treat Windows/Linux as untested, not unsupported.

## 1. Python

```bash
brew install python@3.12
```

Homebrew's `python@3.12` is required, not the system Python or a
python.org installer; those don't reliably support
`sqlite3.enable_load_extension()`, which the sqlite-vec backend needs.
`vectordrive doctor` (below) checks this for you.

## 2. Virtual environment

```bash
cd /path/to/vectordrive
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,ocr]"
```

- `dev`: pytest, pytest-cov, `build` (for packaging checks).
- `ocr`: RapidOCR + onnxruntime, the default (Tier 2) OCR engine. Required
  for indexing any image-only PDF page, JPG, or PNG.

Two further extras exist and are optional (off by default even if
installed; see [OCR](ocr.md)):

```bash
.venv/bin/pip install -e ".[paddleocr-vl]"   # Tier 3, ~1.93GB model download, ~9.7GB RAM once loaded
.venv/bin/pip install -e ".[tesseract]"       # Tier 4, also needs `brew install tesseract` (the binary itself)
```

## 3. Ollama and local models

VectorDrive embeds every chunk and every query locally through Ollama;
nothing is ever sent to a remote embedding API.

```bash
brew install ollama      # or download from https://ollama.com
ollama serve              # if it isn't already running as a background service
ollama pull qwen3-embedding:8b
ollama pull qwen2.5:3b       # optional completion model for the Chat tab
```

`qwen3-embedding:8b` (used at 1024 dimensions, via the model's supported
Matryoshka truncation from its native 2560) is the only embedding model
v0.1 supports. VectorDrive never pulls, swaps, or auto-downloads a model;
if it isn't already in `ollama list`, indexing and vector/hybrid search
will fail with a clear error until you pull it. `search --mode fts` never
needs Ollama at all.

The desktop Chat tab lists chat-suitable models already installed in Ollama.
It excludes embedding-only models as well as raw `base` and fill-in-the-middle
completion models, which do not reliably follow chat roles. VectorDrive never
downloads or deletes models. `qwen2.5:3b` is a practical small default, but it
is not selected silently: choose it (or another installed chat model) in Chat.
Chat accepts only an Ollama endpoint on this computer's loopback interface;
indexed excerpts are never sent to a remote host.

## 4. Verify the install

```bash
.venv/bin/vectordrive doctor
```

`doctor` checks, in order: SQLite loadable-extension support, the
sqlite-vec backend (a real insert+KNN round-trip, not just an import),
Ollama reachability and the embedding model's presence, PyMuPDF,
python-docx, RapidOCR, whichever optional OCR tiers are installed, and that
your data directory resolves outside this repository. Only the
**required** checks (SQLite extensions, PyMuPDF, python-docx,
sqlite-vec-or-NumPy-fallback) must pass; Ollama and the optional OCR
tiers are advisory (`[WARN]`, not `[FAIL]`) and don't block use.

Exit code `0` means every required check passed. Continue to the
[User Guide](user-guide.md).
