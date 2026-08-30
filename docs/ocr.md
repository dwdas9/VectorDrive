# OCR

VectorDrive currently has two separate OCR paths, built from Config by two
separate functions in `services/engines.py`:

- **Phase 1 "Create Markdown"** (`build_extraction_engines`,
  `services/markdown_generation_service.py`) — generates the
  `*.vectordrive.md` sidecar for a file. Its OCR engine is **Qwen3-VL**, a
  local vision-language model, described immediately below.
- **Legacy indexing** (`build_index_engines`, `pipeline/indexer.py`) — the
  tiered RapidOCR -> PaddleOCR-VL -> Tesseract stack, unchanged, described
  in the rest of this document. Indexing does not yet read from Phase 1's
  validated sidecars, so it still runs this older pipeline itself; that
  changes in a future Phase 2.

Both paths route native-text pages around OCR entirely (Tier 1 below), and
both cache every OCR result, so re-running OCR on an already-resolved page
never re-invokes any engine.

When a page still has native text alongside a `needs_ocr` OCR result (e.g. a
watermark over a full-page scan), the two paths merge that native text
differently, via `ocr_needs_ocr_pages`'s `preserve_ocr_structure` option
(`pipeline/ocr/page_ocr.py`), default `False`:

- **Legacy indexing** leaves this at its default and keeps
  `merge_native_and_ocr_text`'s line-by-line, containment-based dedup
  (`pipeline/ocr/merge.py`) — correct for plain-text tiered OCR output, but
  it unconditionally drops blank lines and punctuation-only lines as noise.
- **Phase 1 Markdown generation** passes `preserve_ocr_structure=True`,
  because its OCR result is validated structured Markdown (Qwen3-VL), not
  plain text. A real-corpus finding showed the legacy line-by-line merge
  silently destroyed GFM table delimiter rows (`|---|---|`) and
  paragraph/table blank-line boundaries in an otherwise-valid Qwen
  transcription. When this option is set, `merge_native_with_structured_ocr_markdown`
  is used instead: it never touches the OCR Markdown's lines. If the native
  text's word tokens are already substantially covered by the OCR
  transcription, the OCR Markdown is returned byte-for-byte unchanged;
  otherwise the native text is prepended (with a blank-line separator) ahead
  of the untouched OCR block. The page's `text_source` is still `'merged'`
  in either case (both sources were considered), and provenance
  (`ocr_result_id`) still points at the selected OCR result.

The two paths' *extraction-stage* cache rows — the `extracted_docs`/`pages`
rows that carry each page's `needs_ocr` routing flag and resolved text —
are isolated from each other, not shared. Legacy indexing's
`extract_with_cache` call (`pipeline/extract/cache.py`) keys its row on the
extractor's own `extractor_version` alone, unchanged. Phase 1 Markdown
generation instead passes an explicit `extractor_version_override` —
`extractor.VERSION` plus the configured tier2 OCR engine's `NAME` and
`MODEL_VERSION` (`_phase1_extractor_cache_version` in
`services/markdown_generation_service.py`) — which opens a separate cache
namespace/row under the same `(content_hash, extractor_name)`. This
prevents two failure modes: an old RapidOCR-resolved `extracted_doc` (from
a file legacy indexing already ran, which flips a page's `needs_ocr` from
1 to 0 with RapidOCR text) silently suppressing Qwen because Phase 1 reused
that row; and changing the configured Qwen model or prompt version
silently reusing page-routing state resolved under a previous Qwen
configuration. A miss under the override always inserts a new row rather
than updating an existing one, so neither path can ever mutate the other's
row. Native pages still bypass OCR normally within Phase 1's isolated row.

## Phase 1: Qwen3-VL, the Markdown-generation OCR engine

`pipeline/ocr/qwen_vl_engine.py`'s `QwenVlOcrEngine` is the sole OCR engine
`build_extraction_engines` constructs for every `needs_ocr` page during
Markdown generation — no RapidOCR, no PaddleOCR-VL, no Tesseract, no
tier3/tier4 escalation. It calls a **local** Ollama instance's
`POST /api/chat` (default model **`qwen3-vl:8b-instruct`**, default
endpoint `config.ollama_endpoint`, 600s request timeout) with the page
image and a document-neutral, versioned prompt requiring exact visible
transcription, `[unclear]` markers instead of guessed text, preserved
headings, separate tables reconstructed only where visually clear,
each of several documents/regions on a page kept separately, no outside
knowledge, and Markdown-only output. A single optional outer ` ```markdown`
/ ` ```md` fence wrapping the *entire* response is stripped; nothing else
about the model's output is rewritten.

- **Local-only, by construction:** the configured endpoint must be an
  explicit `http`/`https` loopback host (`localhost`, `127.0.0.1`, `::1`)
  with no credentials, path, query, or fragment — checked and rejected
  *before* any request is made. The HTTP client never trusts
  `HTTP(S)_PROXY`/`NO_PROXY` and never follows redirects, so a page image
  can never be silently routed off this machine.
- **No confidence, honestly:** Qwen3-VL is a vision-language model, not a
  confidence-scored OCR engine — `OcrResult.mean_confidence` is always
  `None` here (never fabricated as 1.0 or any other number) and no
  `OcrLine`/bounding-box data is invented (`lines` stays empty), the same
  honesty PaddleOCR-VL already has (see below). Because Phase 1 configures
  no tier3/tier4 escalation engine, `pipeline/ocr/tiered.py`'s
  confidence-threshold gate never gets a chance to escalate a successful,
  None-confidence Qwen result anywhere — it's used as-is. No change was
  needed to the tiered-escalation policy itself for this.
- **Caching/provenance:** engine results are cached exactly like every
  other tier (`page_image_hash, engine_name, model_version, lang,
  config_hash)`), and `MODEL_VERSION` embeds both the configured model tag
  and a prompt/settings version string — changing the prompt or the fixed
  request options (`temperature=0, seed=42, num_ctx=16384,
  num_predict=4096, think=false`) bumps that version, busting the cache
  rather than silently reusing a result produced under the old prompt.
  Separately, the *extraction-stage* row (each page's `needs_ocr`/text
  state, before any OCR engine runs) is also namespaced by this same
  `NAME`/`MODEL_VERSION` pair, isolated from legacy indexing's own
  extraction row — see the cache-isolation paragraph above.
- **Configuring the model:** `phase1_markdown_vision_model` under `[ocr]`
  in `config.toml` (default `qwen3-vl:8b-instruct`); the endpoint is the
  existing top-level `ollama_endpoint`.
- **Table syntax gate, one bounded retry:** every response is checked by a
  conservative, standard-library-only structural validator
  (`validate_markdown_tables`) for pipe-table syntax outside fenced code
  blocks — a GFM delimiter row must immediately follow its header row (never
  precede it), and consecutive pipe-delimited rows must resolve to a valid
  delimiter with a matching column count. This is a **syntax-only** check:
  it says nothing about whether a structurally valid table's contents are
  actually correct, and it never rewrites OCR output. If the first response
  fails it, the engine retries exactly once with the same image/model/
  settings and a short corrective prompt restating the GFM rules (the
  invalid first response's text is never sent back to the model). If the
  retry is also invalid, the page fails visibly with `QwenVlOcrError` — a
  content-safe message with no extracted text or image data — rather than
  silently caching malformed Markdown as the page's sole searchable
  payload.
- **A file that can't be extracted at all fails before OCR runs.** A
  password-protected PDF, or one MuPDF can only open in repair mode with
  zero pages, raises `DocumentUnreadableError` in `Phase1PdfExtractor`
  (`pipeline/extract/pdf_page_router.py`). Phase 1 records the file
  `failed` with a sanitized message prefixed by a stable label
  (`encrypted: …` / `corrupted: …`) and persists **no sidecar** — a
  supported document with zero extracted pages never becomes a successful,
  searchable sidecar. If a page *is* extracted but its OCR ultimately
  fails, Phase 1 keeps a diagnostic sidecar (page `mode='failed'`), marks
  the file `failed`, and Phase 2 refuses to index it.

## Legacy indexing: tiered OCR (Tiers 1-4)

The rest of this document describes `build_index_engines`'s tiered
RapidOCR -> PaddleOCR-VL -> Tesseract stack, used by `vectordrive index`
today. It resolves text for a page in tiers, cheapest and most reliable
first.

## Tier 1: native text extraction (both paths)

PDF pages with real, trustworthy embedded text, and DOCX/TXT/MD files,
never touch OCR at all. Text comes directly from PyMuPDF (PDF) or
python-docx (DOCX).

A PDF page is routed by **composition**, not just how much native text it
has (`pipeline/extract/pdf.py`): a page is flagged `needs_ocr` if its
raster images cover at least 30% of the page area (`RASTER_AREA_THRESHOLD`),
*or* its native text is at least 2% Unicode replacement/control characters
(`GARBLED_RATIO_THRESHOLD`, a broken font/encoding mapping), *or* it simply
has fewer than 20 non-whitespace characters (`MIN_NATIVE_CHARS`). Any one
signal is enough to route to OCR — deliberately biased toward OCRing when
uncertain, since a wasted OCR pass is cheap and a silently-skipped document
is not.

This replaced a bare `len(text) >= 20` check that missed pages where a
short watermark or footer (e.g. "Scanned by CamScanner") sits on top of an
otherwise full-page scan: 20+ characters of real text made the page look
native even though the actual content was an unrecognized raster image.
When a page needs OCR, whatever native text it had (the watermark, in that
example) is kept rather than discarded: OCR write-back merges it with the
OCR result (`text_source = 'merged'`) instead of overwriting it, unless
there was no native text at all (`text_source = 'ocr'`).

## Tier 2: RapidOCR (the normal path)

RapidOCR (PP-OCRv5 mobile, on onnxruntime) is the default, always-on OCR
engine, installed via the `[ocr]` extra, required for any real indexing
run that touches image-only PDF pages, JPGs, or PNGs. It runs on every
page that Tier 1 couldn't resolve.

Because it's unconditionally constructed for every indexing run
(`services/engines.py`, no config toggle), RapidOCR's own runtime
dependencies (`cv2`, `onnxruntime`) are **not optional** for the packaged
macOS app: `scripts/build_app.py` must bundle them (`--collect-all
rapidocr`, `--collect-all onnxruntime`). Tier 3/4's deps stay excluded from
the packaged app regardless of the `[ocr]` config defaults below (see
Tier 3's packaged-app note). See
[Troubleshooting](troubleshooting.md#packaged-app-ocr-silently-fails) if a
packaged build reports `indexed` for a file with no searchable text.

## Tier escalation and fallback

```
Tier 2 (RapidOCR) runs first.
  succeeds with confidence >= threshold (default 0.70)  -> done, use it
  fails, OR succeeds with confidence < threshold          -> escalate:
    Tier 3 (PaddleOCR-VL), if enabled -> succeeds -> use it
    Tier 4 (Tesseract), if enabled    -> succeeds -> use it
    otherwise -> fall back to Tier 2's own result if it had one, else a clear failure
```

Escalation is driven purely by RapidOCR's own confidence score. There's no
document-type classifier deciding "this looks hard." A page RapidOCR is
confident about is never sent to Tier 3, so the ~9.7GB PaddleOCR-VL model
(next section) is never loaded unless it's actually needed. Every tier's
result is recorded: a tier that fails is a clearly-logged failure, not a
silent skip.

A real-corpus benchmark (2026-08-23, this M3 Max, sampled receipts and
certificates at real indexing resolution, not the tiny synthetic test
fixtures) measured PaddleOCR-VL at **~300s for a single page** when run
directly, against ~4s for RapidOCR on the same page at 0.96 confidence,
with no accuracy gain from PaddleOCR-VL on that page (it was missing a
line RapidOCR captured). The ~7s "warm predict" figure quoted in Tier 3
below was measured on a 600x200 synthetic image and does not hold at real
page resolution. This confirmed running PaddleOCR-VL as the *primary*
engine for every OCR page is impractical at this corpus's scale; the
escalate-only-on-low-confidence design above is what makes Tier 3 cheap
enough to default on. Across the full 4-file sample: 6.3s total for the
tiered strategy vs. 744.1s for PaddleOCR-VL-as-primary, with RapidOCR's
confidence never dropping below the 0.70 threshold (0 of 4 pages
escalated). One known gap this surfaced: on a bilingual English/Hindi
certificate, RapidOCR was *confident* (0.92) but garbled the Hindi script
into nonsense Latin characters, while PaddleOCR-VL correctly read the
Devanagari text — a wrong-but-confident result that a pure
confidence-threshold escalation can't catch. Not addressed here (out of
this fix's scope); worth revisiting if non-English documents are common
in the corpus.

## Tier 3: PaddleOCR-VL (on by default, escalation-only)

A real, working OCR backend, used only as an escalation target (never as
the primary engine — see the benchmark note above):

- **Install:** `pip install -e ".[paddleocr-vl]"`. Without it, escalating
  to Tier 3 fails cleanly (`PaddleOcrVlUnavailableError`, caught by
  `run_ocr_with_cache` and recorded as a normal cached failure) and the
  page falls back to Tier 2's own result — never a crash, never silent
  data loss.
- **Enable/disable:** on by default (`tier3_paddleocr_vl_enabled = true`
  under `[ocr]` in `config.toml`); set to `false` to opt back out.
- **Packaged macOS app:** `paddle`/`paddleocr`/`paddlex` stay excluded
  from the bundle (~1-2GB of model weights) regardless of this default,
  same limitation as Structure analysis below — a packaged-app install
  degrades to Tier 2 (RapidOCR) only, gracefully, via the same
  cached-failure path. The default-on config only takes effect for a
  source/`pip install -e ".[paddleocr-vl]"` checkout.
- **Model storage:** ~1.93GB, downloaded once to
  `~/.paddlex/official_models` (outside the repo, outside
  `VECTORDRIVE_HOME`) on first real use, then cached.
- **Memory:** ~9.7GB RSS once loaded. Escalation-only usage means this is
  paid only on pages RapidOCR was unsure about, not on every page.
- **Runtime:** ~16.5s cold init + first predict on a tiny synthetic image;
  a single real-resolution page can take on the order of minutes (see the
  benchmark note above) — escalation, not primary use, is what keeps this
  affordable in aggregate.
- **Quality:** better than RapidOCR on hard scans in earlier synthetic
  testing; the one real-document sample checked so far showed no
  advantage over RapidOCR's already-high-confidence result. Not yet
  benchmarked at scale against the real corpus's harder (low-confidence)
  pages specifically — that's what escalation actually exercises.
- PaddleOCR-VL's output carries no per-block confidence score (unlike
  RapidOCR); `OcrLine.confidence` stays `None` for its results rather
  than a fabricated number.

## Tier 4: Tesseract (last resort, off by default)

A real, implemented `OcrEngine` (via `pytesseract`), last resort, never
tried before Tier 2 or Tier 3. Requires
`pip install -e ".[tesseract]"` **and** the `tesseract` binary itself
(`brew install tesseract`, not installable via pip). Enable via
`tier4_tesseract_enabled = true` under `[ocr]`.

## Structure analysis (PP-StructureV3, optional, off by default)

Not a fifth OCR tier: a separate, optional pass over already-extracted
pages that runs *after* Tier 1-4 resolve a page's text, driven by its
own `[structure]` config table rather than `[ocr]`. Off by default: a
full index run with it disabled writes zero `structure_results` rows
and produces chunk text byte-identical to a build without this
feature at all.

- **Install:** `pip install -e ".[paddleocr-vl]"`, the same extra as
  Tier 3, since PP-StructureV3 ships in the same `paddleocr` package.
- **Enable:** set `enabled = true` under `[structure]` in
  `config.toml`. `analyze_native_pages` (default `true`) controls
  whether pages whose text came from native extraction (not OCR) are
  also analyzed; set to `false` to skip them and only structure-analyze
  scanned/OCR'd pages.
- **What it stores:** one `structure_results` row per analyzed page
  image (content-addressed by page image hash, same caching philosophy
  as `ocr_results`) holding the page's reading-order markdown and a
  JSON list of layout blocks (headings, paragraphs, tables, figures,
  headers/footers, with bounding boxes and reading order).
  `pages.structure_result_id` links each page to its result;
  `pages.structure_error` records why a page's analysis failed, if it
  did. A failed page still indexes normally with flat chunking, never
  fails the whole file.
- **What it changes downstream:** a document with at least one
  successfully structure-analyzed page is chunked by the
  structure-aware chunker instead of the flat character-window one:
  headings become `chunks.section` context prefixed onto their
  following paragraphs, tables become GFM markdown tables (split by row
  with the header repeated in every piece, for long tables), and
  `chunks.block_type`/`bbox_json`/`reading_order` carry the source
  block's metadata through to search results.
- **Packaged-app limitation:** the macOS `.app` build excludes
  `paddle`/`paddleocr`/`paddlex` (same as Tier 3/4; see
  `scripts/build_app.py`'s exclude list) to avoid shipping ~1-2GB of
  model weights in every install. Structure analysis (like Tier 3) only
  works from a `pip install -e ".[paddleocr-vl]"` checkout, not the
  packaged app; enabling `[structure]` there records a per-page
  `structure_error` naming the packaged limitation, then falls back to
  normal flat chunking. Normal
  `VectorDrive.app` users therefore do **not** receive structure-aware
  indexing. Use a source/pip installation with the `paddleocr-vl` extra
  when PP-StructureV3 is required.

## Confidence thresholds and caching

- Default escalation threshold: **0.70** mean confidence
  (`DEFAULT_CONFIDENCE_THRESHOLD` in `pipeline/ocr/tiered.py`). Not
  currently exposed as a CLI flag; change it in code if you need a
  different value.
- OCR results are cached by `(page_image_hash, engine_name, model_version,
  lang, config_hash)`: a rendered page image that's byte-identical to one
  already OCR'd (even from a different file) reuses the cached result
  instead of re-running any engine.
- A cached **failure** is recorded as a failure (`success = 0` with a
  clear `error_message`), not silently retried on every run.

## Native+OCR text merge (mixed pages)

When a page has both real native text (`pages.text` from extraction) and
still gets routed to OCR (raster coverage, garbled text, a watermark over
a scan — see composition routing above), the two texts are merged, not
concatenated. `pipeline/ocr/merge.py` keeps the native text as the base
and appends only OCR lines that are **not** already (near-)covered by it,
using deterministic word-token containment scoring (`difflib`), not a
naive whole-string equality check. This avoids the two failure modes a
naive merge has: duplicating most of the page (a full OCR pass
re-transcribes everything the native layer already had) while still
preserving genuinely additional OCR content (handwritten annotations, a
stamp, a signature the native text layer never captured). Only
punctuation-only lines (no alphanumeric content at all — a ruled-line
artifact, a stray dash) are dropped unconditionally; a short but
genuinely alphanumeric line ("A", "1", "OK" — an initial, a page number,
a checkbox mark) goes through the same containment scoring as any other
line, so a unique one survives and only a genuine duplicate is suppressed.

## OCR quality review (manual-review queue)

Every indexed page gets a deterministic quality assessment
(`pipeline/ocr/quality.py`, orchestrated by `pipeline/review/assess.py`)
recorded in `page_quality_reviews`/`document_quality_reviews`. Three
decisions: `auto_accepted`, `manual_review_recommended`,
`extraction_failed`. Each carries stable machine-readable reason codes
plus human-readable messages, the measured metrics, and the exact
thresholds applied — all persisted, so a decision is reproducible from
the DB alone.

**The auto-accept gate is deliberately conservative**: auto-accept means
VERY HIGH-confidence extraction on *every* signal at once, never mean
confidence alone. For an OCR-derived page (`mode='ocr'` or `'merged'`),
all of the following must hold:

- mean OCR confidence **>= 0.98** (`MIN_AUTO_ACCEPT_CONFIDENCE`) — a
  0.95-confidence scan is `manual_review_recommended`
  (`CONFIDENCE_BELOW_AUTO_ACCEPT_BAR`), not auto-accepted, even though
  0.95 is well above the merely-risky `LOW_CONFIDENCE` floor (0.80);
- resolved text >= 40 characters (`MIN_AUTO_ACCEPT_CHARS`, stricter than
  the 20-character `SPARSE_OUTPUT` floor native pages use) and >= 8 word
  tokens (`MIN_AUTO_ACCEPT_WORDS`);
- the OCR engine reported >= 2 lines (`MIN_AUTO_ACCEPT_LINES`);
- resolved text corresponds >= 85% (word-token similarity,
  `MIN_TEXT_LINE_CORRESPONDENCE`) with what the engine actually reported
  per line — a low ratio (`TEXT_LINE_MISMATCH`) means the resolved text
  and the engine's own lines have diverged, e.g. a merge bug;
- garbled-character ratio below 1% (`GARBLED_OUTPUT` otherwise);
- **orientation is verified**, not just clean: at least 3 OCR lines must
  have usable bounding-box geometry, or the page is
  `ORIENTATION_UNVERIFIED` and blocked from auto-accept — missing
  geometry is never treated as "presumably fine". Only once verified can
  a majority of tall/narrow line boxes trip `ORIENTATION_RISK` instead.
  Bounding-box geometry detects a *text-layout* orientation risk in the
  resolved output; it cannot guarantee the original source image's
  orientation, since an engine may auto-rotate internally before ever
  reporting geometry.

A full-page scan/image is still **not** flagged merely for being a scan:
`FULL_PAGE_SCAN` (mode='ocr', no native text at all) and
`OCR_DERIVED_MIXED_PAGE` (mode='merged', native text plus OCR — region or
mixed-page OCR, not necessarily a whole-page scan) are both informational
composition labels, never blocking by themselves. A demonstrably
excellent scan that clears every signal above still auto-accepts. Native
(`mode='native'`) pages keep their own simpler logic (just the 20-char
floor) — none of the OCR-specific signals apply to them. The assessment
functions never receive a filename or file path, so identity documents
(passports, etc.) are never inferred from filename or content semantics —
only technical image/page/geometry signals.

### Local handoff packages (no upload)

The GUI's **Review** screen (`services/review_service.py`,
`gui/web/views/review.js`) lists flagged pages with their file/page path
and reasons. Clicking **"Prepare for ChatGPT"** or **"Prepare for
Claude"** (the only two choices — no free-text provider box) builds a
complete *local* package under VectorDrive's own data directory
(`<data_home>/review_handoffs/<...>/`, never beside the source file):

- `manifest.json` — the page's decision, reasons, metrics, thresholds,
  engine/model, **and its resolved OCR/extracted text**;
- `review_prompt.txt` — the same text plus a structured review prompt,
  referencing the image artifact by filename so a reviewer can compare
  image against OCR text directly;
- `page-NNNN.<ext>` — a rendered page image for a PDF page
  (`render_page_to_image`, PNG) or a verbatim byte copy for a standalone
  `jpg`/`jpeg`/`png` source (always page 1). Any other source type, a
  missing source file, or an invalid page number fails clearly (a named
  error code) with **nothing written** — no partial directory, no
  `review_handoffs` audit row — rather than silently omitting the image.

This is a fully local, explicit, per-page action; nothing is ever
uploaded or sent to a remote model automatically, no direct-send
integration exists in this build, and no API key is required. The
`review_handoffs` table logs that local preparation happened (including
the artifact paths, in `page_artifacts_json`) — never that any upload
did. A future cloud-provider adapter, if ever added, is separately
consented, later work.

See [Architecture](architecture.md) for how OCR fits into the full
extraction → chunking → embedding pipeline.
