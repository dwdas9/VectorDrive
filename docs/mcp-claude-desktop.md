# Claude Desktop and MCP

VectorDrive ships a **read-only** MCP server: it cannot index, modify,
rebuild, verify, prune, or delete anything. It only reads already-indexed
data through a database connection opened in SQLite's `mode=ro` +
`PRAGMA query_only=ON`. This is enforced at the connection level, not just
by convention.

## The three tools

- **`search_drive(query, top_k=10, mode="hybrid")`**: same `fts`/
  `vector`/`hybrid` modes as the CLI (see the
  [User Guide](user-guide.md#searching)). Returns structured citations:
  path, page, chunk index, source type, excerpt, score. `top_k` is capped
  at 50.
- **`read_document(path, page=None)`**: `path` must be one already
  recorded as successfully indexed (citations from `search_drive` are
  exactly such paths); anything else is rejected, including path
  traversal and symlink escapes. The server never opens an arbitrary
  file from disk; it only reads previously-extracted text already stored
  in the database. A specific `page` returns just that page; omitting it
  returns a bounded preview (≤4000 characters), explicitly marked
  `truncated` when content was cut.
- **`index_status()`**: the latest indexing run's summary, per-status
  file counts, embedding model/dimensions, and vector backend. No secrets,
  no local file paths beyond what's already implied by citations.

**Privacy note:** only the passages a tool call actually returns are ever
sent to Claude: a handful of bounded excerpts, or a capped preview/single
page you (or Claude, on your behalf) explicitly requested. Never your full
document collection, never a file Claude hasn't asked for by path.

## Configuration example

Using the permanent `VECTORDRIVE_HOME` (see
[Storage & Backup](storage-and-backup.md)):

```json
{
  "mcpServers": {
    "vectordrive": {
      "command": "/absolute/path/to/vectordrive/.venv/bin/vectordrive",
      "args": ["mcp"],
      "env": {
        "VECTORDRIVE_HOME": "/absolute/path/to/your/home/Library/Application Support/VectorDrive"
      }
    }
  }
}
```

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
yourself. VectorDrive never writes to it. Both `command` and the
`VECTORDRIVE_HOME` value must be **absolute paths**; if either contains a
space (as `Application Support` does), the JSON string itself doesn't
need extra shell-style quoting; just make sure it's one JSON string, not
split across multiple array elements. (If you ever invoke a path like
this directly in a shell instead of JSON, wrap it in double quotes:
`"$HOME/Library/Application Support/VectorDrive"`.)

Since `~/Library/Application Support/VectorDrive` is already the default
`VECTORDRIVE_HOME`, the `env` block above is only required if you use a
non-default location.

## Test the server before editing Claude Desktop

Don't debug MCP issues by trial-and-error inside Claude Desktop. Test the
server directly first, over the same stdio protocol Claude Desktop uses:

```bash
.venv/bin/python -m vectordrive.cli.main mcp
```

This blocks, waiting for JSON-RPC on stdin. It's meant to be driven by a
client, not typed at. To actually exercise it, use a real MCP client
library (e.g. Python's `mcp.client.stdio` + `mcp.client.session`) to spawn
this exact command, call `list_tools()`, and call each tool. This is
precisely how VectorDrive's own test suite validates the server
(`tests/integration/test_mcp_protocol.py`): real stdio JSON-RPC, not
mocked internals.

## Restart behaviour

The MCP server builds its search context once at startup and reuses it for
every tool call: restarting the server does not rebuild the sqlite-vec
index (typical startup-to-first-result latency is well under a second).
If you re-index while an MCP
server is already running, restart it afterward to pick up the new data;
it doesn't watch the database for live changes.

## Troubleshooting

- **Server won't start / Claude Desktop shows it as failed:** run the
  direct stdio command above yourself first; a startup error prints to
  stderr, which Claude Desktop's own logs will show but your terminal
  makes easier to read directly.
- **No database found:** the server still starts and lists its tools even
  with no database: every tool call then returns a clear error
  (`No VectorDrive database found at ...`) instead of the server crashing.
  Run `vectordrive index --path ...` first.
- **Tool calls return errors after working before:** check whether Ollama
  stopped (`vector`/`hybrid` search need it; `fts` doesn't) or whether the
  database became corrupt. See [Troubleshooting](troubleshooting.md).
- **Claude Desktop config not picking up changes:** fully quit and restart
  Claude Desktop after editing `claude_desktop_config.json`; it doesn't
  hot-reload the MCP server list.
