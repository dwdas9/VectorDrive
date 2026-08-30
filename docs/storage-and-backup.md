# Storage, Backup and Recovery

## Two separate things live in two separate places

- **Your source documents** stay exactly where they are: in Google Drive,
  a local folder, wherever you pointed `index --path` at. VectorDrive never
  moves, renames, or edits them; it only reads them.
- **`VECTORDRIVE_HOME`** holds everything VectorDrive itself generates: the
  SQLite database (extracted text, OCR results, chunks, embeddings, the
  vector index), a small on-disk cache, logs, and `config.toml`. This is a
  local catalogue *about* your documents, not a copy of the documents
  themselves (see [Architecture](architecture.md#data-lifecycle) for
  exactly what is and isn't stored).

Default location on macOS: `$HOME/Library/Application Support/VectorDrive`.
Override with the `VECTORDRIVE_HOME` environment variable.

## Never store the live database in a synced folder

Don't point `VECTORDRIVE_HOME` at Google Drive, OneDrive, Dropbox, iCloud
Drive, or any other folder a sync client watches. SQLite's WAL mode
(which VectorDrive uses) writes to the database through short-lived
`-wal`/`-shm` sidecar files that a sync client can upload mid-write,
producing a corrupted or inconsistent copy on other devices; sync clients
are built for whole-file changes, not for a database's write pattern.
`vectordrive doctor`'s `data_dir_outside_repo` check catches one class of
misconfiguration (data dir inside the *code* repo); it does not currently
detect "data dir inside a synced cloud folder"; that's on you to avoid.

## Permanent location

```bash
export VECTORDRIVE_HOME="$HOME/Library/Application Support/VectorDrive"
```

This is macOS's *default*: if you never set `VECTORDRIVE_HOME` at all,
VectorDrive already resolves here. Set the variable explicitly only if you
want a non-default location, or want it to be unambiguous in scripts.
Never hardcode a specific user's home directory path (e.g. avoid
`/Users/someone/...`); always derive from `$HOME` so the same command
works for anyone.

## Mac and Windows should not share one index

If you use VectorDrive on more than one machine, each machine should have
its own `VECTORDRIVE_HOME` and its own database. There is no sync/merge
mechanism for the database itself (and per the section above, you
shouldn't put it in a synced folder to make one happen). Index the same
source folder independently on each machine.

## One index, many source folders

A single `VECTORDRIVE_HOME`/database can hold files from any number of
different source folders; there's no concept of "one index per folder."
Run `vectordrive index --path` once per folder you want included; `status`
and `search` then cover all of them together. See the
[User Guide](user-guide.md#multiple-source-folders-one-index).

## Backup

The entire catalogue is the `VECTORDRIVE_HOME` directory. To back it up:

```bash
cp -R "$HOME/Library/Application Support/VectorDrive" /path/to/backup-location
```

Do this while no `vectordrive index`/`mcp` process is running, so the
database isn't mid-write. A closed SQLite database (no active writer) is
safe to copy as a single file; no special export step needed.

## Integrity checks

```bash
sqlite3 "$HOME/Library/Application Support/VectorDrive/vectordrive.db" "PRAGMA integrity_check;"
```

Expect the single line `ok`. If your system `sqlite3` CLI doesn't have the
sqlite-vec extension loaded, `PRAGMA integrity_check` and
`PRAGMA foreign_key_check` still work fine against every table except the
`vec_chunks` virtual table; check that one from Python instead:

```bash
.venv/bin/python -c "
import sqlite3, sqlite_vec
conn = sqlite3.connect('$HOME/Library/Application Support/VectorDrive/vectordrive.db')
conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
print(conn.execute('SELECT COUNT(*) FROM vec_chunks').fetchone())
"
```

## Restore

Copy a backed-up `VECTORDRIVE_HOME` directory back into place (or point
`VECTORDRIVE_HOME` at the backup directly) with no `vectordrive` process
running. No migration or rebuild step is needed. VectorDrive's schema
migration (`connection.py`'s `_migrate()`) is additive-only and runs
automatically on next connect if the backup predates a newer column.

## Moving `VECTORDRIVE_HOME` safely

1. Confirm no `vectordrive index` or `vectordrive mcp` process is running.
2. Run `PRAGMA integrity_check` on the current database (see above) and
   record the row counts you care about (`files`, `chunks`, `embeddings`,
   etc.; see [Architecture](architecture.md) for the full schema) as a
   before/after baseline.
3. Copy the whole `VECTORDRIVE_HOME` directory (database, `config.toml`,
   `cache/`, `logs/`) to the new location; a plain recursive copy, no
   special tooling required.
4. Point `VECTORDRIVE_HOME` at the new location and re-run the integrity
   check and row counts; they should match exactly.
5. Confirm `vectordrive status` and a known search still work from the new
   location before relying on it.
6. Keep the old location until you've confirmed the new one works; don't
   delete it in the same step as the move.

This procedure (copy, verify integrity, verify row counts, verify
`status`/search/MCP work, keep the old copy as rollback) leaves no
window where you don't have a working copy of your data.
