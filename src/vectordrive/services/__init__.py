"""Shared service layer — the single place CLI, MCP and GUI construct
engines/providers from Config and query DB state, so no interface
duplicates another's business logic (v0.2 G1).

Every function here must be importable and testable without importing
`webview`, `argparse`, or the MCP SDK — see docs/gui.md and
.claude/plans/continue-vectordrive-from-docs-current-recursive-noodle.md
for the full design rationale.
"""
