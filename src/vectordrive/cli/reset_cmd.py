"""CLI adapter for the allow-listed derived-state reset service."""
from __future__ import annotations

import sys
from pathlib import Path

from vectordrive.config import get_data_home, get_db_path
from vectordrive.services.errors import IndexLocked
from vectordrive.services.reset_service import reset_derived_state


def run_reset_command(data_home: Path | None = None, *, yes: bool = False) -> int:
    home = Path(data_home) if data_home is not None else get_data_home()
    db_path = get_db_path(home)

    if not yes:
        answer = input(
            f"This will delete VectorDrive's derived database, cache, logs, "
            f"backups, and review handoffs under {home}. config.toml, registered "
            "source documents, and every *.vectordrive.md file are untouched. "
            "No processing job may be running. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing changed.")
            return 1

    try:
        result = reset_derived_state(home)
    except IndexLocked as exc:
        print(f"Reset refused: {exc}", file=sys.stderr)
        return 2

    if result.database_existed:
        print(f"Reset: {db_path} recreated from the current schema.")
    else:
        print(f"{db_path} did not exist yet — created fresh from the current schema.")
    print(
        "config.toml, registered source documents, and *.vectordrive.md files "
        "were untouched. Run "
        "`vectordrive index --path <folder>` for each to reindex."
    )
    return 0
