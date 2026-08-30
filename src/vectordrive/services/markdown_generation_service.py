"""Phase 1: Generate adjacent *.vectordrive.md sidecars for source files.

Walks a folder deterministically, runs extraction/OCR/canonicalization,
persists markdown sidecars, and records Markdown metadata in the database
without chunking, embedding, or indexing.

Reuses existing extraction cache, OCR engines, optional structure analysis,
and sidecar persistence from the main indexer pipeline. Respects write
locking and cancellation. Never logs or exports extracted text.
"""
from __future__ import annotations

import logging
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from vectordrive.config import Config, get_data_home
from vectordrive.pipeline.canonicalize import build_canonical_document
from vectordrive.pipeline.extract.base import (
    DOCUMENT_FAILURE_CORRUPTED,
    DocumentUnreadableError,
)
from vectordrive.pipeline.extract.cache import extract_with_cache
from vectordrive.pipeline.extract.pdf_page_router import Phase1PdfExtractor
from vectordrive.pipeline.extract.registry import get_extractor_for
from vectordrive.pipeline.hashing import sha256_file
from vectordrive.pipeline.indexer import (
    CancelCallback,
    EventCallback,
    _classify,
    _iter_files,
    _now,
    _normalize_path,
    _relative_path_under,
)
from vectordrive.pipeline.ocr.page_ocr import ocr_needs_ocr_pages
from vectordrive.pipeline.ocr.cache import is_transient_ocr_failure
from vectordrive.pipeline.ocr.tiered import DEFAULT_CONFIDENCE_THRESHOLD
from vectordrive.pipeline.review.assess import assess_and_persist_document
from vectordrive.pipeline.structure.analyze import analyze_document_structure
from vectordrive.services.engines import build_extraction_engines
from vectordrive.services.errors import OllamaUnavailable, RootUnavailable
from vectordrive.services.exclusion_service import resolve_excluded_abs_paths
from vectordrive.services.locking import index_lock
from vectordrive.services.markdown_sidecar_service import (
    persist_sidecar,
    preflight_sidecar,
    SidecarPersistenceError,
)
from vectordrive.services.roots_service import backfill_files_under_root, ensure_root
from vectordrive.services.settings_service import setup_file_logging
from vectordrive.services.processing_run_service import (
    ProcessingItemSeed, claim_next_item, complete_item, complete_run,
    create_run, fail_item, fail_run, pause_run, resume_run,
)

logger = logging.getLogger(__name__)


def _ensure_phase1_ollama_ready(engine) -> None:
    """Fail before discovery/writes when Phase 1's required Qwen is absent."""
    if getattr(engine, "NAME", None) != "qwen-vl-ollama":
        return  # injected test/non-Ollama engine
    endpoint = getattr(engine, "endpoint", "")
    model = getattr(engine, "model", "")
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=3.0) as client:
            response = client.get(f"{endpoint}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise OllamaUnavailable(
            f"Ollama is required for OCR but is unavailable at {endpoint} ({type(exc).__name__}).",
            hint="Start Ollama and verify /api/tags is reachable, then retry.",
        ) from exc
    names = {
        name.strip().casefold()
        for item in payload.get("models", [])
        if isinstance(item, dict)
        if isinstance(name := (item.get("name") or item.get("model")), str)
        and name.strip()
    } if isinstance(payload, dict) else set()
    if not isinstance(model, str) or model.strip().casefold() not in names:
        raise OllamaUnavailable(
            f"Ollama is running but required OCR model {model!r} is not installed.",
            hint=f"Run `ollama pull {model}` and retry.",
        )


@dataclass(frozen=False)
class _ResultAccumulator:
    """Mutable container for accumulating outcome counts."""

    created: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    skipped_final: int = 0
    unsupported: int = 0
    deferred: int = 0
    failed: int = 0
    cancelled: bool = False
    walk_errors: int = 0
    run_id: int | None = None
    skipped_checkpoint: int = 0

    def to_result(self, source: Literal["cli", "gui"]) -> MarkdownGenerationResult:
        return MarkdownGenerationResult(
            created=self.created,
            updated=self.updated,
            skipped_unchanged=self.skipped_unchanged,
            skipped_final=self.skipped_final,
            unsupported=self.unsupported,
            deferred=self.deferred,
            failed=self.failed,
            cancelled=self.cancelled,
            walk_errors=self.walk_errors,
            source=source,
            run_id=self.run_id,
            skipped_checkpoint=self.skipped_checkpoint,
        )


@dataclass(frozen=True)
class MarkdownGenerationResult:
    """Outcome of a Markdown generation run."""

    created: int
    updated: int
    skipped_unchanged: int
    skipped_final: int
    unsupported: int
    deferred: int
    failed: int
    cancelled: bool
    walk_errors: int
    source: Literal["cli", "gui"]
    run_id: int | None = None
    skipped_checkpoint: int = 0


def generate_markdown(
    conn: sqlite3.Connection,
    root: Path,
    *,
    config: Config,
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    source: Literal["cli", "gui"] = "cli",
    data_home: Path | None = None,
    run_id: int | None = None,
    ocr_lang: str = "en",
    ocr_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> MarkdownGenerationResult:
    """Phase 1: Generate *.vectordrive.md sidecars for all supported files.

    Walks a folder, extracts/OCRs each file, persists adjacent sidecars,
    and records markdown metadata in the database. Status is always
    'not_indexed', never 'indexed'. Does not chunk, embed, or index.

    Args:
        conn: Writable sqlite3 connection to vectordrive.db
        root: Folder to scan (must exist and be a directory)
        config: VectorDrive configuration
        on_event: Optional callback for progress events
        should_cancel: Optional cancellation-check callback (polled per-file)
        source: Origin indicator ('cli' or 'gui')
        data_home: Override for VECTORDRIVE_HOME (defaults to env/config)
        ocr_lang: OCR language (default 'en')
        ocr_confidence_threshold: OCR confidence threshold (uses indexer default)
        run_id: Reserved for durable Phase 1 resume integration. The current
            executor remains restart-safe through sidecar preflight, but does
            not yet expose a persisted Markdown plan.

    Returns:
        MarkdownGenerationResult with outcome counts

    Raises:
        RootUnavailable: If root does not exist or is not a directory
    """
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise RootUnavailable(
            f"{root} does not exist or is not a directory",
            hint="Check that the folder path is correct and accessible.",
        )

    home = Path(data_home) if data_home is not None else get_data_home()
    setup_file_logging(home)

    acc = _ResultAccumulator()

    with index_lock(home, source=source, root=str(root)):
        root_id = ensure_root(conn, str(root))
        backfill_files_under_root(conn, root_id, str(root))

        # Build extraction engines (OCR, structure, but no embedding/indexing)
        engines = build_extraction_engines(config)
        _ensure_phase1_ollama_ready(engines.tier2)

        if on_event is not None:
            on_event({"phase": "markdown_generation", "root": str(root), "status": "started"})

        def _on_walk_error(path: Path, exc: OSError) -> None:
            logger.warning("event=walk_error path=%s reason=%s", str(path), exc.strerror or str(exc))
            acc.walk_errors += 1

        # Wrap should_cancel to detect and track cancellation
        cancel_seen = False
        def _wrapped_cancel() -> bool:
            nonlocal cancel_seen
            if should_cancel is not None and should_cancel():
                cancel_seen = True
                return True
            return False

        start_time = time.perf_counter()
        # Review panel "Remove from VectorDrive": a file the user excluded
        # must never be re-scanned into a sidecar. resolve_excluded_abs_paths
        # keys on root identity + relative path (with an absolute fallback),
        # so a relinked folder still skips the same files.
        excluded_paths = resolve_excluded_abs_paths(config)
        discovered = [
            path for path in _iter_files(root, should_cancel=_wrapped_cancel, on_error=_on_walk_error)
            if not path.name.endswith(".vectordrive.md")
            and _normalize_path(path) not in excluded_paths
        ]
        if cancel_seen and run_id is None:
            acc.cancelled = True
            acc.run_id = None
            return acc.to_result(source)

        config_fingerprint = hashlib.sha256(json.dumps({
            "model": getattr(engines.tier2, "MODEL_VERSION", "unknown"),
            "lang": ocr_lang,
            "confidence": ocr_confidence_threshold,
        }, sort_keys=True).encode()).hexdigest()
        seeds = []
        for path in discovered:
            try:
                content = sha256_file(path)
            except OSError as exc:
                logger.warning("event=file_unreadable path=%s error=%s", path, exc)
                continue
            seeds.append(ProcessingItemSeed(
                path.resolve().relative_to(root).as_posix(), content, config_fingerprint
            ))
        if not seeds:
            raise RootUnavailable(f"{root} contains no readable files")
        old_items = {}
        old_run = None
        if run_id is not None:
            old_run = conn.execute("SELECT * FROM processing_runs WHERE id = ?", (run_id,)).fetchone()
            if old_run is None or old_run["phase"] != "markdown" or Path(old_run["root_path"]).resolve() != root:
                raise ValueError(f"Markdown run {run_id} does not belong to {root}")
            old_items = {row["item_key"]: row for row in conn.execute(
                "SELECT * FROM processing_run_items WHERE run_id = ?", (run_id,)
            ).fetchall()}
            exact = (
                old_run["status"] in ("queued", "paused")
                and old_run["config_fingerprint"] == config_fingerprint
                and len(old_items) == len(seeds)
                and all(seed.item_key in old_items and old_items[seed.item_key]["content_fingerprint"] == seed.content_fingerprint for seed in seeds)
            )
            if not exact:
                previous_id = run_id
                run_id = None
        if run_id is None:
            run_id = create_run(
                conn, phase="markdown", root_path=str(root),
                content_fingerprint=hashlib.sha256(
                    "\n".join(f"{s.item_key}:{s.content_fingerprint}" for s in seeds).encode()
                ).hexdigest(),
                config_fingerprint=config_fingerprint, items=seeds,
            )
            if old_items and old_run["config_fingerprint"] == config_fingerprint:
                now = _now()
                with conn:
                    for seed in seeds:
                        old = old_items.get(seed.item_key)
                        if old is not None and old["status"] == "completed" and old["content_fingerprint"] == seed.content_fingerprint:
                            conn.execute("UPDATE processing_run_items SET status='completed', completed_at=?, updated_at=? WHERE run_id=? AND item_key=?", (now, now, run_id, seed.item_key))

        resume_run(conn, run_id)
        acc.run_id = run_id
        checkpoint_items = {
            row["item_key"]: row for row in conn.execute(
                "SELECT * FROM processing_run_items WHERE run_id = ?", (run_id,)
            ).fetchall()
        }
        acc.skipped_checkpoint = sum(row["status"] == "completed" for row in checkpoint_items.values())
        processed_items = acc.skipped_checkpoint
        if on_event is not None:
            on_event({
                "kind": "run_started",
                "phase": "markdown",
                "run_id": run_id,
                "total": len(seeds),
                "completed": processed_items,
            })

        def _emit_item_done() -> None:
            nonlocal processed_items
            processed_items += 1
            if on_event is not None:
                on_event({
                    "kind": "file_done",
                    "phase": "markdown",
                    "run_id": run_id,
                    "completed": processed_items,
                    "total": len(seeds),
                })

        while True:
            if should_cancel is not None and should_cancel():
                pause_run(conn, run_id)
                acc.cancelled = True
                break
            claimed = claim_next_item(conn, run_id)
            if claimed is None:
                break
            file_path = root / claimed.item_key
            # Ignore *.vectordrive.md sidecars entirely BEFORE classification
            if file_path.name.endswith(".vectordrive.md"):
                logger.debug("event=file_skip_sidecar path=%s", _normalize_path(file_path))
                complete_item(conn, run_id, claimed.id)
                _emit_item_done()
                continue

            file_type = _classify(file_path)

            # Deferred types (e.g., .eml) are not processed in Phase 1
            if file_type == "eml":
                acc.deferred += 1
                logger.info("event=file_deferred path=%s", _normalize_path(file_path))
                complete_item(conn, run_id, claimed.id)
                _emit_item_done()
                continue

            # Unsupported types
            if file_type is None:
                acc.unsupported += 1
                logger.info("event=file_unsupported path=%s", _normalize_path(file_path))
                complete_item(conn, run_id, claimed.id)
                _emit_item_done()
                continue

            before_failed = acc.failed
            try:
                _process_one_markdown_file(
                    conn, file_path, file_type, root=root, root_id=root_id,
                    engines=engines, acc=acc, on_event=on_event,
                    ocr_lang=ocr_lang, ocr_confidence_threshold=ocr_confidence_threshold,
                )
                if acc.failed > before_failed:
                    fail_item(conn, run_id, claimed.id, "Markdown generation failed")
                else:
                    complete_item(conn, run_id, claimed.id)
                _emit_item_done()
            except Exception as exc:
                if isinstance(exc, OllamaUnavailable):
                    fail_item(conn, run_id, claimed.id, str(exc))
                    fail_run(conn, run_id, str(exc))
                    raise
                message = f"{type(exc).__name__}: {exc}"
                fail_item(conn, run_id, claimed.id, message)
                acc.failed += 1
                logger.exception("event=file_failed path=%s", file_path)
                _emit_item_done()

        if not acc.cancelled:
            if acc.failed:
                fail_run(conn, run_id, f"{acc.failed} Markdown file(s) failed")
            else:
                complete_run(conn, run_id)

        acc.cancelled = acc.cancelled or cancel_seen
        duration = time.perf_counter() - start_time
        if on_event is not None:
            on_event(
                {
                    "phase": "markdown_generation",
                    "status": "completed" if not acc.cancelled else "cancelled",
                    "created": acc.created,
                    "updated": acc.updated,
                    "skipped_unchanged": acc.skipped_unchanged,
                    "skipped_final": acc.skipped_final,
                    "unsupported": acc.unsupported,
                    "deferred": acc.deferred,
                    "failed": acc.failed,
                    "walk_errors": acc.walk_errors,
                    "duration_seconds": duration,
                }
            )

        logger.info(
            "event=phase_complete phase=markdown_generation created=%d updated=%d "
            "skipped_unchanged=%d skipped_final=%d unsupported=%d deferred=%d failed=%d "
            "walk_errors=%d cancelled=%s duration_ms=%d",
            acc.created,
            acc.updated,
            acc.skipped_unchanged,
            acc.skipped_final,
            acc.unsupported,
            acc.deferred,
            acc.failed,
            acc.walk_errors,
            acc.cancelled,
            int(duration * 1000),
        )

    return acc.to_result(source)


def _phase1_extractor_cache_version(extractor, tier2_engine) -> str:
    """Deterministic Phase 1 extraction-cache version: the real extractor's
    own VERSION plus the configured OCR recipe (tier2 engine NAME and
    MODEL_VERSION).

    Passed as `extract_with_cache`'s `extractor_version_override`, this
    namespaces Phase 1's own extracted_docs/pages row separately from
    legacy indexing's (which calls `extract_with_cache` with no override
    and so keeps resolving to `extractor.VERSION` alone, unchanged). Two
    failure modes this prevents: an old RapidOCR-resolved extracted_doc
    (legacy indexing already mutated a page's needs_ocr 1 -> 0 with
    RapidOCR text) silently suppressing Qwen because Phase 1 reused that
    row; and changing the configured Qwen model or prompt version (which
    changes `MODEL_VERSION`) silently reusing page-routing state resolved
    under a previous Qwen configuration. Native pages still bypass OCR
    normally within this isolated row — only the cache row identity
    changes, not extraction/routing logic itself. See docs/ocr.md.
    """
    return f"{extractor.VERSION}+ocr:{tier2_engine.NAME}:{tier2_engine.MODEL_VERSION}"


def _process_one_markdown_file(
    conn: sqlite3.Connection,
    file_path: Path,
    file_type: str,
    *,
    root: Path,
    root_id: int,
    engines,
    acc: _ResultAccumulator,
    on_event: EventCallback | None = None,
    ocr_lang: str = "en",
    ocr_confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> None:
    """Process a single file: extract, OCR, structure, canonicalize, persist sidecar.

    Updates acc in place. On failure, records markdown_status='failed' in DB
    without calling extraction/OCR/structure/persist. On exact-source FINAL,
    skips extraction/OCR/structure/persist and preserves existing indexed status.
    """
    normalized_path = _normalize_path(file_path)
    now = _now()
    stat = file_path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns
    relative_path = _relative_path_under(normalized_path, root, root_id)

    # --- Preflight sidecar check (fail-closed) ---
    preflight = None
    content_hash = None
    try:
        preflight = preflight_sidecar(file_path)
        # Use preflight's source hash to avoid double hashing
        content_hash = preflight.source_sha256
    except SidecarPersistenceError as exc:
        # Fail-closed: do NOT extract/OCR/persist. Preserve existing sidecar.
        # Compute content_hash via sha256_file since preflight failed.
        # Create/update file row with markdown_status='failed' and error.
        acc.failed += 1
        normalized_path_str = str(normalized_path)
        # For SidecarPersistenceError, sanitized/truncated message is acceptable
        error_msg = type(exc).__name__ + ": " + str(exc)[:200]
        # Compute hash since preflight failed
        content_hash = sha256_file(file_path)
        existing = conn.execute(
            "SELECT id FROM files WHERE path = ?", (normalized_path_str,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
                "root_id, relative_path, markdown_status, markdown_error) "
                "VALUES (?, ?, ?, ?, ?, ?, 'not_indexed', ?, ?, 'failed', ?)",
                (normalized_path_str, size, stat.st_mtime, mtime_ns, content_hash, file_type,
                 root_id, relative_path, error_msg),
            )
        else:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, file_type = ?, "
                "markdown_status = 'failed', markdown_error = ?, root_id = ?, relative_path = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, content_hash, file_type, error_msg,
                 root_id, relative_path, existing["id"]),
            )
        conn.commit()
        logger.error("event=file_failed path=%s reason=%s", normalized_path, error_msg)
        if on_event is not None:
            on_event({"phase": "markdown_file", "path": str(file_path), "outcome": "failed"})
        return

    # --- Check for exact-source FINAL sidecar (skip extraction/OCR/persist) ---
    if preflight is not None and preflight.decision == "skip_final":
        acc.skipped_final += 1
        normalized_path_str = str(normalized_path)
        existing = conn.execute(
            "SELECT id, status FROM files WHERE path = ?", (normalized_path_str,)
        ).fetchone()
        # Preserve existing indexed status if present; otherwise not_indexed
        preserved_status = existing["status"] if existing and existing["status"] == "indexed" else "not_indexed"
        if existing is None:
            conn.execute(
                "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
                "root_id, relative_path, markdown_status, markdown_path, "
                "markdown_body_hash, markdown_updated_at, markdown_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'final', ?, ?, ?, NULL)",
                (normalized_path_str, size, stat.st_mtime, mtime_ns, content_hash, file_type,
                 preserved_status, root_id, relative_path,
                 str(preflight.sidecar_path), preflight.body_sha256, now),
            )
        else:
            conn.execute(
                "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, file_type = ?, "
                "markdown_status = 'final', markdown_path = ?, markdown_body_hash = ?, "
                "markdown_updated_at = ?, markdown_error = NULL, "
                "root_id = ?, relative_path = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, content_hash, file_type,
                 str(preflight.sidecar_path), preflight.body_sha256, now,
                 root_id, relative_path, existing["id"]),
            )
        conn.commit()
        logger.info("event=file_skip_final path=%s", normalized_path)
        if on_event is not None:
            on_event({"phase": "markdown_file", "path": str(file_path), "outcome": "skipped_final"})
        return

    # --- Normal extraction/OCR/structure/persist flow ---
    normalized_path_str = str(normalized_path)
    existing = conn.execute("SELECT id FROM files WHERE path = ?", (normalized_path_str,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO files (path, size, mtime, mtime_ns, content_hash, file_type, status, "
            "root_id, relative_path, markdown_status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'not_indexed', ?, ?, NULL)",
            (normalized_path_str, size, stat.st_mtime, mtime_ns, content_hash, file_type,
             root_id, relative_path),
        )
        file_id = conn.execute("SELECT id FROM files WHERE path = ?", (normalized_path_str,)).fetchone()["id"]
    else:
        file_id = existing["id"]
        # Update existing row's metadata before extraction, but preserve status and timestamps
        conn.execute(
            "UPDATE files SET size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, file_type = ?, "
            "root_id = ?, relative_path = ? WHERE id = ?",
            (size, stat.st_mtime, mtime_ns, content_hash, file_type, root_id, relative_path, file_id),
        )
        # Clean up stale extracted_docs if content hash changed
        conn.execute("DELETE FROM extracted_docs WHERE file_id = ? AND content_hash != ?", (file_id, content_hash))
    conn.commit()

    try:
        # --- Extraction ---
        stage_start = time.perf_counter()
        # Phase 1 routes PDF pages with its own structural page-level router
        # (native / native_plus_qwen_page / qwen_page). Legacy direct indexing
        # keeps using the registry's PdfExtractor unchanged. All other file
        # types use the shared registry extractor here too.
        extractor = Phase1PdfExtractor() if file_type == "pdf" else get_extractor_for(file_type)
        phase1_extractor_version = _phase1_extractor_cache_version(extractor, engines.tier2)
        extract_with_cache(
            conn, file_id, content_hash, file_path, extractor,
            extractor_version_override=phase1_extractor_version,
        )
        logger.info(
            "event=stage_complete stage=extract path=%s file_type=%s duration_ms=%d",
            normalized_path, file_type, int((time.perf_counter() - stage_start) * 1000),
        )

        extracted_doc_id = conn.execute(
            "SELECT id FROM extracted_docs WHERE content_hash = ? AND extractor_name = ? AND extractor_version = ?",
            (content_hash, extractor.NAME, phase1_extractor_version),
        ).fetchone()["id"]

        # Defense in depth: a supported document with zero extracted pages must
        # never become a successful, searchable sidecar. Phase1PdfExtractor now
        # raises DocumentUnreadableError directly for corrupt/encrypted PDFs,
        # but this also covers a page set hydrated from an older, broken cached
        # extraction row and any future extractor regression.
        page_row_count = conn.execute(
            "SELECT COUNT(*) AS c FROM pages WHERE extracted_doc_id = ?", (extracted_doc_id,)
        ).fetchone()["c"]
        if page_row_count == 0:
            raise DocumentUnreadableError(
                "extraction produced zero pages; refusing to create an empty sidecar",
                label=DOCUMENT_FAILURE_CORRUPTED,
            )

        # --- OCR ---
        stage_start = time.perf_counter()
        ocr_outcomes = ocr_needs_ocr_pages(
            conn, extracted_doc_id, file_path, file_type,
            tier2_engine=engines.tier2,
            tier3_engine=engines.tier3,
            tier4_engine=engines.tier4,
            lang=ocr_lang,
            confidence_threshold=ocr_confidence_threshold,
            # This stage's OCR engine is Qwen3-VL producing validated
            # Markdown, not plain OCR text -- the legacy line-by-line native
            # merge would drop blank lines and GFM table delimiter rows from
            # it (release-blocking real-smoke finding). Preserve the OCR
            # Markdown untouched instead.
            preserve_ocr_structure=True,
        )
        ocr_succeeded = sum(1 for o in ocr_outcomes if o.success)
        ocr_failed = len(ocr_outcomes) - ocr_succeeded
        transient_ocr_error = next(
            (
                outcome.error_message
                for outcome in ocr_outcomes
                if not outcome.success and is_transient_ocr_failure(outcome.error_message)
            ),
            None,
        )
        if transient_ocr_error is not None:
            raise OllamaUnavailable(
                "Ollama became unavailable during OCR; no blank sidecar was written.",
                hint="Restart Ollama, verify the configured model is available, then retry.",
            )
        logger.info(
            "event=stage_complete stage=ocr path=%s pages=%d success=%d failed=%d duration_ms=%d",
            normalized_path, len(ocr_outcomes), ocr_succeeded, ocr_failed,
            int((time.perf_counter() - stage_start) * 1000),
        )

        # --- Structure analysis (if enabled) ---
        if engines.structure_engine is not None and file_type in ("pdf", "jpg", "jpeg", "png"):
            stage_start = time.perf_counter()
            analyze_document_structure(
                conn, extracted_doc_id, file_path, file_type,
                engine=engines.structure_engine,
                analyze_native_pages=engines.structure_analyze_native,
            )
            logger.info(
                "event=stage_complete stage=structure path=%s duration_ms=%d",
                normalized_path, int((time.perf_counter() - stage_start) * 1000),
            )

        # --- Canonicalization ---
        stage_start = time.perf_counter()
        canonical_document = build_canonical_document(conn, file_id, extracted_doc_id)
        page_count = len(canonical_document.pages)
        logger.info(
            "event=stage_complete stage=canonicalize path=%s pages=%d duration_ms=%d",
            normalized_path, page_count, int((time.perf_counter() - stage_start) * 1000),
        )

        # --- Quality assessment (before sidecar persistence) ---
        assess_and_persist_document(
            conn, file_id=file_id, extracted_doc_id=extracted_doc_id, source_relative_path=relative_path
        )

        # --- Persist sidecar ---
        stage_start = time.perf_counter()
        persist_result = persist_sidecar(file_path, canonical_document)
        logger.info(
            "event=stage_complete stage=persist_sidecar path=%s outcome=%s duration_ms=%d",
            normalized_path, persist_result.outcome, int((time.perf_counter() - stage_start) * 1000),
        )

        # If any OCR page failed, the sidecar we just persisted is a diagnostic
        # artifact (page mode=failed), not indexable content. Keep it on disk and
        # record its path/hash/time for Review UI provenance, but surface the
        # Phase 1 file outcome as visibly failed so Phase 2 never treats it as an
        # empty document. Manual/final skips never reach this branch.
        if ocr_failed:
            acc.failed += 1
            markdown_error = (
                f"OCR failed for {ocr_failed} of {len(ocr_outcomes)} page(s); "
                "diagnostic sidecar retained for review"
            )
            conn.execute(
                "UPDATE files SET status = 'not_indexed', size = ?, mtime = ?, mtime_ns = ?, "
                "content_hash = ?, file_type = ?, markdown_status = 'failed', markdown_path = ?, "
                "markdown_body_hash = ?, markdown_updated_at = ?, markdown_error = ?, "
                "root_id = ?, relative_path = ? WHERE id = ?",
                (size, stat.st_mtime, mtime_ns, content_hash, file_type,
                 str(persist_result.sidecar_path), persist_result.body_sha256, now,
                 markdown_error, root_id, relative_path, file_id),
            )
            conn.commit()
            logger.error(
                "event=file_failed path=%s reason=ocr_failed pages_failed=%d",
                normalized_path, ocr_failed,
            )
            if on_event is not None:
                on_event({
                    "phase": "markdown_file",
                    "path": str(file_path),
                    "outcome": "failed",
                    "pages": page_count,
                })
            return

        # Map lifecycle.content_status to markdown_status
        content_status = persist_result.lifecycle.content_status
        markdown_status_map = {"generated": "ready", "final": "final"}
        markdown_status = markdown_status_map.get(content_status, "ready")

        # Track outcome and update file row
        if persist_result.outcome == "created":
            acc.created += 1
        elif persist_result.outcome == "updated":
            acc.updated += 1
        elif persist_result.outcome == "skipped_unchanged":
            acc.skipped_unchanged += 1
        elif persist_result.outcome == "skipped_final":
            acc.skipped_final += 1

        # For generated/updated sidecars: status='not_indexed'. For unchanged/final: preserve existing.
        if persist_result.outcome in ("created", "updated"):
            new_status = "not_indexed"
        else:
            # skipped_unchanged or skipped_final: preserve existing indexed status if present
            existing_row = conn.execute("SELECT status FROM files WHERE id = ?", (file_id,)).fetchone()
            new_status = existing_row["status"] if existing_row and existing_row["status"] == "indexed" else "not_indexed"

        conn.execute(
            "UPDATE files SET status = ?, size = ?, mtime = ?, mtime_ns = ?, content_hash = ?, "
            "file_type = ?, markdown_status = ?, markdown_path = ?, markdown_body_hash = ?, "
            "markdown_updated_at = ?, markdown_error = NULL, "
            "root_id = ?, relative_path = ? WHERE id = ?",
            (new_status, size, stat.st_mtime, mtime_ns, content_hash, file_type,
             markdown_status, str(persist_result.sidecar_path), persist_result.body_sha256, now,
             root_id, relative_path, file_id),
        )
        conn.commit()

        if on_event is not None:
            on_event({
                "phase": "markdown_file",
                "path": str(file_path),
                "outcome": persist_result.outcome,
                "pages": page_count,
            })

    except DocumentUnreadableError as exc:
        # Corrupt / zero-page / encrypted supported document: fail closed with
        # the extractor's authored, content-safe message, prefixed by its
        # stable failure label ("encrypted: ..." / "corrupted: ..."). No path,
        # no page text, no raw MuPDF string. No sidecar is persisted.
        acc.failed += 1
        error_msg = exc.labelled_message()
        conn.execute(
            "UPDATE files SET markdown_status = 'failed', markdown_error = ? WHERE id = ?",
            (error_msg, file_id),
        )
        conn.commit()
        logger.error("event=file_failed path=%s reason=%s", normalized_path, error_msg)
        if on_event is not None:
            on_event({"phase": "markdown_file", "path": str(file_path), "outcome": "failed"})
    except SidecarPersistenceError as exc:
        # SidecarPersistenceError: sanitized/truncated message is acceptable
        acc.failed += 1
        error_msg = type(exc).__name__ + ": " + str(exc)[:200]
        conn.execute(
            "UPDATE files SET markdown_status = 'failed', markdown_error = ? WHERE id = ?",
            (error_msg, file_id),
        )
        conn.commit()
        logger.error("event=file_failed path=%s reason=%s", normalized_path, error_msg)
        if on_event is not None:
            on_event({"phase": "markdown_file", "path": str(file_path), "outcome": "failed"})
    except Exception as exc:
        # For all other exceptions: only store type, never the body
        acc.failed += 1
        error_msg = f"processing failed: {type(exc).__name__}"
        conn.execute(
            "UPDATE files SET markdown_status = 'failed', markdown_error = ? WHERE id = ?",
            (error_msg, file_id),
        )
        conn.commit()
        logger.error("event=file_failed path=%s reason=%s", normalized_path, error_msg)
        if on_event is not None:
            on_event({"phase": "markdown_file", "path": str(file_path), "outcome": "failed"})
