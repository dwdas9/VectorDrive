"""SQLite connection helper: applies schema.sql and attempts to load
sqlite-vec, recording which vector backend ("sqlite_vec" or "numpy") is
available on this connection.

Full Coverage Pass, Section 1: the database is a disposable build
artifact, fully derived from indexed files on disk — connect() no longer
migrates an existing database in place. It used to (additive ALTER-only
upgrades, never dropping a real table); that's gone. The replacement is
drift *detection*: schema_hash (sha256 of schema.sql's own text) is
written into schema_version once, at creation. Every later open compares
schema.sql's current hash against that stored value; any mismatch means
schema.sql changed since this database was built, and connect() refuses
to open it — raising SchemaDriftError with a structural diff — rather
than silently drifting out of sync with it or attempting a partial fix.

Why: the previous additive-migration design is exactly what let the
pages.text_source CHECK constraint drift happen. schema.sql gained
'merged' as a valid value; SQLite cannot ALTER a CHECK constraint, so
nothing short of a real migration step could have applied that to an
existing database — and no such step was ever written, because nothing
detected that one was needed. The live database kept accepting
connections, kept "working," and silently rejected every write Section
1's OCR-merge feature ever attempted, for a whole release, at real cost
(16 real documents — passports, NRIC, a PR application, voter IDs —
failing to index). Recovery is `vectordrive reset` (cli/reset_cmd.py):
drop the database file, recreate from schema.sql, reindex. Roots stay
registered in config.toml, so that's a bounded, mechanical step — not a
migration to get right under pressure.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class SchemaDriftError(RuntimeError):
    """Raised by connect() when an existing database's schema no longer
    matches schema.sql. Carries a human-readable structural diff in its
    message — see _diff_schema()."""


def _vd_basename(path: str) -> str:
    """Just the filename — e.g. "Name_Correction_README.txt" from a full
    path. The strongest fts_files signal (see schema.sql): what a user
    actually remembers a file being *called*, independent of which
    machine, account, or folder depth it happens to live under.
    """
    return Path(path).name


def _vd_folder_path(relative_path: str | None) -> str:
    """The folder a file lives in, relative to its registered indexed
    root — never the machine's absolute path (schema.sql explains why).
    A weaker, optional fts_files signal: files with no relative_path
    (indexed before roots existed, or via the bare CLI path with no root
    registration) get "" here and rely on basename alone, never falling
    back to any absolute-path text.
    """
    if not relative_path:
        return ""
    parent = str(Path(relative_path).parent)
    return "" if parent == "." else parent


def _register_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("vd_basename", 1, _vd_basename)
    conn.create_function("vd_folder_path", 1, _vd_folder_path)


@dataclass
class DbConnection:
    conn: sqlite3.Connection
    vector_backend: str  # "sqlite_vec" | "numpy"


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension on `conn`.

    This only proves the extension *loads* on this connection. The real
    Apple Silicon smoke test (insert + KNN round-trip) lives in
    vectordrive.cli.doctor, which is what actually decides and persists the
    vector backend into config.toml.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        return False


def compute_schema_hash() -> str:
    """sha256 of schema.sql's own raw text. Deliberately coarse — any
    byte changes the hash, including a comment-only edit — because the
    whole point is that nothing about "is this different" is left to
    human judgment or a manually bumped counter (a plain version integer
    needs exactly the discipline that failed to catch the CHECK
    constraint drift last time).
    """
    return hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def _is_empty_database(conn: sqlite3.Connection) -> bool:
    """True only for a database with no user tables at all yet — the one
    case connect() applies schema.sql fresh. Checking for "no tables at
    all" rather than just "no schema_version table" matters: a database
    that already has *some* tables (e.g. a hand-rolled pre-hash-tracking
    shape, or any other old database) but happens to be missing
    schema_version is drift, not a blank slate — running schema.sql's
    CREATE TABLE IF NOT EXISTS statements over it would silently skip
    every table that already exists in its old shape while still
    creating whatever's new, leaving a database that's neither the old
    shape nor the new one and isn't flagged as either.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row is None


def _stored_schema_hash(conn: sqlite3.Connection) -> str | None:
    """None means "no usable stored hash" — either no row at all, or (a
    database from before this feature existed) a schema_version table
    with no schema_hash column yet. Both fold into the same drift path:
    a proper SchemaDriftError telling the user to reset, not a raw
    sqlite3.OperationalError leaking a column name.
    """
    try:
        row = conn.execute("SELECT schema_hash FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row["schema_hash"] if row is not None else None


def _schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(type, name) -> CREATE ... text for every real object in this
    connection's schema (tables/indexes/triggers/views, including FTS5's
    auto-generated shadow tables — those are structurally part of "what
    the CREATE VIRTUAL TABLE statement produced" and belong in the diff
    like anything else). Excludes SQLite's own internal sqlite_% objects
    and anything with a NULL sql (autoindexes).
    """
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
    ).fetchall()
    return {(row["type"], row["name"]): row["sql"] for row in rows}


def _reference_schema_objects() -> dict[tuple[str, str], str]:
    """What schema.sql itself would build, fresh, right now — the source
    of truth _diff_schema() compares a live database against."""
    ref = sqlite3.connect(":memory:")
    ref.row_factory = sqlite3.Row
    _register_functions(ref)
    ref.executescript(SCHEMA_PATH.read_text())
    objects = _schema_objects(ref)
    ref.close()
    return objects


def _diff_schema(conn: sqlite3.Connection) -> str:
    """Human-readable structural diff: not just "the hash changed" but
    which objects were added, removed, or altered — e.g. exactly the
    pages.text_source CHECK-constraint change that motivated this whole
    mechanism, spelled out instead of merely detected.
    """
    live = _schema_objects(conn)
    expected = _reference_schema_objects()

    lines: list[str] = []
    for key in sorted(set(expected) - set(live)):
        lines.append(f"  + {key[0]} {key[1]}  (in schema.sql, missing from this database)")
    for key in sorted(set(live) - set(expected)):
        lines.append(f"  - {key[0]} {key[1]}  (in this database, no longer in schema.sql)")
    for key in sorted(set(live) & set(expected)):
        if live[key] != expected[key]:
            lines.append(f"  ~ {key[0]} {key[1]} changed:")
            lines.append(f"      database:   {live[key]}")
            lines.append(f"      schema.sql: {expected[key]}")

    if not lines:
        return "  (schema.sql's text changed, but no structural difference was detected — e.g. a comment-only edit)"
    return "\n".join(lines)


# Additive, in-place migrations for an already-initialized database.
#
# The default policy for this schema is drift *detection*, not migration
# (see the module docstring): the database is a disposable build artifact.
# A small allow-list of purely-additive objects is the deliberate
# exception — new tables/indexes with no foreign keys, no triggers, and no
# effect on any existing object, where recreating the whole database just
# to gain an empty table would be gratuitous. Each entry's DDL text is a
# verbatim copy of the matching statement in schema.sql; if it ever drifts
# from schema.sql the post-migration structural comparison below will fail
# to match and connect() will still raise SchemaDriftError rather than
# silently re-stamp a wrong hash.
_ADDITIVE_MIGRATIONS: dict[tuple[str, str], str] = {
    ("table", "file_exclusions"): (
        "CREATE TABLE IF NOT EXISTS file_exclusions (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    root_path TEXT NOT NULL,\n"
        "    relative_path TEXT NOT NULL,\n"
        "    abs_path TEXT NOT NULL,\n"
        "    reason TEXT,\n"
        "    excluded_at TEXT NOT NULL,\n"
        "    UNIQUE (root_path, relative_path)\n"
        ")"
    ),
    ("index", "idx_file_exclusions_abs_path"): (
        "CREATE INDEX IF NOT EXISTS idx_file_exclusions_abs_path "
        "ON file_exclusions(abs_path)"
    ),
}


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing database forward across purely-additive schema
    changes without a full reset, then re-stamp schema_hash — but only
    when the sole difference from schema.sql is missing allow-listed
    objects. Any removed or structurally-changed object is left untouched
    for check_schema_drift() to reject, so this never masks the kind of
    drift the hash mechanism exists to catch.
    """
    if _stored_schema_hash(conn) == compute_schema_hash():
        return  # already current

    live = _schema_objects(conn)
    expected = _reference_schema_objects()
    missing = set(expected) - set(live)
    removed = set(live) - set(expected)
    changed = {key for key in set(live) & set(expected) if live[key] != expected[key]}

    if removed or changed:
        return  # real drift — check_schema_drift() will raise
    if not missing or not missing <= set(_ADDITIVE_MIGRATIONS):
        return  # nothing to do, or a missing object we have no migration for

    _order = {"table": 0, "index": 1, "trigger": 2, "view": 3}
    for key in sorted(missing, key=lambda k: _order.get(k[0], 4)):
        conn.executescript(_ADDITIVE_MIGRATIONS[key])
    conn.commit()

    # Only re-stamp if the database now matches schema.sql exactly.
    if _schema_objects(conn) == expected:
        conn.execute("UPDATE schema_version SET schema_hash = ?", (compute_schema_hash(),))
        conn.commit()


def check_schema_drift(conn: sqlite3.Connection) -> None:
    """Raise SchemaDriftError if an already-initialized database's stored
    schema_hash doesn't match schema.sql's current hash. A no-op for a
    database with no schema_version table at all yet (connect() handles
    that as "brand new" before this is ever called).
    """
    stored_hash = _stored_schema_hash(conn)
    current_hash = compute_schema_hash()
    if stored_hash == current_hash:
        return

    diff = _diff_schema(conn)
    raise SchemaDriftError(
        "Database schema no longer matches schema.sql "
        f"(stored hash {stored_hash!r}, current hash {current_hash!r}).\n\n"
        f"What changed:\n{diff}\n\n"
        "This database is fully derived from indexed files and is not migrated in "
        "place. Run `vectordrive reset` to drop and recreate it from the current "
        "schema, then reindex your folders."
    )


def connect(db_path: Path) -> DbConnection:
    """Open (creating if needed) the VectorDrive SQLite database at
    `db_path`, apply the core schema on first creation, and report the
    vector backend. Raises SchemaDriftError instead of opening an
    existing database whose schema has drifted from schema.sql — see the
    module docstring.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _register_functions(conn)  # vd_basename/vd_folder_path, used by fts_files's triggers below

    vector_backend = "sqlite_vec" if _try_load_sqlite_vec(conn) else "numpy"

    if _is_empty_database(conn):
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute("UPDATE schema_version SET schema_hash = ?", (compute_schema_hash(),))
        conn.commit()
    else:
        _apply_additive_migrations(conn)  # purely-additive allow-listed objects only
        check_schema_drift(conn)  # raises SchemaDriftError on mismatch; conn stays open on the caller

    return DbConnection(conn=conn, vector_backend=vector_backend)
