"""`vectordrive index --path <folder> [--rebuild | --verify]`: wires the
M9 folder orchestrator (index_folder) to real engines/providers chosen
from the persisted config, and prints readable indexed/skipped/
unsupported/failed counts plus per-file errors. Contains no pipeline or
search logic of its own (requirement 3) — every stage runs exactly what
M0-M9 already built.

M10.5 requirement 7: --rebuild and --verify are mutually exclusive
(--rebuild forces full reprocessing by resetting file rows before
index_folder even sees them; --verify instead asks index_folder to
recompute hashes despite unchanged metadata, but only reprocess files
whose content actually changed — combining the two is contradictory).

Deferred decision (requirement 14, recorded here rather than silently
dropped): a file that disappears from the indexed folder between runs is
NOT detected or cleaned up in M10/M10.5 — index_folder only distinguishes
unchanged vs. changed files it still finds on disk. A `--prune`-style pass
that removes DB rows for vanished files is a candidate for a later
milestone, not implemented here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from vectordrive.cli.format import format_table
from vectordrive.config import get_db_path, load_config
from vectordrive.db.connection import connect
from vectordrive.pipeline.embed.ollama_provider import OllamaEmbeddingProvider
from vectordrive.services.errors import IndexLocked
from vectordrive.services.index_service import run_index


def _print_summary(conn, summary) -> None:
    print(
        format_table(
            ["Indexed", "Skipped", "Unsupported", "Failed"],
            [[str(summary.indexed), str(summary.skipped), str(summary.unsupported), str(summary.failed)]],
        )
    )

    # M-indexing-fix: a directory os.walk couldn't scan at all doesn't show
    # up in any of the four counts above (see IndexRunSummary.walk_errors),
    # so this used to print nothing at all for that case -- silently
    # implying a clean run. walk_errors is checked here too, not just
    # `failed`, so it's never hidden.
    if summary.walk_errors:
        print(
            f"\n{summary.walk_errors} director"
            f"{'y' if summary.walk_errors == 1 else 'ies'} could not be scanned "
            "-- files inside may be missing from the counts above. See Errors below."
        )

    if summary.failed or summary.walk_errors:
        import json

        row = conn.execute(
            "SELECT errors_json FROM index_runs WHERE id = ?", (summary.run_id,)
        ).fetchone()
        errors = json.loads(row["errors_json"]) if row and row["errors_json"] else []
        if errors:
            print("\nErrors:")
            print(format_table(["Path", "Error"], [[e["path"], e["error"]] for e in errors]))


def run_index_command(path: str, rebuild: bool = False, verify: bool = False, register: bool = True) -> int:
    """v0.2 G2: `register` implements the approved v0.2 Decision 2 —
    `vectordrive index --path X` auto-registers X into config.
    indexed_folders (idempotent) so the GUI's Folders catalogue never
    drifts from what the CLI has actually indexed. `--no-register`
    (cli/main.py) sets register=False for a one-off/temporary index that
    shouldn't permanently appear in the GUI.

    Also v0.2 G2: takes the index lock (services.locking, via
    services.index_service.run_index) so a concurrent `vectordrive index`
    or GUI indexing job is refused with a clear message instead of
    silently interleaving writes — exit code 3, distinct from the other
    error exit codes here, so scripts can distinguish "another indexer is
    running, try again" from a real failure.
    """
    if rebuild and verify:
        print("error: --rebuild and --verify are mutually exclusive")
        return 2

    folder = Path(path).expanduser()
    if not folder.is_dir():
        print(f"error: not a folder: {folder}")
        return 2

    config = load_config()
    try:
        db = connect(get_db_path())
    except sqlite3.DatabaseError as exc:
        print(f"error: the database at {get_db_path()} appears corrupt: {exc}")
        return 1

    mode = "rebuild" if rebuild else ("verify" if verify else "incremental")

    # v0.2 G1/G2: engine construction + locking + folder registration all
    # moved to services/index_service.py (shared with the GUI, G6).
    # OllamaEmbeddingProvider is still passed through explicitly so
    # tests/unit/test_cli_index.py's
    # `monkeypatch.setattr(index_cmd, "OllamaEmbeddingProvider", ...)`
    # keeps taking effect exactly as before.
    try:
        summary = run_index(
            db.conn,
            folder,
            config=config,
            vector_backend=db.vector_backend,
            mode=mode,
            register_folder=register,
            embed_provider_cls=OllamaEmbeddingProvider,
            source="cli",
        )
    except IndexLocked as exc:
        print(f"error: {exc}")
        return 3

    _print_summary(db.conn, summary)
    return 0
