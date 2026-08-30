"""`vectordrive gui`: launches the local desktop administration console.
Mirrors cli/mcp_cmd.py's minimalism — all real logic lives in gui.app.
"""
from __future__ import annotations

from vectordrive.gui.app import run as _run_gui


def run_gui_command() -> int:
    return _run_gui()
