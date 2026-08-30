"""Indexing-adjacent operations shared by the CLI and (from G5/G6 onward)
the GUI: the folder-preview scan, the locked/registered top-level "run an
index" entry point, retrying only failed files, resetting files for a
forced rebuild, and reading back a run's per-file errors.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vectordrive.config import Config, get_config_path, path_is_inside, update_config
from vectordrive.pipeline.embed.base import EmbeddingProvider
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.pipeline.indexer import (
    CancelCallback,
    EventCallback,
    IndexRunSummary,
    _classify,
    _iter_files,
    index_folder,
)
from vectordrive.services.engines import build_index_engines
from vectordrive.services.engines import build_sidecar_index_engines
from vectordrive.services.errors import RootUnavailable
from vectordrive.services.exclusion_service import resolve_excluded_abs_paths
from vectordrive.services.locking import index_lock
from vectordrive.services.roots_service import backfill_files_under_root, ensure_root
from vectordrive.services.settings_service import setup_file_logging
from vectordrive.services.sidecar_index_service import (
    SidecarIndexRunSummary,
    run_sidecar_index,
)


def reset_files_under_root(conn: sqlite3.Connection, root: Path) -> int:
    """--rebuild: delete every `files` row under `root` so index_folder
    treats each as brand-new (bypassing the content_hash skip-unchanged
    check). Deletion cascades (schema.sql FKs) to extracted_docs/pages/
    chunks/chunk_runs/fts_chunks for that file; the embeddings cache is
    content-addressed by chunk_hash and independent of file identity, so
    identical chunk text is still served from cache, not re-embedded —
    "rebuild" forces reprocessing, not a full cache bust.

    Moved verbatim from cli/index_cmd.py (v0.2 G1) so the GUI's Folders
    screen can reuse the exact same "rebuild" semantics without a second
    implementation. Returns the number of rows deleted.
    """
    rows = conn.execute("SELECT id, path FROM files").fetchall()
    stale_ids = [row["id"] for row in rows if path_is_inside(Path(row["path"]), root)]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", stale_ids)
        conn.commit()
    return len(stale_ids)


def _reset_failed_files_under_root(conn: sqlite3.Connection, root: Path) -> int:
    """--retry-failed: like reset_files_under_root, but scoped to only
    status='failed' rows — files already successfully indexed under the
    same root are left completely alone.
    """
    rows = conn.execute("SELECT id, path FROM files WHERE status = 'failed'").fetchall()
    stale_ids = [row["id"] for row in rows if path_is_inside(Path(row["path"]), root)]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", stale_ids)
        conn.commit()
    return len(stale_ids)


def _register_folder(root: Path, *, config_path: Path | None) -> None:
    """Decision 2 (v0.2 approval): idempotently add `root` to
    config.indexed_folders under the atomic read-modify-write lock, so
    the CLI and GUI folder catalogues can never drift apart. A folder
    already present is left exactly where it was (no reordering), so
    this is safe to call on every successful run.
    """
    root_str = str(root)

    def _mutate(cfg: Config) -> None:
        if root_str not in cfg.indexed_folders:
            cfg.indexed_folders.append(root_str)

    update_config(_mutate, config_path)


def run_index(
    conn: sqlite3.Connection,
    root: Path,
    *,
    config: Config,
    vector_backend: str,
    mode: Literal["incremental", "rebuild", "verify"] = "incremental",
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    register_folder: bool = True,
    embed_provider_cls: type[EmbeddingProvider] = OllamaEmbeddingProvider,
    source: Literal["cli", "gui"] = "cli",
    data_home: Path | None = None,
) -> IndexRunSummary:
    """The one place a full indexing run happens, for both the CLI and
    the GUI: takes the index lock (raises IndexLocked if another process
    already holds it — see services.locking), applies `mode`, builds
    engines from config, runs index_folder(), and (by default) registers
    `root` into config.indexed_folders on success.

    `data_home` is normally left as None (resolved from VECTORDRIVE_HOME);
    tests pass it explicitly to stay isolated from the real data home.
    """
    from vectordrive.config import get_data_home

    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise RootUnavailable(
            f"{root} does not exist or is not a folder. Its index has been preserved.",
            hint="If the folder moved or was renamed, use Relink Folder…; "
            "if it is gone for good, remove it explicitly.",
        )
    home = Path(data_home) if data_home is not None else get_data_home()
    setup_file_logging(home)

    with index_lock(home, source=source, root=str(root)):
        root_id = ensure_root(conn, str(root))
        backfill_files_under_root(conn, root_id, str(root))

        if mode == "rebuild":
            reset_files_under_root(conn, root)

        engines = build_index_engines(conn, config, vector_backend, embed_provider_cls=embed_provider_cls)
        summary = index_folder(
            conn,
            root,
            verify=(mode == "verify"),
            tier2_engine=engines.tier2,
            tier3_engine=engines.tier3,
            tier4_engine=engines.tier4,
            chunk_config=engines.chunk_config,
            embed_provider=engines.embed_provider,
            embed_config=engines.embed_config,
            embed_batch_size=engines.embed_batch_size,
            vector_index=engines.vector_index,
            on_event=on_event,
            should_cancel=should_cancel,
            indexed_folders=config.indexed_folders,
            root_id=root_id,
            structure_engine=engines.structure_engine,
            structure_analyze_native=engines.structure_analyze_native,
            excluded_paths=resolve_excluded_abs_paths(config),
        )

    if register_folder:
        _register_folder(root, config_path=get_config_path(home))

    return summary


def run_sidecar_index_for_root(
    conn: sqlite3.Connection,
    root: Path,
    *,
    config: Config,
    vector_backend: str,
    run_id: int | None = None,
    mode: Literal["incremental", "rebuild"] = "incremental",
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    register_folder: bool = True,
    embed_provider_cls: type[EmbeddingProvider] | None = None,
    source: Literal["cli", "gui"] = "gui",
    data_home: Path | None = None,
) -> SidecarIndexRunSummary:
    """Locked Phase 2 entry point used by the GUI.

    Unlike ``run_index`` (the retained legacy CLI pipeline), this path
    constructs no extraction/OCR engines and accepts searchable content only
    from adjacent, validated VectorDrive Markdown artifacts.
    """
    from vectordrive.config import get_data_home

    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise RootUnavailable(
            f"{root} does not exist or is not a folder. Its index has been preserved.",
            hint="Reconnect the folder before resuming. No source or Markdown files were changed.",
        )
    home = Path(data_home) if data_home is not None else get_data_home()
    setup_file_logging(home)
    with index_lock(home, source=source, root=str(root)):
        root_id = ensure_root(conn, str(root))
        backfill_files_under_root(conn, root_id, str(root))
        if mode == "rebuild":
            # Match direct-index Full Rebuild semantics before consuming the
            # freshly generated sidecars. This stays inside the same writer
            # lock as Phase 2, so no reader can observe a competing rebuild.
            reset_files_under_root(conn, root)
        if embed_provider_cls is None:
            # Resolve at call time so packaged/runtime substitution and unit
            # tests can replace the provider without constructing OCR engines.
            from vectordrive.services import engines as engines_module

            embed_provider_cls = engines_module.OllamaEmbeddingProvider
        engines = build_sidecar_index_engines(
            conn, config, vector_backend, embed_provider_cls=embed_provider_cls
        )
        result = run_sidecar_index(
            conn,
            root,
            embed_provider=engines.embed_provider,
            embed_config=engines.embed_config,
            vector_index=engines.vector_index,
            chunker_config=engines.chunk_config,
            embed_batch_size=engines.embed_batch_size,
            root_id=root_id,
            run_id=run_id,
            on_event=on_event,
            should_cancel=should_cancel,
            excluded_paths=resolve_excluded_abs_paths(config),
        )
    if register_folder:
        _register_folder(root, config_path=get_config_path(home))
    return result


def retry_failed_sidecar_index_for_root(
    conn: sqlite3.Connection,
    root: Path,
    **kwargs,
) -> SidecarIndexRunSummary:
    """Create a successor of the latest failed Phase 2 run.

    The sidecar runner carries forward matching completed items and queues
    failed or changed items.  It never routes retries through source OCR.
    """
    root = Path(root).resolve()
    row = conn.execute(
        "SELECT id FROM processing_runs WHERE phase='index' AND root_path=? "
        "AND status='failed' ORDER BY id DESC LIMIT 1",
        (str(root),),
    ).fetchone()
    return run_sidecar_index_for_root(
        conn, root, run_id=(int(row["id"]) if row is not None else None), **kwargs
    )


def retry_failed(
    conn: sqlite3.Connection,
    root: Path,
    *,
    config: Config,
    vector_backend: str,
    on_event: EventCallback | None = None,
    should_cancel: CancelCallback | None = None,
    embed_provider_cls: type[EmbeddingProvider] = OllamaEmbeddingProvider,
    source: Literal["cli", "gui"] = "cli",
    data_home: Path | None = None,
) -> IndexRunSummary:
    """Reset only status='failed' rows under `root`, then run an
    incremental index. Already-indexed files under the same root are
    untouched; the reset rows will cache-hit extract/OCR/chunk (unchanged
    content under a cached content_hash) and only redo whatever actually
    failed last time (commonly: embeddings, if Ollama was down).
    """
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise RootUnavailable(
            f"{root} does not exist or is not a folder. Its index has been preserved.",
            hint="If the folder moved or was renamed, use Relink Folder…; "
            "if it is gone for good, remove it explicitly.",
        )
    from vectordrive.config import get_data_home

    home = Path(data_home) if data_home is not None else get_data_home()
    setup_file_logging(home)

    with index_lock(home, source=source, root=str(root)):
        root_id = ensure_root(conn, str(root))
        backfill_files_under_root(conn, root_id, str(root))

        _reset_failed_files_under_root(conn, root)
        engines = build_index_engines(conn, config, vector_backend, embed_provider_cls=embed_provider_cls)
        return index_folder(
            conn,
            root,
            verify=False,
            tier2_engine=engines.tier2,
            tier3_engine=engines.tier3,
            tier4_engine=engines.tier4,
            chunk_config=engines.chunk_config,
            embed_provider=engines.embed_provider,
            embed_config=engines.embed_config,
            embed_batch_size=engines.embed_batch_size,
            vector_index=engines.vector_index,
            on_event=on_event,
            should_cancel=should_cancel,
            indexed_folders=config.indexed_folders,
            root_id=root_id,
            structure_engine=engines.structure_engine,
            structure_analyze_native=engines.structure_analyze_native,
            excluded_paths=resolve_excluded_abs_paths(config),
        )


def latest_run_errors(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Parse index_runs.errors_json for one run — the per-file error list
    the GUI's "Details ▾" expander shows.
    """
    row = conn.execute("SELECT errors_json FROM index_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["errors_json"] is None:
        return []
    return json.loads(row["errors_json"])


@dataclass(frozen=True)
class FolderPreview:
    root: str
    supported_count: int
    unsupported_count: int
    total_bytes: int
    by_type: dict[str, int]
    sampled: bool
    walk_errors: list[str]


def preview_folder(
    root: Path,
    *,
    should_cancel: CancelCallback | None = None,
    max_files: int = 50_000,
    max_seconds: float = 20.0,
) -> FolderPreview:
    """Pure filesystem walk (touches no DB, writes nothing) reusing the
    indexer's own file classification (`_classify`/`_iter_files`) so the
    "N supported files" a user sees before indexing is guaranteed to
    match what indexing will actually process — not a second guess at
    the same extension table.

    Capped at `max_files`/`max_seconds` (whichever comes first) so a
    pathologically large tree can't hang the Folders screen's "Add
    folder" preview; `sampled=True` tells the caller the counts are a
    partial estimate, not a complete scan.
    """
    root = Path(root)
    supported_count = 0
    unsupported_count = 0
    total_bytes = 0
    by_type: dict[str, int] = {}
    walk_errors: list[str] = []
    sampled = False
    seen = 0
    start = time.monotonic()

    def _on_walk_error(bad_path: Path, exc: OSError) -> None:
        walk_errors.append(f"{bad_path}: {exc}")

    for path in _iter_files(root, should_cancel=should_cancel, on_error=_on_walk_error):
        if should_cancel is not None and should_cancel():
            sampled = True
            break
        if seen >= max_files or (time.monotonic() - start) >= max_seconds:
            sampled = True
            break
        seen += 1

        try:
            size = path.stat().st_size
        except OSError as exc:
            walk_errors.append(f"{path}: {exc}")
            continue

        file_type = _classify(path)
        if file_type is None:
            unsupported_count += 1
        else:
            supported_count += 1
            total_bytes += size
            by_type[file_type] = by_type.get(file_type, 0) + 1

    if should_cancel is not None and should_cancel():
        sampled = True

    return FolderPreview(
        root=str(root),
        supported_count=supported_count,
        unsupported_count=unsupported_count,
        total_bytes=total_bytes,
        by_type=by_type,
        sampled=sampled,
        walk_errors=walk_errors,
    )
