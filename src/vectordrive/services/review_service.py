"""OCR quality review-queue access and provider-neutral handoff
preparation (requirement 5), following the same service-layer pattern as
index_service.py/settings_service.py: pure functions over an injected
connection/data_home, no GUI/CLI-specific concerns.

Handoff preparation never uploads anything and never calls a remote
model or network endpoint of any kind — it only reads already-persisted
review rows and writes a complete local package (manifest, review
prompt, and a rendered/copied page-image artifact per selected page)
under `data_home/review_handoffs/`, which is VectorDrive-controlled
storage, never beside the source document (requirement 6). Every call is
an explicit, per-file action the caller (GUI) took on purpose
(requirement 5) and is logged both to the application log (content-safe:
counts/paths/decisions only, never the resolved text the local files
themselves carry) and the `review_handoffs` table.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vectordrive.pipeline.ocr.page_render import render_page_to_image
from vectordrive.services.errors import VectorDriveError

logger = logging.getLogger("vectordrive.review")

# Upper bound on how much resolved page text the Review inspector shows.
# A flagged page's text is normally short; this only guards against a
# pathological OCR result flooding the bridge/DOM.
MAX_REVIEW_TEXT_CHARS = 20_000

_ALLOWED_DECISIONS = frozenset({"auto_accepted", "manual_review_recommended", "extraction_failed"})
_DEFAULT_QUEUE_DECISIONS = ("manual_review_recommended", "extraction_failed")
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Requirement 6/reviewer correction: the GUI offers exactly these two
# explicit local-preparation choices (no free-text provider box) -- the
# backend enforces the same set so a stray/typo'd value can never end up
# silently recorded as if it meant something. Neither value ever triggers
# a network call in this slice; both are local-package labels only.
_ALLOWED_PROVIDERS = frozenset({"chatgpt", "claude"})

# PDF pages are rendered fresh (render_page_to_image); these file types are
# instead a single standalone image whose bytes are copied verbatim -- they
# have exactly one page, always page_number=1. Anything else has no
# supported page-image artifact path in this slice and fails clearly
# (reviewer requirement) rather than silently skipping the image.
_COPYABLE_IMAGE_TYPES = {"jpg": "jpg", "jpeg": "jpg", "png": "png"}


@dataclass(frozen=True)
class ReviewQueueEntry:
    page_review_id: int
    file_id: int
    page_number: int
    decision: str
    reason_codes: list[str]
    reasons: list[dict]
    metrics: dict
    thresholds: dict
    engine_name: str | None
    model_version: str | None
    source_relative_path: str | None
    assessed_at: str
    file_path: str


@dataclass(frozen=True)
class PageReviewDetail(ReviewQueueEntry):
    """A single review-queue row plus the page's resolved extracted/OCR
    text and page-level provenance, for the Review panel's inspector.
    Superset of ReviewQueueEntry — every existing field is preserved.
    """

    resolved_text: str = ""
    text_source: str | None = None
    page_ocr_error: str | None = None
    text_truncated: bool = False


def _row_to_entry(row: sqlite3.Row) -> ReviewQueueEntry:
    return ReviewQueueEntry(
        page_review_id=row["id"],
        file_id=row["file_id"],
        page_number=row["page_number"],
        decision=row["decision"],
        reason_codes=json.loads(row["reason_codes_json"]),
        reasons=json.loads(row["reasons_json"]),
        metrics=json.loads(row["metrics_json"]),
        thresholds=json.loads(row["thresholds_json"]),
        engine_name=row["engine_name"],
        model_version=row["model_version"],
        source_relative_path=row["source_relative_path"],
        assessed_at=row["assessed_at"],
        file_path=row["file_path"],
    )


def list_review_queue(
    conn: sqlite3.Connection,
    *,
    decisions: tuple[str, ...] = _DEFAULT_QUEUE_DECISIONS,
    limit: int = 500,
) -> list[ReviewQueueEntry]:
    """Pages flagged for manual review or that failed extraction, most
    recently assessed first. `decisions` defaults to excluding
    auto_accepted (the review queue's whole purpose) but any subset of
    the three decisions may be requested explicitly.
    """
    for decision in decisions:
        if decision not in _ALLOWED_DECISIONS:
            raise VectorDriveError(f"Unknown review decision filter: {decision!r}", code="invalid_decision")
    if not decisions:
        return []
    placeholders = ",".join("?" for _ in decisions)
    rows = conn.execute(
        f"SELECT pqr.id AS id, pqr.file_id AS file_id, pqr.page_number AS page_number, "
        f"pqr.decision AS decision, pqr.reason_codes_json AS reason_codes_json, "
        f"pqr.reasons_json AS reasons_json, pqr.metrics_json AS metrics_json, "
        f"pqr.thresholds_json AS thresholds_json, pqr.engine_name AS engine_name, "
        f"pqr.model_version AS model_version, pqr.source_relative_path AS source_relative_path, "
        f"pqr.assessed_at AS assessed_at, files.path AS file_path "
        f"FROM page_quality_reviews pqr JOIN files ON files.id = pqr.file_id "
        f"WHERE pqr.decision IN ({placeholders}) "
        f"ORDER BY pqr.assessed_at DESC, pqr.file_id, pqr.page_number LIMIT ?",
        (*decisions, int(limit)),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def get_page_review_detail(conn: sqlite3.Connection, page_review_id: int) -> PageReviewDetail:
    """A flagged page's full review record plus the resolved page text and
    page-level provenance, for the Review panel inspector. Text is bounded
    to MAX_REVIEW_TEXT_CHARS (``text_truncated`` flags when it was cut).
    """
    row = conn.execute(
        "SELECT pqr.id AS id, pqr.file_id AS file_id, pqr.page_number AS page_number, "
        "pqr.decision AS decision, pqr.reason_codes_json AS reason_codes_json, "
        "pqr.reasons_json AS reasons_json, pqr.metrics_json AS metrics_json, "
        "pqr.thresholds_json AS thresholds_json, pqr.engine_name AS engine_name, "
        "pqr.model_version AS model_version, pqr.source_relative_path AS source_relative_path, "
        "pqr.assessed_at AS assessed_at, files.path AS file_path, "
        "p.text AS page_text, p.text_source AS page_text_source, p.ocr_error AS page_ocr_error "
        "FROM page_quality_reviews pqr JOIN files ON files.id = pqr.file_id "
        "LEFT JOIN pages p ON p.id = pqr.page_id WHERE pqr.id = ?",
        (page_review_id,),
    ).fetchone()
    if row is None:
        raise VectorDriveError(f"No review entry with id {page_review_id!r}.", code="review_not_found")

    entry = _row_to_entry(row)
    full_text = row["page_text"] or ""
    truncated = len(full_text) > MAX_REVIEW_TEXT_CHARS
    return PageReviewDetail(
        **{f: getattr(entry, f) for f in ReviewQueueEntry.__dataclass_fields__},
        resolved_text=full_text[:MAX_REVIEW_TEXT_CHARS],
        text_source=row["page_text_source"],
        page_ocr_error=row["page_ocr_error"],
        text_truncated=truncated,
    )


def _safe_component(text: str) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("_", text).strip("_")
    return cleaned or "file"


def _render_page_artifact(source_path: Path, file_type: str, page_number: int) -> tuple[bytes, str]:
    """Return (image_bytes, file_extension) for one page's local handoff
    artifact. Never writes anything -- pure render/read. Raises
    VectorDriveError with a clear, stable code for every failure mode
    (missing source, unsupported type, invalid page number, a render/read
    failure) rather than ever silently omitting the image.
    """
    if not source_path.exists():
        raise VectorDriveError(
            f"Source file no longer exists at the recorded path for page {page_number}.",
            code="source_not_found",
        )

    file_type_lower = (file_type or "").lower()

    if file_type_lower == "pdf":
        if page_number < 1:
            raise VectorDriveError(
                f"page_number must be a positive 1-based integer, got {page_number}.",
                code="invalid_page_number",
            )
        try:
            image_bytes = render_page_to_image(source_path, page_number)
        except Exception as exc:  # noqa: BLE001 -- surface any render failure clearly
            raise VectorDriveError(
                f"Could not render page {page_number}: {exc}", code="page_render_failed",
            ) from exc
        return image_bytes, "png"

    if file_type_lower in _COPYABLE_IMAGE_TYPES:
        if page_number != 1:
            raise VectorDriveError(
                f"A standalone image file only has page 1, got page_number={page_number}.",
                code="invalid_page_number",
            )
        try:
            image_bytes = source_path.read_bytes()
        except OSError as exc:
            raise VectorDriveError(
                f"Could not read the source image for page {page_number}: {exc}",
                code="page_render_failed",
            ) from exc
        return image_bytes, _COPYABLE_IMAGE_TYPES[file_type_lower]

    raise VectorDriveError(
        f"Page-image handoff artifacts are not supported for file type {file_type!r}.",
        code="unsupported_page_artifact",
    )


def prepare_review_handoff(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    page_numbers: list[int],
    provider: str,
    data_home: Path,
) -> dict:
    """Build a complete, local, provider-neutral handoff package for the
    given file's flagged pages: a manifest (JSON, including each page's
    resolved OCR/extracted text), a review prompt (plain text, also
    including that text so a reviewer can compare it against the image),
    and one rendered/copied page-image artifact per page. Everything is
    written under `data_home/review_handoffs/` -- never beside the
    source, never uploaded anywhere. `provider` must be one of
    `_ALLOWED_PROVIDERS` (the GUI's explicit "Prepare for ChatGPT"/
    "Prepare for Claude" buttons — no free-text provider box); it is
    never used to make a network call in this slice, only recorded as a
    label on the local package.

    Every page is fully validated and its artifact rendered/read *before*
    anything is written to disk or the database — a failure on any one
    page (missing source, unsupported type, invalid page number, a
    render/read failure) aborts the whole call with nothing left behind:
    no partial handoff directory, no `review_handoffs` audit row.
    """
    if not isinstance(provider, str) or provider not in _ALLOWED_PROVIDERS:
        raise VectorDriveError(
            f"Unsupported provider {provider!r}; must be one of {sorted(_ALLOWED_PROVIDERS)}.",
            code="invalid_provider",
        )
    if not page_numbers:
        raise VectorDriveError("At least one page number is required.", code="invalid_pages")

    file_row = conn.execute(
        "SELECT id, path, relative_path, file_type FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if file_row is None:
        raise VectorDriveError(f"No file with id {file_id!r}.", code="file_not_found")
    source_path = Path(file_row["path"])
    file_type = file_row["file_type"]
    relative_path = file_row["relative_path"] or source_path.name

    # Phase 1: gather + render everything in memory. Nothing is written
    # to disk or the DB until every page has succeeded.
    prepared_pages = []
    for page_number in page_numbers:
        row = conn.execute(
            "SELECT page_id, decision, reasons_json, metrics_json, thresholds_json, "
            "engine_name, model_version FROM page_quality_reviews "
            "WHERE file_id = ? AND page_number = ?",
            (file_id, page_number),
        ).fetchone()
        if row is None:
            raise VectorDriveError(
                f"No review entry for file {file_id} page {page_number}.", code="review_not_found"
            )
        page_row = conn.execute("SELECT text FROM pages WHERE id = ?", (row["page_id"],)).fetchone()
        resolved_text = (page_row["text"] if page_row is not None else "") or ""

        image_bytes, extension = _render_page_artifact(source_path, file_type, page_number)

        prepared_pages.append(
            {
                "page_number": page_number,
                "decision": row["decision"],
                "reasons": json.loads(row["reasons_json"]),
                "metrics": json.loads(row["metrics_json"]),
                "thresholds": json.loads(row["thresholds_json"]),
                "engine_name": row["engine_name"],
                "model_version": row["model_version"],
                "resolved_text": resolved_text,
                "image_bytes": image_bytes,
                "image_filename": f"page-{page_number:04d}.{extension}",
            }
        )

    # Phase 2: everything is validated/rendered -- now actually write the
    # package. Staged in a throwaway, collision-resistant temp directory
    # first (uuid4 suffix: two preparations for the same file in the same
    # second never collide), then published by a single rename only once
    # every file write has succeeded. Nothing under the *published* name
    # is ever visible half-written.
    now = datetime.now(timezone.utc)
    handoffs_root = Path(data_home) / "review_handoffs"
    unique_suffix = uuid.uuid4().hex[:12]
    dir_name = (
        f"{_safe_component(Path(relative_path).stem)}-{file_id}-"
        f"{now.strftime('%Y%m%dT%H%M%SZ')}-{unique_suffix}"
    )
    published_dir = handoffs_root / dir_name
    staging_dir = handoffs_root / f".staging-{dir_name}"

    staging_dir_owned_by_us = False
    published_dir_owned_by_us = False
    db_row_committed = False
    try:
        # exist_ok=False: the uuid4 suffix makes a genuine collision
        # astronomically unlikely, but if it somehow does happen this must
        # fail loudly rather than write into (and later delete) a
        # directory this call didn't create.
        staging_dir.mkdir(parents=True, exist_ok=False)
        staging_dir_owned_by_us = True

        artifact_paths = []
        page_entries = []
        for page in prepared_pages:
            (staging_dir / page["image_filename"]).write_bytes(page["image_bytes"])
            # Recorded/returned path points at the *published* location,
            # even though the bytes are still sitting in staging right now.
            artifact_paths.append(str(published_dir / page["image_filename"]))
            page_entries.append(
                {
                    "page_number": page["page_number"],
                    "decision": page["decision"],
                    "reasons": page["reasons"],
                    "metrics": page["metrics"],
                    "thresholds": page["thresholds"],
                    "engine_name": page["engine_name"],
                    "model_version": page["model_version"],
                    # Explicit local handoff artifact -- unlike
                    # page_quality_reviews or the application log, this
                    # file is allowed to carry the actual extracted text
                    # (requirement 5).
                    "resolved_text": page["resolved_text"],
                    "image_filename": page["image_filename"],
                }
            )

        manifest = {
            "schema_version": 2,
            "prepared_at": now.isoformat(),
            "provider": provider,
            "source": {"relative_path": relative_path},
            "pages": page_entries,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        (staging_dir / "review_prompt.txt").write_text(
            _render_review_prompt(relative_path, page_entries), encoding="utf-8"
        )

        if published_dir.exists():
            # Should be unreachable given the uuid4 suffix; refuse rather
            # than ever overwrite/remove a directory this call didn't
            # create.
            raise VectorDriveError(
                "A handoff directory with this name already exists.", code="handoff_write_failed"
            )
        staging_dir.rename(published_dir)
        staging_dir_owned_by_us = False  # renamed away; nothing left at the old path
        published_dir_owned_by_us = True

        manifest_path = published_dir / "manifest.json"
        prompt_path = published_dir / "review_prompt.txt"

        conn.execute(
            "INSERT INTO review_handoffs (file_id, page_numbers_json, provider, manifest_path, "
            "prompt_path, page_artifacts_json, source_relative_path, prepared_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                json.dumps(page_numbers),
                provider,
                str(manifest_path),
                str(prompt_path),
                json.dumps(artifact_paths),
                relative_path,
                now.isoformat(),
            ),
        )
        conn.commit()
        db_row_committed = True
    except BaseException as exc:
        # Roll back whichever part of this call actually mutated
        # something: an uncommitted DB write, and/or a staging/published
        # directory this call itself created. Never touch anything this
        # call did not create.
        if not db_row_committed:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 -- best-effort, the original failure is what matters
                pass
        if published_dir_owned_by_us and published_dir.exists():
            shutil.rmtree(published_dir, ignore_errors=True)
        if staging_dir_owned_by_us and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if isinstance(exc, VectorDriveError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise VectorDriveError(
            f"Could not prepare the local handoff package ({type(exc).__name__}).",
            code="handoff_write_failed",
        ) from exc

    # Content-safe: counts/paths/decisions only, never the resolved text
    # that manifest.json and review_prompt.txt (deliberately) carry.
    logger.info(
        "event=review_handoff_prepared file_id=%s pages=%s provider=%s manifest_path=%s "
        "artifact_count=%s",
        file_id, page_numbers, provider, manifest_path, len(artifact_paths),
    )

    return {
        "manifest_path": str(manifest_path),
        "prompt_path": str(prompt_path),
        "page_artifact_paths": artifact_paths,
        "provider": provider,
        "page_numbers": page_numbers,
    }


def _render_review_prompt(relative_path: str, page_entries: list[dict]) -> str:
    lines = [
        "You are reviewing OCR/extraction output that VectorDrive flagged for manual review.",
        "This is a fully local handoff package: for each page below, a rendered/copied page",
        "image was saved next to this prompt (see the image filename per page) AND the exact",
        "OCR/extracted text VectorDrive resolved for that page is included below. Compare the",
        "image against that text directly. Nothing here has been uploaded or sent anywhere --",
        "this file, the manifest, and the page images are local files for you to open, paste,",
        "or attach yourself.",
        "",
        f"Source document (relative path only, no absolute path): {relative_path}",
        "",
        "For each page below, answer:",
        "  1. semantic_verdict: pass | partial | fail",
        "  2. layout_verdict: pass | partial | fail | not_reviewed",
        "  3. factual_errors: list anything you can tell is wrong",
        "  4. omissions: list anything that looks missing",
        "",
    ]
    for entry in page_entries:
        lines.append(f"--- Page {entry['page_number']} (image: {entry['image_filename']}) ---")
        lines.append(f"VectorDrive decision: {entry['decision']}")
        reasons_text = "; ".join(f"{r['code']}: {r['message']}" for r in entry["reasons"]) or "(none)"
        lines.append(f"Reasons: {reasons_text}")
        lines.append(f"Engine/model: {entry['engine_name']} / {entry['model_version']}")
        lines.append("OCR/extracted text VectorDrive resolved for this page:")
        lines.append(entry["resolved_text"] or "(no text resolved)")
        lines.append("")
    return "\n".join(lines)
