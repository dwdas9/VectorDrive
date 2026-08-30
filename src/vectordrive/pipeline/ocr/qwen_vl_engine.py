"""Phase 1 "Create Markdown" primary OCR engine: Qwen3-VL, served locally by
Ollama, for every page the existing PDF/image routing flags `needs_ocr`.

This is the *only* engine `build_extraction_engines()` wires up for Phase 1
Markdown generation (`services/engines.py`) — legacy indexing
(`build_index_engines()`) keeps the RapidOCR -> PaddleOCR-VL -> Tesseract
stack unchanged. Unlike RapidOCR/PaddleOCR-VL, this engine talks to a
network endpoint (loopback-only, see `_validate_loopback_endpoint`), so it
gets its own strict transport validation rather than reusing another
engine's shape.

Privacy/policy:
- The endpoint is restricted to explicit HTTP(S) loopback hosts
  (localhost/127.0.0.1/::1) with no credentials, path, query or fragment,
  checked *before* any request is made.
- The owned HTTP client never trusts proxy environment variables and never
  follows redirects (`httpx.Client(trust_env=False, follow_redirects=False)`)
  — a misconfigured HTTP(S)_PROXY or a redirecting endpoint must never
  cause a page image to leave this machine silently.

Honesty:
- Qwen3-VL is a general vision-language model, not an OCR engine with a
  native per-character/per-line confidence score. `mean_confidence` is
  always `None` here — never fabricated as 1.0 or any other number — and
  no `OcrLine`/bounding-box data is invented; `OcrResult.lines` stays
  empty, matching PaddleOCR-VL's already-established "no confidence" honesty
  (`pipeline/ocr/paddleocr_vl_engine.py`).

Cache provenance: `MODEL_VERSION` embeds both the served model tag and a
prompt/settings version (`_PROMPT_VERSION`). Bumping `_PROMPT_VERSION` when
the prompt or request options change is what busts the OCR cache
(`pipeline/ocr/cache.py` keys on `(page_image_hash, engine_name,
model_version, lang, config_hash)`) — a prompt change without a version
bump would otherwise silently serve stale Markdown from an old prompt.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from vectordrive.config import DEFAULT_OLLAMA_ENDPOINT, DEFAULT_PHASE1_MARKDOWN_VISION_MODEL
from vectordrive.pipeline.ocr.base import OcrConfig, OcrResult

logger = logging.getLogger(__name__)

DEFAULT_MODEL = DEFAULT_PHASE1_MARKDOWN_VISION_MODEL
DEFAULT_TIMEOUT = 600.0

# Loopback-only allowlist. No wildcard subnet matching (e.g. 127.0.0.0/8
# beyond the literal address) — an explicit, small allowlist is easier to
# reason about and audit than IP-range logic here.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Hard upper bound on the raw HTTP response body. Ollama's /api/chat replies
# are small (one JSON object with a Markdown string); anything far larger is
# treated as a malformed/hostile response rather than parsed.
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Bump this whenever _PROMPT/_RETRY_PROMPT, the request `options` below,
# or the output-validation acceptance rules (e.g. validate_markdown_tables)
# change, so any such change always busts the OCR cache rather than
# silently reusing a result produced under the old prompt/settings/validation.
_PROMPT_VERSION = "v5"

# Shared between the primary prompt and the corrective retry prompt
# (`_RETRY_PROMPT` below) so both state the same GFM table syntax explicitly.
# These rules exist because a real-corpus review (2026-08-26, insurance
# scans) found prompt-v3 sometimes emitting pipe-delimited rows with no GFM
# delimiter row at all, and a stricter variant sometimes emitting the
# delimiter row *before* its header — both structurally invalid Markdown
# that `validate_markdown_tables` below now catches before either response
# is accepted.
_TABLE_SYNTAX_RULES = """- Every table must be valid GitHub-Flavored Markdown.
- A delimiter row (for example |---|---|) must appear immediately after that table's header row.
- Never emit a delimiter row before its header row.
- Never emit two or more consecutive pipe-delimited rows unless the second one is a valid GFM delimiter row with the same number of columns as the first.
- Start a new, separate table whenever the visual column layout changes.
- Keep ordinary paragraphs outside tables."""

# Document-neutral: applies equally to a scanned form, a receipt, a
# certificate, or any other page — never names a specific document type.
_PROMPT = f"""Transcribe this scanned document page into structured Markdown.

Rules:
- Transcribe exactly what is visibly printed. Do not correct spelling, arithmetic, totals, dates, names, identifiers, or addresses.
- Never infer an unclear character or value. Write [unclear] for any uncertain portion.
- Preserve headings and reconstruct each visually distinct table separately where its row/column relationship is visually clear.
{_TABLE_SYNTAX_RULES}
- Never place the entire page inside one table.
- If the page contains multiple distinct documents, keep them separate.
- Do not summarize, explain, or add facts.
- Do not use outside knowledge.
- Output only Markdown."""

# Sent only as a bounded, one-shot correction when the first response fails
# `validate_markdown_tables`. Deliberately does not include the first
# response's text — invalid model output is never fed back to the model,
# only a short restatement of the same GFM requirements plus the original
# exact-transcription rules.
_RETRY_PROMPT = f"""Your previous transcription of this exact page used invalid Markdown table syntax. Transcribe this scanned document page again, from scratch, following these rules:

- Transcribe exactly what is visibly printed. Do not correct spelling, arithmetic, totals, dates, names, identifiers, or addresses.
- Never infer an unclear character or value. Write [unclear] for any uncertain portion.
- Preserve headings and reconstruct each visually distinct table separately where its row/column relationship is visually clear.
{_TABLE_SYNTAX_RULES}
- Never place the entire page inside one table.
- If the page contains multiple distinct documents, keep them separate.
- Do not summarize, explain, or add facts.
- Do not use outside knowledge.
- Output only Markdown."""


class QwenVlOcrError(RuntimeError):
    """Raised for a QwenVlOcrEngine failure.

    Never carries image bytes, base64 data, or extracted text — only safe
    metadata (endpoint, status code, model identity, response shape). The
    OCR cache wrapper (`run_ocr_with_cache`) catches this and records a
    normal cached failure; it never propagates raw response content.
    """


def _validate_loopback_endpoint(endpoint: str) -> str:
    """Reject anything but a bare http(s) loopback origin, before any
    request is ever made. Credentials, a path, a query, or a fragment are
    all refused — the endpoint must be exactly a scheme+host[:port] pointing
    at this machine.
    """
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        raise QwenVlOcrError(f"Ollama endpoint must be http or https, got scheme {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise QwenVlOcrError("Ollama endpoint must not include credentials")
    if parts.path not in ("", "/"):
        raise QwenVlOcrError("Ollama endpoint must not include a path")
    if parts.query:
        raise QwenVlOcrError("Ollama endpoint must not include a query string")
    if parts.fragment:
        raise QwenVlOcrError("Ollama endpoint must not include a fragment")
    try:
        parts.port  # Accessing .port validates it; raises ValueError if malformed (e.g. out of range).
    except ValueError as exc:
        raise QwenVlOcrError(f"Ollama endpoint has an invalid port: {exc}") from exc
    host = parts.hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        raise QwenVlOcrError(
            f"Ollama endpoint host must be one of {sorted(_LOOPBACK_HOSTS)} (loopback only), got {host!r}"
        )
    # Ollama's macOS server commonly binds only to IPv4 loopback. Canonicalize
    # localhost so a machine preferring ::1 cannot report a false refusal.
    if host.lower() == "localhost":
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://127.0.0.1{port}"
    return endpoint.rstrip("/")


def _strip_outer_markdown_fence(text: str) -> str:
    """Strip a single outer ```markdown or ```md fence wrapping the *entire*
    response, if present. Any other content (unfenced text, or text with
    internal/nested fences that aren't the sole outer wrapper) is returned
    byte-for-byte unchanged — this must never rewrite model content beyond
    that one specific, optional wrapper.
    """
    body = text.strip()
    for prefix in ("```markdown", "```md"):
        if body.startswith(prefix) and body.endswith("```") and len(body) > len(prefix) + 3:
            inner = body[len(prefix) : -3]
            if inner.startswith("\r\n"):
                inner = inner[2:]
            elif inner.startswith("\n"):
                inner = inner[1:]
            if inner.endswith("\n"):
                inner = inner[:-1]
            return inner
    return text


class MarkdownTableError(ValueError):
    """Raised by `validate_markdown_tables` for structurally invalid
    pipe-table syntax. The message carries only a 1-based line number and a
    short, fixed description of the structural rule that was violated —
    never the offending line's text — so it stays content-safe even when
    logged or included in a `QwenVlOcrError`.
    """


_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")


def _find_unescaped_pipe_positions(line: str) -> list[int]:
    """Index positions of `|` characters in `line` that are real GFM cell
    separators: not escaped (`\\|`) and not inside an inline code span
    (backtick-delimited). Conservative and line-local — a code span that
    itself spans multiple lines is not tracked, since that's rare in a
    single transcribed table row and getting it wrong only ever makes this
    validator *more* likely to treat a line as prose, never less.
    """
    positions: list[int] = []
    in_code = False
    code_run_len = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            run_len = j - i
            if not in_code:
                in_code = True
                code_run_len = run_len
            elif run_len == code_run_len:
                in_code = False
                code_run_len = 0
            i = j
            continue
        if ch == "|" and not in_code:
            positions.append(i)
        i += 1
    return positions


def _split_row_cells(line: str) -> list[str] | None:
    """Split `line` into GFM table cells on unescaped, non-code-span pipes,
    dropping one optional leading and/or trailing empty cell (GFM's
    optional outer pipes). Returns `None` if `line` has no such pipe at
    all — i.e. it is not a pipe-delimited row candidate, including a plain
    prose line that happens to contain only an escaped `\\|`.
    """
    stripped = line.strip()
    if not stripped:
        return None
    positions = _find_unescaped_pipe_positions(stripped)
    if not positions:
        return None
    cells: list[str] = []
    start = 0
    for pos in positions:
        cells.append(stripped[start:pos])
        start = pos + 1
    cells.append(stripped[start:])
    if cells and cells[0].strip() == "" and stripped.startswith("|"):
        cells = cells[1:]
    if cells and cells[-1].strip() == "" and stripped.endswith("|"):
        cells = cells[:-1]
    if not cells:
        return None
    return [c.strip() for c in cells]


def _is_delimiter_cells(cells: list[str]) -> bool:
    """True if every cell matches a GFM delimiter cell (one or more `-`,
    with optional leading/trailing `:` for alignment) — e.g. `---`, `:--`,
    `--:`, `:-:`.
    """
    return bool(cells) and all(_DELIMITER_CELL_RE.match(c) for c in cells)


def _mask_fenced_code_lines(lines: list[str]) -> list[bool]:
    """Return one bool per line: True if that line falls inside (or is a
    delimiter of) a ``` or ~~~ fenced code block, and must be excluded from
    table detection — a pipe-delimited-looking row inside a fenced code
    block is code content, never a table.
    """
    in_fence = False
    fence_marker = ""
    masked: list[bool] = []
    for line in lines:
        stripped = line.strip()
        is_fence_line = False
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_marker = stripped[:3]
                in_fence = True
                is_fence_line = True
        elif stripped.startswith(fence_marker):
            in_fence = False
            fence_marker = ""
            is_fence_line = True
        masked.append(in_fence or is_fence_line)
    return masked


def validate_markdown_tables(text: str) -> None:
    """Conservative, standard-library-only structural check for pipe-table
    syntax outside fenced code blocks. This is a *syntax* gate, not a
    semantic or layout correctness check — it never rewrites content and
    says nothing about whether a valid table's data is actually right.

    Scans for maximal runs of consecutive pipe-delimited-row candidates
    (see `_split_row_cells`) and rejects a run when:
    - its first row is itself delimiter-shaped (catches both a standalone
      delimiter row and a delimiter row emitted *before* its header); or
    - the run has 2+ rows and the second row is not a valid GFM delimiter
      row with the same column count as the first (header) row; or
    - any later data row in the run has a different column count than the
      header/delimiter row (the table's column count must stay fixed until
      the run of consecutive pipe rows ends).

    A single pipe-delimited row that starts a run of length 1 (e.g. an
    isolated `|` in an ordinary sentence) is never rejected on its own —
    GFM itself only requires a delimiter row once a table is actually
    being started, i.e. once a second consecutive pipe row shows up.
    """
    lines = text.splitlines()
    fenced = _mask_fenced_code_lines(lines)
    cells_per_line: list[list[str] | None] = [
        None if is_fenced else _split_row_cells(line) for line, is_fenced in zip(lines, fenced)
    ]

    n = len(lines)
    i = 0
    while i < n:
        if cells_per_line[i] is None:
            i += 1
            continue

        run_start = i
        j = i
        while j < n and cells_per_line[j] is not None:
            j += 1
        run_len = j - run_start
        header_cells = cells_per_line[run_start]
        assert header_cells is not None  # narrowed by the loop condition above

        if _is_delimiter_cells(header_cells):
            raise MarkdownTableError(
                f"line {run_start + 1}: GFM delimiter row is not immediately preceded by a pipe-delimited header row"
            )

        if run_len >= 2:
            delim_cells = cells_per_line[run_start + 1]
            assert delim_cells is not None
            if not _is_delimiter_cells(delim_cells) or len(delim_cells) != len(header_cells):
                raise MarkdownTableError(
                    f"line {run_start + 1}: pipe-delimited row is followed by another pipe-delimited row "
                    "that is not a valid GFM delimiter row matching its column count"
                )

            for k in range(run_start + 2, j):
                data_cells = cells_per_line[k]
                assert data_cells is not None
                if len(data_cells) != len(header_cells):
                    raise MarkdownTableError(
                        f"line {k + 1}: GFM table data row column count does not match "
                        "the header/delimiter row"
                    )

        i = j


class QwenVlOcrEngine:
    """Tier2/primary OCR engine for Phase 1 Markdown generation: Qwen3-VL
    via a local Ollama `/api/chat`. Implements the `OcrEngine` protocol
    (`pipeline/ocr/base.py`).
    """

    NAME = "qwen-vl-ollama"

    def __init__(
        self,
        endpoint: str = DEFAULT_OLLAMA_ENDPOINT,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ):
        self.endpoint = _validate_loopback_endpoint(endpoint)
        self.model = model
        self.timeout = timeout
        # Cache provenance: any change to the model tag OR the prompt/
        # settings version changes this string, which is part of the OCR
        # cache key (page_image_hash, engine_name, model_version, lang,
        # config_hash) in pipeline/ocr/cache.py.
        self.MODEL_VERSION = f"{self.model}::ollama-chat::prompt-{_PROMPT_VERSION}"
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            # Local-only transport: never inherit HTTP(S)_PROXY (a page
            # image must never be silently routed through an
            # environment-configured proxy), never follow redirects (a
            # compromised/misconfigured local endpoint must not be able to
            # bounce the request elsewhere).
            self._client = httpx.Client(trust_env=False, follow_redirects=False)
        return self._client

    def close(self) -> None:
        """Close the HTTP client, but only if this engine created it itself
        — an injected client is the caller's to close.
        """
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _read_bounded_response(self, client: httpx.Client, url: str, payload: dict) -> bytes:
        """POST via httpx streaming, checking the status code as soon as
        headers arrive (before reading any body), then accumulating body
        bytes chunk-by-chunk only up to `_MAX_RESPONSE_BYTES` — aborting
        mid-stream the moment that bound is crossed, rather than reading a
        larger body fully into memory first and checking its length after
        the fact. Content-safe: a non-2xx response's body is never read
        into an error message.
        """
        with client.stream("POST", url, json=payload, timeout=self.timeout) as response:
            if response.status_code != 200:
                raise QwenVlOcrError(f"Ollama returned HTTP {response.status_code} from {self.endpoint}")

            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise QwenVlOcrError(f"Ollama response exceeded the {_MAX_RESPONSE_BYTES}-byte bound")
            return bytes(body)

    def _request_transcription(self, client: httpx.Client, image_bytes: bytes, prompt: str) -> str:
        """Send one `/api/chat` request with `prompt` and return the
        fence-stripped transcription text. Shared by both the first attempt
        and the corrective retry in `run()`, so streaming, response-shape,
        model-identity, and fence-stripping behavior is identical either
        way — only the prompt text (and thus request body) differs.
        """
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                }
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": 16384,
                "num_predict": 4096,
            },
        }
        url = f"{self.endpoint}/api/chat"

        try:
            body = self._read_bounded_response(client, url, payload)
        except httpx.TimeoutException as exc:
            raise QwenVlOcrError(f"Ollama request to {self.endpoint} timed out after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise QwenVlOcrError(f"{type(exc).__name__} contacting Ollama at {self.endpoint}") from exc

        try:
            data = json.loads(body)
        except Exception as exc:
            raise QwenVlOcrError(f"malformed JSON response from Ollama: {type(exc).__name__}") from exc

        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise QwenVlOcrError("unexpected response shape from Ollama (expected a message object)")

        message_content = data["message"].get("content")
        if not isinstance(message_content, str) or not message_content.strip():
            raise QwenVlOcrError("blank or missing message.content in Ollama response")

        served_model = data.get("model")
        if (
            not isinstance(served_model, str)
            or served_model.strip().casefold() != self.model.strip().casefold()
        ):
            raise QwenVlOcrError(
                f"served model identity {served_model!r} does not match requested model {self.model!r}"
            )

        # Only the fence-stripped text itself is kept (byte-for-byte, for
        # unfenced content); `.strip()` is used here solely to decide
        # whether the result is blank, never to alter what's returned.
        text = _strip_outer_markdown_fence(message_content)
        if not text.strip():
            raise QwenVlOcrError("Ollama response contained no transcription text after fence stripping")

        return text

    def run(self, image_bytes: bytes, config: OcrConfig) -> OcrResult:
        started = time.monotonic()
        client = self._get_client()

        text = self._request_transcription(client, image_bytes, _PROMPT)
        try:
            validate_markdown_tables(text)
        except MarkdownTableError as first_error:
            # Bounded, single retry: same image/model/settings, a short
            # corrective prompt restating the GFM requirements. The first
            # (invalid) response text is never sent back to the model —
            # `_RETRY_PROMPT` re-requests a from-scratch transcription.
            logger.warning(
                "event=qwen_vl_table_retry endpoint=%s model=%s reason=%s",
                self.endpoint,
                self.model,
                first_error,
            )
            text = self._request_transcription(client, image_bytes, _RETRY_PROMPT)
            try:
                validate_markdown_tables(text)
            except MarkdownTableError as second_error:
                # Content-safe: the exception message carries only a line
                # number and a fixed rule description, never extracted text
                # or image data (see MarkdownTableError's docstring).
                raise QwenVlOcrError(
                    "Ollama transcription had structurally invalid Markdown table syntax "
                    f"after one corrective retry ({second_error})"
                ) from None

        duration_ms = (time.monotonic() - started) * 1000
        # Structured, content-safe log: endpoint/model/timing/sizes only —
        # never the prompt, image/base64 bytes, or extracted Markdown.
        logger.info(
            "event=qwen_vl_ocr endpoint=%s model=%s duration_ms=%d input_bytes=%d output_chars=%d",
            self.endpoint,
            self.model,
            int(duration_ms),
            len(image_bytes),
            len(text),
        )

        return OcrResult(
            engine_name=self.NAME,
            model_version=self.MODEL_VERSION,
            lang=config.lang,
            success=True,
            text=text,
            mean_confidence=None,
        )
