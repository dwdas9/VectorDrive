"""`vectordrive status`: the latest index_runs row, per-status file counts,
and the active embedding configuration + vector backend — all read
directly from existing tables/config, no new state.
"""
from __future__ import annotations

import sqlite3

from vectordrive.cli.format import format_table
from vectordrive.config import get_db_path, load_config
from vectordrive.db.connection import connect
from vectordrive.services.status_service import get_status_snapshot


def run_status_command() -> int:
    db_path = get_db_path()
    if not db_path.exists():
        print(f"error: no index found at {db_path}. Run `vectordrive index --path <folder>` first.")
        return 1

    config = load_config()
    try:
        db = connect(db_path)
    except sqlite3.DatabaseError as exc:
        # M12: db_path.exists() only rules out a *missing* file — a
        # corrupt one (exists, isn't valid SQLite) only fails once connect()
        # actually tries to read it. Caught here so this prints a clear
        # message instead of an unhandled traceback.
        print(f"error: the database at {db_path} appears corrupt: {exc}")
        return 1

    # v0.2 G1: the three queries below moved to services/status_service.py
    # (shared with the GUI's Overview screen, G4); this function now only
    # formats/prints the snapshot it returns.
    snapshot = get_status_snapshot(
        db.conn,
        embedding_provider=config.embedding_provider,
        embedding_model=config.embedding_model,
        embedding_dims=config.embedding_dims,
        vector_backend=db.vector_backend,
        db_path=db_path,
    )

    if snapshot.latest_run is None:
        print("No indexing run recorded yet. Run `vectordrive index --path <folder>` to start one.")
    else:
        latest_run = snapshot.latest_run
        print("Latest run:")
        print(
            format_table(
                ["Started", "Finished", "Indexed", "Skipped", "Unsupported", "Failed"],
                [[
                    latest_run["started_at"],
                    latest_run["finished_at"] or "(in progress)",
                    str(latest_run["files_indexed"]),
                    str(latest_run["files_skipped"]),
                    str(latest_run["files_unsupported"]),
                    str(latest_run["files_failed"]),
                ]],
            )
        )

    print("\nFile status counts:")
    print(
        format_table(
            ["Status", "Count"],
            [[status, str(n)] for status, n in snapshot.file_status_counts.items()],
        )
    )
    if snapshot.ocr_warning_count:
        # G12 finding: 'indexed' above includes files whose OCR failed or
        # produced no searchable text — this is not a separate `status`
        # value (see pipeline/indexer.py's ocr_warning), so it needs its
        # own line here rather than being hidden inside "indexed".
        print(
            f"\n{snapshot.ocr_warning_count} indexed file(s) have OCR warnings "
            "(failed OCR or no searchable text was extracted). "
            "Run `vectordrive index --path <folder> --rebuild` after fixing "
            "the OCR engine to retry them."
        )

    print("\nEmbedding configuration:")
    print(
        format_table(
            ["Provider", "Model", "Dimensions", "Vector backend"],
            [[
                snapshot.embedding_provider,
                snapshot.embedding_model,
                str(snapshot.embedding_dims),
                snapshot.vector_backend,
            ]],
        )
    )

    return 0
