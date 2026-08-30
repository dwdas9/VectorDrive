"""gui_state.json — small GUI-only state (window geometry, last route,
last_mcp_verified_at, last_claude_restart_confirmed_at, backfill-prompt
dismissal) kept deliberately separate from config.toml, which stays a
small, meaningful, hand-editable file (v0.2 Decision 3 / Migration
Impact section). Losing this file costs nothing but a reset window
position and re-asking a couple of already-answered prompts — it is
always safe to delete.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from vectordrive.services.atomic_io import atomic_write_text

STATE_FILENAME = "gui_state.json"


@dataclass
class GuiState:
    window_width: int = 1120
    window_height: int = 760
    last_route: str = "overview"
    last_mcp_verified_at: str | None = None
    last_claude_restart_confirmed_at: str | None = None
    adopt_folders_prompt_dismissed: bool = False
    chat_model: str | None = None
    # Review panel: whether the right-side inspector is collapsed. Purely
    # cosmetic — always safe to lose (see module docstring).
    review_inspector_collapsed: bool = False
    # Folders panel: same cosmetic preference for its details inspector.
    folders_inspector_collapsed: bool = False
    # Folder processing mode. True keeps inspectable Markdown sidecars;
    # False takes the direct OCR-to-index route without writing sidecars.
    output_markdown_files: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GuiState":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)


def state_path(data_home: Path) -> Path:
    return Path(data_home) / STATE_FILENAME


def load_state(data_home: Path) -> GuiState:
    path = state_path(data_home)
    if not path.exists():
        return GuiState()
    try:
        return GuiState.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError):
        # Corrupt/unreadable gui_state.json is never fatal — it holds
        # nothing essential. Fall back to defaults rather than crashing
        # the whole GUI over a cosmetic preferences file.
        return GuiState()


def save_state(data_home: Path, state: GuiState) -> None:
    atomic_write_text(state_path(data_home), json.dumps(state.to_dict(), indent=2))
