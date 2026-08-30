"""GuiApi — the js_api object exposed to the web frontend.

Every public method is wrapped by @_envelope: it validates, calls a
vectordrive.services.* function, and shapes the result — it must never
contain SQL, pipeline calls, or a sys.platform branch of its own (see the
approved plan's acceptance criteria). No method ever raises across the
bridge; every outcome, success or failure, comes back as
{"ok": bool, "data": ..., "error": {...} | None}.

GuiApi takes its window by injection specifically so it stays testable
with a fake — see tests/unit/gui/test_api_envelope.py, which imports this
module with zero `webview` dependency.
"""
from __future__ import annotations

import functools
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import httpx

from vectordrive.config import (
    SUPPORTED_PHASE1_VISION_MODELS,
    get_config_path,
    get_data_home,
    get_db_path,
    load_config,
    path_is_inside,
    update_config,
)
from vectordrive.db.connection import connect
from vectordrive.db.readonly import open_readonly_connection, try_load_sqlite_vec, vec_table_exists
from vectordrive.gui.chat_jobs import ChatRunner
from vectordrive.gui.jobs import JobRunner
from vectordrive.gui.state import GuiState, load_state, save_state
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.services.chat_service import (
    ChatHistoryMessage,
    ChatService,
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
)
from vectordrive.services.claude_desktop import (
    apply as _apply_claude_diff,
    desired_entry as _desired_claude_entry,
    detect as _detect_claude,
    list_backups as _list_claude_backups,
    preview_connect as _preview_claude_connect,
    preview_disconnect as _preview_claude_disconnect,
    restore_backup as _restore_claude_backup,
)
from vectordrive.services.errors import DbUnavailable, IndexLocked, VectorDriveError
from vectordrive.services.locking import index_lock
from vectordrive.services.folders_service import add_folder as _add_folder
from vectordrive.services.folders_service import list_file_warnings as _list_file_warnings
from vectordrive.services.folders_service import list_folders as _list_folders
from vectordrive.services.folders_service import relink_folder as _relink_folder
from vectordrive.services.folders_service import remove_folder as _remove_folder
from vectordrive.services.folders_service import validate_folder
from vectordrive.services.folders_service import validate_relink as _validate_relink
from vectordrive.services.markdown_generation_service import generate_markdown as _generate_markdown
from vectordrive.services.engines import build_search_stack
from vectordrive.services.exclusion_service import remove_file_from_vectordrive as _remove_file
from vectordrive.services.health_service import compute_next_action, run_health_checks
from vectordrive.services.index_service import latest_run_errors as _latest_run_errors
from vectordrive.services.index_service import preview_folder as _preview_folder
from vectordrive.services.index_service import (
    retry_failed_sidecar_index_for_root as _retry_failed_sidecar_index,
    run_index as _run_index,
)
from vectordrive.services.index_service import run_sidecar_index_for_root as _run_sidecar_index
from vectordrive.services.mcp_probe import deep_probe as _deep_probe
from vectordrive.services.mcp_probe import quick_probe as _quick_probe
from vectordrive.services import platform_paths
from vectordrive.services.review_service import (
    get_page_review_detail as _get_page_review_detail,
    list_review_queue as _list_review_queue,
    prepare_review_handoff as _prepare_review_handoff,
)
from vectordrive.services.platform_paths import open_path_in_default_app, reveal_path_in_file_manager
from vectordrive.services.processing_run_service import (
    list_resumable_runs as _list_resumable_runs,
    recover_interrupted_runs as _recover_interrupted_runs,
)
from vectordrive.services.reset_service import reset_derived_state as _reset_derived_state
from vectordrive.services.resource_usage import get_ollama_model_memory
from vectordrive.services.ollama_chat import OllamaChatClient
from vectordrive.services.search_service import search_index
from vectordrive.services.settings_service import (
    backup_database as _backup_database,
    check_integrity as _check_integrity,
    get_data_home_info as _get_data_home_info,
    get_log_tail as _get_log_tail,
    list_database_backups as _list_db_backups,
    setup_file_logging as _setup_file_logging,
    vacuum_database as _vacuum_database,
    wal_checkpoint as _wal_checkpoint,
)
from vectordrive.services.status_service import get_status_snapshot

logger = logging.getLogger("vectordrive.gui")

CHAT_DELTA_FLUSH_CHARS = 2_048
CHAT_DELTA_FLUSH_INTERVAL_S = 0.15


def _envelope(fn: Callable[..., Any]) -> Callable[..., dict]:
    @functools.wraps(fn)
    def wrapper(self: "GuiApi", *args, **kwargs) -> dict:
        try:
            data = fn(self, *args, **kwargs)
            return {"ok": True, "data": data, "error": None}
        except VectorDriveError as exc:
            logger.warning("GuiApi.%s: %s", fn.__name__, exc)
            return {
                "ok": False,
                "data": None,
                "error": {"code": exc.code, "message": str(exc), "hint": exc.hint},
            }
        except Exception as exc:  # noqa: BLE001 — the bridge must never raise, ever
            logger.exception("GuiApi.%s: unhandled exception", fn.__name__)
            return {
                "ok": False,
                "data": None,
                "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}", "hint": None},
            }

    return wrapper


class GuiApi:
    """Constructed once per window. `window` is injected (not imported
    from `webview` directly) so this class has no hard dependency on a
    live pywebview window for testing.
    """

    def __init__(
        self,
        *,
        window: Any = None,
        data_home: Path | None = None,
        jobs: JobRunner | None = None,
        chat_jobs: ChatRunner | None = None,
    ) -> None:
        self.window = window
        self.data_home = Path(data_home) if data_home is not None else get_data_home()
        self.jobs = jobs or JobRunner()
        self.chat_jobs = chat_jobs or ChatRunner(event_bus=self.jobs.bus)
        self._available_chat_models: set[str] = set()
        self._available_ocr_models: set[str] = set()
        self._app_started_at = time.time()

    # -- App ---------------------------------------------------------

    @_envelope
    def get_app_info(self) -> dict:
        import vectordrive

        return {
            "version": vectordrive.__version__,
            "data_home": str(self.data_home),
            "uptime_s": round(time.time() - self._app_started_at, 1),
        }

    @_envelope
    def get_resource_usage(self) -> dict:
        """System memory (used/total) and Ollama's loaded-model VRAM
        footprint, for the global job-status card. The frontend polls
        this only while a job is active (gui/web/job-status.js) — both
        signals are independently best-effort (each may come back None)
        so one being unavailable never breaks the other, and both
        underlying calls carry their own short timeouts (platform_paths.
        get_memory_usage, resource_usage.get_ollama_model_memory) so this
        can never block the bridge thread for long.
        """
        config = load_config(get_config_path(self.data_home))
        return {
            "memory": platform_paths.get_memory_usage(),
            "ollama_model_bytes": get_ollama_model_memory(config.ollama_endpoint),
        }

    @_envelope
    def poll_events(self, cursor: int = 0) -> dict:
        events = self.jobs.bus.since(int(cursor))
        return {
            "events": [e.to_dict() for e in events],
            "cursor": events[-1].seq if events else int(cursor),
            "gap": bool(events and events[0].seq > int(cursor) + 1),
        }

    @_envelope
    def get_job(self, job_id: str) -> dict:
        record = self.jobs.get(job_id)
        if record is None:
            raise VectorDriveError(f"No job found with id {job_id!r}.", code="job_not_found")
        return record.to_dict()

    @_envelope
    def cancel_job(self, job_id: str) -> dict:
        cancelled = self.jobs.cancel(job_id)
        return {"cancelled": cancelled}

    @_envelope
    def pause_job(self, job_id: str) -> dict:
        """Cooperatively pause a durable Markdown/index worker.

        The active model call is allowed to finish; the processing service
        observes the signal at its next safe file/page boundary.
        """
        return {"pausing": self.jobs.pause(job_id)}

    @_envelope
    def list_resumable_runs(self) -> list[dict]:
        """Recover restart-orphaned work and expose resumable index runs."""
        # While a GUI indexing/Markdown job is active, its live progress card
        # is authoritative.  A paused predecessor (for example after a
        # restart or an accidental second click on Index) must not appear as
        # a misleading Resume card beside it.
        if self.jobs.active_pausable_job_id() is not None:
            return []
        db = connect(get_db_path(self.data_home))
        try:
            try:
                with index_lock(self.data_home, source="gui"):
                    _recover_interrupted_runs(db.conn)
            except IndexLocked:
                # A real worker is active.  Never rewrite its durable state;
                # only already-paused/queued runs are safe to display.
                pass
            runs = _list_resumable_runs(db.conn)
            # A crash/restart or an accidental second click can leave more
            # than one run for the same folder and phase.  Only the newest
            # run is actionable: an older queued/paused row that any newer
            # run has already superseded — including one that has since
            # completed — must never surface as a Resume card.  Older rows
            # stay in the table untouched for auditability.
            latest_id = {
                (row["root_path"], row["phase"]): row["latest"]
                for row in db.conn.execute(
                    "SELECT root_path, phase, MAX(id) AS latest "
                    "FROM processing_runs GROUP BY root_path, phase"
                )
            }
            return [
                run
                for run in runs
                if latest_id.get((run["root_path"], run["phase"])) == run["run_id"]
            ]
        finally:
            db.conn.close()

    # -- Overview ------------------------------------------------------

    def _build_overview(self) -> dict:
        """Shared by get_overview() (synchronous, initial load) and
        refresh_health() (background job, the "Recheck" button) so the
        two never drift into different logic. Never raises: DB
        missing/corrupt degrades db_available=False rather than
        propagating — the Overview screen must always render something.
        """
        config = load_config(get_config_path(self.data_home))
        db_path = get_db_path(self.data_home)
        checks = run_health_checks(config).checks

        db_available = True
        status_dict = None
        folder_count = len(config.indexed_folders)
        latest_run_exists = False
        failed_file_count = 0

        try:
            conn = open_readonly_connection(db_path)
        except DbUnavailable:
            db_available = False
        else:
            try:
                vector_backend = "sqlite_vec" if (vec_table_exists(conn) and try_load_sqlite_vec(conn)) else "numpy"
                snapshot = get_status_snapshot(
                    conn,
                    embedding_provider=config.embedding_provider,
                    embedding_model=config.embedding_model,
                    embedding_dims=config.embedding_dims,
                    vector_backend=vector_backend,
                    db_path=db_path,
                )
                latest_run_exists = snapshot.latest_run is not None
                failed_file_count = snapshot.file_status_counts.get("failed", 0)
                status_dict = {
                    "latest_run": snapshot.latest_run,
                    "file_status_counts": snapshot.file_status_counts,
                    "embedding_provider": snapshot.embedding_provider,
                    "embedding_model": snapshot.embedding_model,
                    "embedding_dims": snapshot.embedding_dims,
                    "vector_backend": snapshot.vector_backend,
                    "db_bytes": snapshot.db_bytes,
                    "wal_bytes": snapshot.wal_bytes,
                    "db_file_bytes": snapshot.db_file_bytes,
                    "db_free_bytes": snapshot.db_free_bytes,
                    "chunk_count": snapshot.chunk_count,
                    "active_embedding_count": snapshot.active_embedding_count,
                    "cached_embedding_count": snapshot.cached_embedding_count,
                    "ocr_warning_count": snapshot.ocr_warning_count,
                }
            finally:
                conn.close()

        next_action = compute_next_action(
            checks,
            db_available=db_available,
            folder_count=folder_count,
            latest_run_exists=latest_run_exists,
            failed_file_count=failed_file_count,
        )

        return {
            "checks": [
                {"name": c.name, "status": c.status.value, "message": c.message, "required": c.required}
                for c in checks
            ],
            "db_available": db_available,
            "status": status_dict,
            "next_action": (
                {"message": next_action.message, "route": next_action.route, "button_label": next_action.button_label}
                if next_action is not None
                else None
            ),
        }

    @_envelope
    def get_overview(self) -> dict:
        return self._build_overview()

    @_envelope
    def refresh_health(self) -> dict:
        job_id = self.jobs.submit(lambda emit, is_cancelled: self._build_overview())
        return {"job_id": job_id}

    # -- Folders ---------------------------------------------------------

    @_envelope
    def list_folders(self) -> list[dict]:
        config = load_config(get_config_path(self.data_home))
        if not config.indexed_folders:
            # No folders configured yet — the common brand-new-install case,
            # where the database may not even exist. Return the true empty
            # list rather than opening a connection at all, so the Folders
            # screen shows its "add a folder" empty state, not an error.
            return []

        db_path = get_db_path(self.data_home)
        try:
            conn = open_readonly_connection(db_path)
        except DbUnavailable:
            # Folders *are* configured but the DB is missing/corrupt: show
            # the list anyway with stats unavailable, rather than hiding it
            # entirely — the Overview screen's health card is where DB
            # problems get surfaced prominently, not here.
            from vectordrive.services.platform_paths import is_cloud_synced

            return [
                {
                    "path": p, "exists": Path(p).exists(), "indexed": 0, "failed": 0, "unsupported": 0,
                    "last_indexed_at": None, "total_bytes": None, "is_cloud_synced": is_cloud_synced(Path(p)),
                    "warnings": 0, "has_index_history": False,
                }
                for p in config.indexed_folders
            ]
        try:
            rows = _list_folders(conn, config)
        finally:
            conn.close()
        return [
            {
                "path": r.path, "exists": r.exists, "indexed": r.indexed, "failed": r.failed,
                "unsupported": r.unsupported, "last_indexed_at": r.last_indexed_at,
                "total_bytes": r.total_bytes, "is_cloud_synced": r.is_cloud_synced,
                "warnings": r.warnings, "has_index_history": r.has_index_history,
            }
            for r in rows
        ]

    @_envelope
    def get_folder_warnings(self, path: str) -> list[dict]:
        """Files under `path` that are 'indexed' but have a non-NULL
        ocr_warning (G12) — the per-file detail behind list_folders()'s
        aggregate `warnings` count. Returns [] if the DB is unavailable,
        matching list_folders()'s own soft-fail behavior for that case.
        """
        db_path = get_db_path(self.data_home)
        try:
            conn = open_readonly_connection(db_path)
        except DbUnavailable:
            return []
        try:
            return _list_file_warnings(conn, path)
        finally:
            conn.close()

    @_envelope
    def choose_folder(self) -> dict | None:
        """Native folder picker using NSOpenPanel as a window-attached sheet.

        pywebview 6.x on macOS has a deadlock in create_file_dialog() when
        called from a js_api thread: AppHelper.callAfter + runModal() fails
        because NSApplication.run() cannot nest a modal session from a
        callAfter dispatch (returns NSModalResponseAbort or hangs
        indefinitely on the semaphore). The fix is to use the non-blocking
        beginSheetModalForWindow:completionHandler: API, bridged back to
        this synchronous method via a threading.Event.
        """
        if self.window is None:
            raise VectorDriveError("No window available to show a picker.", code="no_window")
        if hasattr(self.window, "_folder_picker_result"):
            r = self.window._folder_picker_result
            if not r:
                return {"path": None}
            return {"path": r[0]}
        import sys
        if sys.platform != "darwin":
            import webview
            dialog_kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", webview.FOLDER_DIALOG)
            result = self.window.create_file_dialog(dialog_kind, allow_multiple=False)
            if not result:
                return {"path": None}
            return {"path": result[0]}

        import threading
        import AppKit, Foundation
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView

        result_holder: list[str | None] = [None]
        done = threading.Event()

        def _show_panel():
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setCanCreateDirectories_(True)
            panel.setAllowsMultipleSelection_(False)

            instances = list(BrowserView.instances.values())
            if not instances:
                done.set()
                return
            ns_window = instances[0].window

            def _completion(response):
                if response == AppKit.NSFileHandlingPanelOKButton:
                    urls = panel.URLs()
                    if urls and len(urls) > 0:
                        result_holder[0] = str(urls[0].path())
                done.set()

            panel.beginSheetModalForWindow_completionHandler_(ns_window, _completion)

        AppHelper.callAfter(_show_panel)
        done.wait(timeout=120)
        return {"path": result_holder[0]}

    @_envelope
    def validate_folder_path(self, path: str) -> dict:
        config = load_config(get_config_path(self.data_home))
        warnings = validate_folder(Path(path), config, self.data_home)
        return {"warnings": warnings}

    @_envelope
    def preview_folder(self, path: str) -> dict:
        job_id = self.jobs.submit(
            lambda emit, is_cancelled: _preview_folder(Path(path), should_cancel=is_cancelled).__dict__
        )
        return {"job_id": job_id}

    @_envelope
    def add_folder(self, path: str) -> dict:
        config = load_config(get_config_path(self.data_home))
        validate_folder(Path(path), config, self.data_home)  # raises InvalidFolder if not OK
        new_config = _add_folder(Path(path), config_path=get_config_path(self.data_home))
        return {"indexed_folders": new_config.indexed_folders}

    @_envelope
    def remove_folder(self, path: str, purge_index: bool = False) -> dict:
        db_path = get_db_path(self.data_home)
        db = connect(db_path)
        try:
            new_config, purged = _remove_folder(
                db.conn, Path(path), purge_index=purge_index, config_path=get_config_path(self.data_home)
            )
            if purge_index and purged > 0:
                # G13 fix: chunks/fts_chunks are already gone (cascade +
                # trigger, inside _remove_folder's own commit above) —
                # sqlite-vec's vec_chunks has no FK tie to chunks, so it
                # needs this explicit refresh or vector/hybrid search
                # keeps surfacing the deleted chunk_ids by rowid until the
                # next real index run. Reuses the exact same non-readonly
                # refresh path search already uses — no new deletion code.
                config = load_config(get_config_path(self.data_home))
                build_search_stack(db.conn, config, db.vector_backend, readonly=False)
        finally:
            db.conn.close()
        return {"indexed_folders": new_config.indexed_folders, "purged_count": purged}

    @_envelope
    def validate_relink(self, old_path: str, new_path: str) -> dict:
        config = load_config(get_config_path(self.data_home))
        db = connect(get_db_path(self.data_home))  # RW: relink needs ensure_root/backfill
        try:
            v = _validate_relink(db.conn, old_path, Path(new_path), config, data_home=self.data_home)
        finally:
            db.conn.close()
        return asdict(v)

    @_envelope
    def relink_folder(self, old_path: str, new_path: str) -> dict:
        db = connect(get_db_path(self.data_home))
        try:
            new_config = _relink_folder(
                db.conn, old_path, Path(new_path),
                config_path=get_config_path(self.data_home), data_home=self.data_home,
            )
        finally:
            db.conn.close()
        return {"indexed_folders": new_config.indexed_folders}

    def _has_resumable_run(self, path: str, *, phase: str | None = None) -> bool:
        """Check for queued/paused durable work without changing its state."""
        db_path = get_db_path(self.data_home)
        if not db_path.exists():
            return False
        db = connect(db_path)
        try:
            clauses = [
                "r.root_path = ?",
                "r.status IN ('queued', 'paused')",
                "NOT EXISTS (SELECT 1 FROM processing_runs newer "
                "WHERE newer.root_path = r.root_path AND newer.phase = r.phase AND newer.id > r.id)",
            ]
            params: list[object] = [str(Path(path).resolve())]
            if phase is not None:
                clauses.append("r.phase = ?")
                params.append(phase)
            row = db.conn.execute(
                "SELECT 1 FROM processing_runs r WHERE " + " AND ".join(clauses) + " LIMIT 1",
                params,
            ).fetchone()
            return row is not None
        finally:
            db.conn.close()

    @_envelope
    def start_index(
        self,
        path: str,
        mode: str = "incremental",
        run_id: int | None = None,
        output_markdown_files: bool | None = None,
    ) -> dict:
        if self.jobs.has_pausable_work():
            raise VectorDriveError(
                "Indexing is already in progress. Pause or wait for it to finish before starting another run.",
                code="job_active",
            )
        if output_markdown_files is not None and not isinstance(output_markdown_files, bool):
            raise VectorDriveError(
                "The Markdown output option must be on or off.", code="invalid_markdown_option"
            )
        if run_id is None and self._has_resumable_run(path):
            raise VectorDriveError(
                "This folder has paused work. Resume it before starting a new index.",
                code="run_paused",
            )
        data_home = self.data_home

        def _job(emit, is_cancelled):
            def _events(workflow_phase: str):
                return lambda event: emit(
                    event.get("kind", "progress"),
                    {**event, "workflow_phase": workflow_phase},
                )

            config = load_config(get_config_path(data_home))
            db = connect(get_db_path(data_home))  # G3: a fresh, worker-thread-owned RW connection
            try:
                root = Path(path)
                has_sidecars = any(root.rglob("*.vectordrive.md"))
                if run_id is not None:
                    # Phase 2: consume only validated Markdown sidecars.
                    summary = _run_sidecar_index(
                        db.conn, root, config=config, vector_backend=db.vector_backend,
                        run_id=run_id,
                        on_event=_events("index"),
                        should_cancel=is_cancelled, source="gui", data_home=data_home,
                    )
                    return summary.__dict__
                if output_markdown_files is True:
                    markdown = _generate_markdown(
                        db.conn, root, config=config,
                        on_event=_events("markdown"),
                        should_cancel=is_cancelled, source="gui", data_home=data_home,
                    )
                    if markdown.cancelled:
                        return {**markdown.__dict__, "cancelled": True, "phase": "markdown"}
                    summary = _run_sidecar_index(
                        db.conn, root, config=config, vector_backend=db.vector_backend,
                        mode=mode,
                        on_event=_events("index"),
                        should_cancel=is_cancelled, source="gui", data_home=data_home,
                    )
                    return {**summary.__dict__, "markdown": markdown.__dict__}
                if output_markdown_files is None and has_sidecars:
                    # Backwards-compatible bridge behavior for older callers:
                    # keep the historical auto-detection until every caller
                    # passes an explicit preference.
                    summary = _run_sidecar_index(
                        db.conn, root, config=config, vector_backend=db.vector_backend,
                        mode=mode,
                        on_event=_events("index"),
                        should_cancel=is_cancelled, source="gui", data_home=data_home,
                    )
                    return summary.__dict__
                else:
                    # Explicit off: direct OCR -> chunk/index, even when old
                    # sidecars happen to be present beside source documents.
                    summary = _run_index(
                        db.conn, root, config=config, vector_backend=db.vector_backend,
                        mode=mode, on_event=_events("index"),
                        should_cancel=is_cancelled, source="gui", data_home=data_home,
                    )
                    return summary.__dict__
            finally:
                db.conn.close()

        return {"job_id": self.jobs.submit(_job, pausable=True)}

    @_envelope
    def start_markdown_generation(self, path: str, run_id: int | None = None) -> dict:
        if self.jobs.has_pausable_work():
            raise VectorDriveError(
                "Processing is already in progress. Pause or wait for it to finish before starting another run.",
                code="job_active",
            )
        if run_id is None and self._has_resumable_run(path, phase="markdown"):
            raise VectorDriveError(
                "This folder has paused Markdown work. Resume it before starting a new run.",
                code="run_paused",
            )
        data_home = self.data_home

        def _job(emit, is_cancelled):
            config = load_config(get_config_path(data_home))
            db = connect(get_db_path(data_home))  # G3: a fresh, worker-thread-owned RW connection
            try:
                result = _generate_markdown(
                    db.conn, Path(path), config=config,
                    on_event=lambda e: emit(e.get("kind", "progress"), e),
                    should_cancel=is_cancelled, source="gui", data_home=data_home,
                    run_id=run_id,
                )
            finally:
                db.conn.close()
            return result.__dict__

        return {"job_id": self.jobs.submit(_job, pausable=True)}

    @_envelope
    def retry_failed(self, path: str) -> dict:
        if self.jobs.has_pausable_work():
            raise VectorDriveError(
                "Processing is already in progress. Pause or wait for it to finish before retrying failed files.",
                code="job_active",
            )
        if self._has_resumable_run(path):
            raise VectorDriveError(
                "This folder has paused work. Resume it before retrying failed files.",
                code="run_paused",
            )
        data_home = self.data_home

        def _job(emit, is_cancelled):
            config = load_config(get_config_path(data_home))
            db = connect(get_db_path(data_home))
            try:
                summary = _retry_failed_sidecar_index(
                    db.conn, Path(path), config=config, vector_backend=db.vector_backend,
                    on_event=lambda e: emit(e.get("kind", "progress"), e),
                    should_cancel=is_cancelled, source="gui", data_home=data_home,
                )
            finally:
                db.conn.close()
            return summary.__dict__

        return {"job_id": self.jobs.submit(_job)}

    @_envelope
    def get_run_errors(self, run_id: int) -> list[dict]:
        conn = open_readonly_connection(get_db_path(self.data_home))
        try:
            return _latest_run_errors(conn, run_id)
        finally:
            conn.close()

    # -- Search ----------------------------------------------------------

    def _resolve_indexed_path(self, conn, path: str) -> str:
        """Structural safety, mirroring mcp/path_safety.py's
        resolve_indexed_file(): only a path already recorded as
        status='indexed' can ever be opened/revealed — never an arbitrary
        filesystem path a user might type or paste.
        """
        normalized = str(Path(path).expanduser().resolve())
        row = conn.execute(
            "SELECT path FROM files WHERE path = ? AND status = 'indexed'", (normalized,)
        ).fetchone()
        if row is None:
            raise VectorDriveError(
                "That file is no longer indexed at the recorded path.",
                code="not_indexed",
                hint="Rescan the folder, or copy the path and check it manually.",
            )
        return row["path"]

    @_envelope
    def search(self, query: str, mode: str = "hybrid", top_k: int = 10) -> dict:
        results = search_index(self.data_home, query, mode=mode, top_k=top_k)

        return {
            "results": [
                {
                    "chunk_id": r.chunk_id, "path": r.path, "page_number": r.page_number,
                    "chunk_index": r.chunk_index, "source_type": r.source_type,
                    "excerpt": r.excerpt, "score": r.score,
                    "section": r.section, "block_type": r.block_type,
                }
                for r in results
            ]
        }

    # -- Local chat ------------------------------------------------------

    def _new_chat_client(self, endpoint: str) -> OllamaChatClient:
        try:
            return OllamaChatClient(endpoint)
        except ValueError as exc:
            raise VectorDriveError(
                str(exc),
                code="ollama_not_local",
                hint="Chat only connects to Ollama on this computer. Use a loopback Ollama endpoint.",
            ) from exc

    @_envelope
    def list_chat_models(self) -> dict:
        config = load_config(get_config_path(self.data_home))
        client = self._new_chat_client(config.ollama_endpoint)
        try:
            models = client.list_models()
        finally:
            client.close()

        self._available_chat_models = {model.name for model in models}
        preferred = load_state(self.data_home).chat_model
        return {
            "models": [asdict(model) for model in models],
            "selected_model": preferred if preferred in self._available_chat_models else None,
        }

    @_envelope
    def set_chat_model(self, model: str) -> dict:
        if not isinstance(model, str) or model not in self._available_chat_models:
            raise VectorDriveError(
                "That chat model is not in the current installed-model list.",
                code="chat_model_unavailable",
                hint="Refresh the Chat model list and choose an installed completion model.",
            )
        state = load_state(self.data_home)
        state.chat_model = model
        save_state(self.data_home, state)
        return {"selected_model": model}

    # -- Curated local OCR model ----------------------------------------

    @_envelope
    def list_ocr_models(self) -> dict:
        """Return the supported Qwen vision models already installed.

        Discovery is loopback-only and bounded by OllamaChatClient. The
        curated allow-list prevents arbitrary completion/embedding models
        from appearing in this OCR control, and this call never downloads.
        """
        config = load_config(get_config_path(self.data_home))
        client = self._new_chat_client(config.ollama_endpoint)
        try:
            installed = client.list_models()
        finally:
            client.close()

        by_casefold = {model.name.casefold(): model for model in installed}
        models = [
            by_casefold[name.casefold()]
            for name in SUPPORTED_PHASE1_VISION_MODELS
            if name.casefold() in by_casefold
        ]
        self._available_ocr_models = {model.name for model in models}
        selected = by_casefold.get(config.ocr.phase1_markdown_vision_model.casefold())
        selected_name = (
            selected.name if selected is not None and selected.name in self._available_ocr_models
            else None
        )
        return {
            "models": [asdict(model) for model in models],
            "selected_model": selected_name,
            "configured_model": config.ocr.phase1_markdown_vision_model,
        }

    @_envelope
    def set_ocr_model(self, model: str) -> dict:
        if not isinstance(model, str) or model not in self._available_ocr_models:
            raise VectorDriveError(
                "That OCR model is not in the curated installed-model list.",
                code="ocr_model_unavailable",
                hint="Refresh the Folders screen and choose an installed Qwen vision model.",
            )

        def _mutate(config) -> None:
            config.ocr.phase1_markdown_vision_model = model

        update_config(_mutate, get_config_path(self.data_home))
        return {"selected_model": model}

    @_envelope
    def start_chat(
        self,
        message: str,
        history: list[dict],
        model: str,
        use_indexed_documents: bool = True,
    ) -> dict:
        if not isinstance(model, str) or model not in self._available_chat_models:
            raise VectorDriveError(
                "Choose an installed local chat model first.",
                code="chat_model_unavailable",
            )
        if not isinstance(history, list) or len(history) > 1_000:
            raise VectorDriveError("Chat history is invalid or too large.", code="invalid_chat")
        if not isinstance(use_indexed_documents, bool):
            raise VectorDriveError("The document-grounding option must be true or false.", code="invalid_chat")
        try:
            current_message = ChatHistoryMessage(role="user", content=message)
            bounded_items = history[-MAX_HISTORY_MESSAGES:]
            normalized_history = []
            for item in bounded_items:
                if not isinstance(item, dict):
                    continue
                role = item["role"]
                content = item["content"]
                if role == "assistant" and isinstance(content, str) and len(content) > MAX_MESSAGE_CHARS:
                    content = content[-MAX_MESSAGE_CHARS:]
                normalized_history.append(ChatHistoryMessage(role=role, content=content))
            bounded_history = tuple(normalized_history)
            if len(bounded_history) != min(len(history), MAX_HISTORY_MESSAGES):
                raise ValueError("Every chat history item must be an object")
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorDriveError(str(exc), code="invalid_chat") from exc

        config = load_config(get_config_path(self.data_home))

        def run(emit, is_cancelled) -> dict:
            if is_cancelled():
                return {"content": ""}
            client = self._new_chat_client(config.ollama_endpoint)
            self.chat_jobs.set_cancel_hook(client.close)
            answer_parts: list[str] = []
            try:
                def retrieve(query: str, top_k: int):
                    if is_cancelled():
                        return []

                    def cancellable_embed_request(url: str, payload: dict, timeout: float):
                        embed_client = httpx.Client(
                            timeout=httpx.Timeout(timeout),
                            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                            trust_env=False,
                            follow_redirects=False,
                        )
                        self.chat_jobs.set_cancel_hook(embed_client.close)
                        try:
                            if is_cancelled():
                                raise httpx.ConnectError("embedding request cancelled")
                            return embed_client.post(url, json=payload)
                        finally:
                            embed_client.close()

                    class ChatQueryEmbeddingProvider(OllamaEmbeddingProvider):
                        def __init__(self, endpoint: str):
                            super().__init__(endpoint=endpoint, request_fn=cancellable_embed_request)

                    try:
                        results = search_index(
                            self.data_home,
                            query,
                            mode="hybrid",
                            top_k=top_k,
                            embed_provider_cls=ChatQueryEmbeddingProvider,
                        )
                    finally:
                        self.chat_jobs.set_cancel_hook(client.close)
                    return [] if is_cancelled() else results

                service = ChatService(client, retrieve)
                prepared = service.prepare_chat(
                    current_message.content,
                    bounded_history,
                    use_documents=use_indexed_documents,
                )
                if is_cancelled():
                    return
                emit("chat_sources", {"sources": [asdict(source) for source in prepared.sources]})
                stream = service.stream_prepared(model, prepared, should_cancel=is_cancelled)
                pending: list[str] = []
                pending_chars = 0
                last_emit_at = 0.0
                try:
                    for delta in stream.deltas:
                        if is_cancelled():
                            break
                        answer_parts.append(delta)
                        pending.append(delta)
                        pending_chars += len(delta)
                        now = time.monotonic()
                        if (
                            last_emit_at == 0.0
                            or pending_chars >= CHAT_DELTA_FLUSH_CHARS
                            or now - last_emit_at >= CHAT_DELTA_FLUSH_INTERVAL_S
                        ):
                            emit("chat_delta", {"text": "".join(pending)})
                            pending.clear()
                            pending_chars = 0
                            last_emit_at = now
                except Exception:  # noqa: BLE001 - transport close is the cancellation mechanism
                    if not is_cancelled():
                        raise
                if pending:
                    emit("chat_delta", {"text": "".join(pending)})
                return {"content": "".join(answer_parts)}
            finally:
                client.close()

        return {"chat_id": self.chat_jobs.start(run)}

    @_envelope
    def cancel_chat(self, chat_id: str) -> dict:
        return {"cancelled": self.chat_jobs.cancel(chat_id)}

    @_envelope
    def get_chat(self, chat_id: str) -> dict:
        record = self.chat_jobs.get(chat_id)
        if record is None:
            raise VectorDriveError(f"No chat found with id {chat_id!r}.", code="chat_not_found")
        return record.to_dict()

    @_envelope
    def open_document(self, path: str, page: int | None = None) -> dict:
        conn = open_readonly_connection(get_db_path(self.data_home))
        try:
            real_path = self._resolve_indexed_path(conn, path)
        finally:
            conn.close()
        open_path_in_default_app(Path(real_path))
        return {"opened": True}

    @_envelope
    def reveal_document(self, path: str) -> dict:
        conn = open_readonly_connection(get_db_path(self.data_home))
        try:
            real_path = self._resolve_indexed_path(conn, path)
        finally:
            conn.close()
        reveal_path_in_file_manager(Path(real_path))
        return {"revealed": True}

    # -- OCR quality review queue -----------------------------------------

    @_envelope
    def list_review_queue(self, decisions: list[str] | None = None) -> list[dict]:
        try:
            conn = open_readonly_connection(get_db_path(self.data_home))
        except DbUnavailable:
            # Fresh install / no DB yet: an empty queue, not an error banner
            # (same soft-fail as get_folder_warnings above).
            return []
        try:
            entries = _list_review_queue(
                conn, decisions=tuple(decisions) if decisions else ("manual_review_recommended", "extraction_failed")
            )
        finally:
            conn.close()
        return [asdict(e) for e in entries]

    @_envelope
    def get_review_detail(self, page_review_id: int) -> dict:
        conn = open_readonly_connection(get_db_path(self.data_home))
        try:
            entry = _get_page_review_detail(conn, int(page_review_id))
        finally:
            conn.close()
        return asdict(entry)

    def _resolve_review_file_path(self, conn, file_id: int) -> str:
        """Structural safety for the Review panel's "Open in Folder": the
        path is resolved from the database by row id, never taken from the
        frontend. Only a file that actually has a review-queue entry, and
        whose recorded root still contains it, can be revealed.
        """
        row = conn.execute(
            "SELECT path, root_id FROM files WHERE id = ?", (int(file_id),)
        ).fetchone()
        if row is None:
            raise VectorDriveError(
                "That file is no longer in VectorDrive's catalogue.", code="file_not_found"
            )
        if conn.execute(
            "SELECT 1 FROM page_quality_reviews WHERE file_id = ? LIMIT 1", (int(file_id),)
        ).fetchone() is None:
            raise VectorDriveError(
                "That file has no review-queue entry.", code="not_in_review_queue"
            )
        real_path = str(Path(row["path"]).expanduser().resolve())
        if row["root_id"] is not None:
            root = conn.execute(
                "SELECT path FROM roots WHERE id = ?", (row["root_id"],)
            ).fetchone()
            if root is not None and not path_is_inside(Path(real_path), Path(root["path"])):
                raise VectorDriveError(
                    "That file's recorded folder no longer contains it.",
                    code="root_mismatch",
                    hint="Rescan or relink the folder, then try again.",
                )
        if not Path(real_path).exists():
            raise VectorDriveError(
                "That file is not on disk at its recorded location.",
                code="source_not_found",
                hint="Copy the path and check it manually, or rescan the folder.",
            )
        return real_path

    @_envelope
    def reveal_review_file(self, file_id: int) -> dict:
        """Reveal a review-queue file in Finder via the native, non-shell
        reveal API. Takes only a database row id — never a path string.
        """
        conn = open_readonly_connection(get_db_path(self.data_home))
        try:
            real_path = self._resolve_review_file_path(conn, int(file_id))
        finally:
            conn.close()
        reveal_path_in_file_manager(Path(real_path))
        return {"revealed": True, "path": real_path}

    @_envelope
    def remove_file_from_vectordrive(self, file_id: int) -> dict:
        """Review panel "Remove from VectorDrive": exclude the file and
        purge everything derived from it. The original source document is
        never touched — only VectorDrive's own catalogue rows and the
        generated *.vectordrive.md sidecar.
        """
        if self.jobs.active_job_id() is not None:
            raise VectorDriveError(
                "Wait for the current operation to finish or pause it before removing a file.",
                code="job_active",
                hint="Removal is blocked while indexing or Markdown generation is active so the file cannot be added back mid-run.",
            )
        db = connect(get_db_path(self.data_home))
        try:
            result = _remove_file(
                db.conn,
                int(file_id),
                config_path=get_config_path(self.data_home),
                data_home=self.data_home,
            )
        finally:
            db.conn.close()
        return result.to_dict()

    @_envelope
    def prepare_review_handoff(self, file_id: int, page_numbers: list[int], provider: str) -> dict:
        # Explicit per-file action (requirement 5): only called when the
        # user clicks "Prepare handoff" in the review queue — never
        # triggered automatically, and never itself a network call.
        db = connect(get_db_path(self.data_home))
        try:
            result = _prepare_review_handoff(
                db.conn,
                file_id=int(file_id),
                page_numbers=[int(p) for p in page_numbers],
                provider=provider,
                data_home=self.data_home,
            )
        finally:
            db.conn.close()
        return result

    # -- Claude Desktop --------------------------------------------------

    def _claude_config_path(self) -> Path | None:
        """Override hook: if a test passed a `_claude_config_path_override`
        at construction, use that; otherwise return None which tells the
        services to use the real platform default. This ensures the GUI
        never silently touches the real config during development.
        """
        return getattr(self, "_claude_config_path_override", None)

    @_envelope
    def claude_detect(self) -> dict:
        state = load_state(self.data_home)
        status = _detect_claude(
            data_home=self.data_home,
            last_verified_at=state.last_mcp_verified_at,
            config_path=self._claude_config_path(),
        )
        return asdict(status)

    @_envelope
    def claude_quick_test(self) -> dict:
        entry = _desired_claude_entry(self.data_home)
        result = _quick_probe(entry["command"])
        return asdict(result)

    @_envelope
    def claude_deep_test(self) -> dict:
        entry = _desired_claude_entry(self.data_home)

        def _job(emit, is_cancelled):
            result = _deep_probe(entry["command"], entry["args"], entry.get("env", {}))
            if result.ok:
                gui_state = load_state(self.data_home)
                gui_state.last_mcp_verified_at = str(time.time_ns())
                save_state(self.data_home, gui_state)
            return asdict(result)

        return {"job_id": self.jobs.submit(_job)}

    @_envelope
    def claude_preview_connect(self) -> dict:
        diff = _preview_claude_connect(self.data_home, config_path=self._claude_config_path())
        return {
            "action": diff.action,
            "before_text": diff.before_text,
            "after_text": diff.after_text,
            "unified_diff": diff.unified_diff,
            "preserved_server_count": diff.preserved_server_count,
            "is_noop": diff.is_noop,
            "config_path": diff.config_path,
            "fingerprint_mtime_ns": diff.fingerprint_mtime_ns,
            "fingerprint_size": diff.fingerprint_size,
        }

    @_envelope
    def claude_preview_disconnect(self) -> dict:
        diff = _preview_claude_disconnect(config_path=self._claude_config_path())
        return {
            "action": diff.action,
            "before_text": diff.before_text,
            "after_text": diff.after_text,
            "unified_diff": diff.unified_diff,
            "preserved_server_count": diff.preserved_server_count,
            "is_noop": diff.is_noop,
            "config_path": diff.config_path,
            "fingerprint_mtime_ns": diff.fingerprint_mtime_ns,
            "fingerprint_size": diff.fingerprint_size,
        }

    @_envelope
    def claude_apply(self, diff_dict: dict) -> dict:
        from vectordrive.services.claude_desktop import ConfigDiff

        diff = ConfigDiff(**diff_dict)
        result = _apply_claude_diff(diff, config_path=self._claude_config_path())
        return {"backup_path": result.backup_path, "config_path": result.config_path}

    @_envelope
    def claude_list_backups(self) -> list[str]:
        return _list_claude_backups(config_path=self._claude_config_path())

    @_envelope
    def claude_restore_backup(self, backup_path: str) -> dict:
        result = _restore_claude_backup(Path(backup_path), config_path=self._claude_config_path())
        return {"backup_path": result.backup_path, "config_path": result.config_path}

    # -- Settings / Maintenance ------------------------------------------

    @_envelope
    def settings_info(self) -> dict:
        return _get_data_home_info(self.data_home)

    @_envelope
    def settings_logs(self, lines: int = 200) -> dict:
        return _get_log_tail(self.data_home, lines=lines)

    @_envelope
    def settings_integrity_check(self) -> dict:
        return _check_integrity(self.data_home)

    @_envelope
    def settings_backup_db(self) -> dict:
        def _job(emit, is_cancelled):
            return _backup_database(self.data_home)
        return {"job_id": self.jobs.submit(_job)}

    @_envelope
    def settings_list_backups(self) -> list[dict]:
        return _list_db_backups(self.data_home)

    @_envelope
    def settings_vacuum(self) -> dict:
        def _job(emit, is_cancelled):
            return _vacuum_database(self.data_home)
        return {"job_id": self.jobs.submit(_job)}

    @_envelope
    def settings_wal_checkpoint(self) -> dict:
        return _wal_checkpoint(self.data_home)

    @_envelope
    def settings_reset_data(self) -> dict:
        """Danger-zone maintenance job: discard VectorDrive-owned derived
        state (database/indexes, OCR and embedding caches, processing
        history, logs, backups, review handoffs) and recreate an empty
        database. Original source documents, adjacent *.vectordrive.md
        sidecars, config.toml, and registered folders are never touched —
        the boundary and atomic/rollback-safe move live entirely in
        services/reset_service.reset_derived_state; this method adds no
        deletion logic and no new allow-list targets.

        Serialized: refused while ANY job — indexing, Markdown generation,
        backup, vacuum, or another maintenance job — is pending or running
        (has_active_work() also covers the pre-start window between
        submit() and the worker picking the job up). The single-worker
        JobRunner and reset_service's own index lock are the second and
        third layers.
        """
        if self.jobs.has_active_work():
            raise VectorDriveError(
                "Wait for the current operation to finish before resetting VectorDrive data.",
                code="job_active",
                hint=(
                    "Reset cannot run while indexing, Markdown generation, backup, "
                    "vacuum, or another maintenance job is active."
                ),
            )

        def _job(emit, is_cancelled):
            result = _reset_derived_state(self.data_home)
            # The RotatingFileHandler installed at app start still points at
            # the old logs/vectordrive.log inode, which reset just
            # quarantined and deleted. Re-point it at the freshly recreated
            # logs directory so later log lines are not written into a
            # vanished inode.
            _setup_file_logging(self.data_home)
            return {
                "database_existed": result.database_existed,
                "removed": list(result.removed),
            }

        return {"job_id": self.jobs.submit(_job)}

    # -- Window/state --------------------------------------------------

    @_envelope
    def get_ui_state(self) -> dict:
        return load_state(self.data_home).to_dict()

    @_envelope
    def save_ui_state(self, patch: dict) -> dict:
        current = load_state(self.data_home)
        merged = GuiState.from_dict({**current.to_dict(), **patch})
        save_state(self.data_home, merged)
        return merged.to_dict()
