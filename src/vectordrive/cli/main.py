"""VectorDrive CLI entry point: doctor, index, search, status.

Each subcommand's real logic lives in its own module (cli.doctor,
cli.index_cmd, cli.search_cmd, cli.status_cmd); this file only builds the
argparse parser and dispatches to them (requirement 3: no duplicated
pipeline/search logic here).
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence

import vectordrive
from vectordrive.cli.doctor import CheckStatus, doctor_exit_code, run_doctor
from vectordrive.cli.gui_cmd import run_gui_command
from vectordrive.cli.index_cmd import run_index_command
from vectordrive.cli.mcp_cmd import run_mcp_command
from vectordrive.cli.reset_cmd import run_reset_command
from vectordrive.cli.search_cmd import run_search_command
from vectordrive.cli.status_cmd import run_status_command

_STATUS_ICON = {
    CheckStatus.PASS: "PASS",
    CheckStatus.WARN: "WARN",
    CheckStatus.FAIL: "FAIL",
}


def _run_doctor_command() -> int:
    results = run_doctor()
    for result in results:
        marker = "(optional) " if not result.required else ""
        print(f"[{_STATUS_ICON[result.status]}] {marker}{result.name}: {result.message}")
    exit_code = doctor_exit_code(results)
    if exit_code == 0:
        print("\ndoctor: all required checks passed.")
    else:
        print("\ndoctor: one or more required checks failed.")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vectordrive")
    parser.add_argument(
        "--version", action="version", version=f"vectordrive {vectordrive.__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor", help="Run environment and dependency smoke tests."
    )

    index_parser = subparsers.add_parser("index", help="Index a folder of documents.")
    index_parser.add_argument("--path", required=True, help="Folder to index.")
    verify_group = index_parser.add_mutually_exclusive_group()
    verify_group.add_argument(
        "--rebuild", action="store_true",
        help="Force reprocessing of every file, ignoring the skip-unchanged cache.",
    )
    verify_group.add_argument(
        "--verify", action="store_true",
        help="Recompute content hashes even when size/mtime look unchanged, "
        "reprocessing only files whose content actually changed.",
    )
    index_parser.add_argument(
        "--no-register", action="store_true",
        help="v0.2: by default, indexing a folder adds it to the GUI's folder "
        "catalogue (config.toml's indexed_folders). Pass this for a one-off or "
        "temporary index that shouldn't permanently show up there.",
    )

    search_parser = subparsers.add_parser("search", help="Search the index.")
    search_parser.add_argument("query", help="Search query text.")
    search_parser.add_argument("--top-k", type=int, default=10, dest="top_k", help="Number of results.")
    search_parser.add_argument(
        "--mode", choices=["hybrid", "fts", "vector"], default="hybrid", help="Search mode."
    )

    subparsers.add_parser("status", help="Show the latest indexing run and configuration.")

    reset_parser = subparsers.add_parser(
        "reset",
        help="Discard allow-listed derived state and create a fresh database. "
        "Config, source files, and *.vectordrive.md files are untouched.",
    )
    reset_parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive confirmation prompt."
    )

    subparsers.add_parser(
        "mcp", help="Start the read-only MCP server over stdio (for Claude Desktop)."
    )

    subparsers.add_parser(
        "gui", help="Launch the local desktop administration console (requires the 'gui' extra)."
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse calls sys.exit() directly on parse errors (missing
        # required args, invalid --mode choice, -h/--help). main() is a
        # library-style function that must always return an int, both for
        # the console_scripts entry point and for direct calls in tests —
        # so convert that SystemExit into a returned code instead of
        # letting it propagate.
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "doctor":
        return _run_doctor_command()
    if args.command == "index":
        return run_index_command(
            args.path, rebuild=args.rebuild, verify=args.verify, register=not args.no_register
        )
    if args.command == "search":
        return run_search_command(args.query, top_k=args.top_k, mode=args.mode)
    if args.command == "status":
        return run_status_command()
    if args.command == "reset":
        return run_reset_command(yes=args.yes)
    if args.command == "mcp":
        return run_mcp_command()
    if args.command == "gui":
        return run_gui_command()

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
