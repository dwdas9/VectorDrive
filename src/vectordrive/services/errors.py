"""Shared error taxonomy for the service layer.

CLI exit codes, MCP ToolError messages, and GUI error envelopes (G3+) all
derive from one place: every VectorDriveError carries a stable machine
`code` (for programmatic handling / tests) and an optional `hint` (one
actionable sentence, suitable for direct display in the GUI).
"""
from __future__ import annotations


class VectorDriveError(Exception):
    """Base class for all service-layer errors.

    Args:
        message: human-readable description (also `str(exc)`).
        code: stable machine-readable identifier, e.g. "db_corrupt".
        hint: one actionable sentence for a UI to show directly, or None.
    """

    def __init__(self, message: str, *, code: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class DbUnavailable(VectorDriveError):
    """The database is missing, corrupt, or otherwise not readable."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="db_unavailable", hint=hint)


class IndexLocked(VectorDriveError):
    """Another process already holds the index write lock."""

    def __init__(self, message: str, *, holder: "object | None" = None) -> None:
        super().__init__(
            message,
            code="index_locked",
            hint="Wait for the other indexing run to finish, or stop it, then try again.",
        )
        self.holder = holder


class IndexIncomplete(VectorDriveError):
    """The latest Phase 2 run did not publish a complete searchable index."""

    def __init__(self, status: str) -> None:
        super().__init__(
            f"The latest indexing run is {status}; search is unavailable until it completes.",
            code="index_incomplete",
            hint="Resume or restart indexing and let it finish before searching.",
        )
        self.status = status


class InvalidFolder(VectorDriveError):
    """A folder path fails validation (missing, not a directory, inside
    VECTORDRIVE_HOME, nested with an already-indexed folder, etc.).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="invalid_folder", hint=hint)


class RootUnavailable(VectorDriveError):
    """The registered root folder does not currently exist on disk."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="root_missing", hint=hint)


class OllamaUnavailable(VectorDriveError):
    """A required local Ollama service/model is unavailable."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="ollama_unavailable", hint=hint)


class ClaudeConfigError(VectorDriveError):
    """Claude Desktop's config file could not be read, parsed, or safely
    written (malformed JSON, permission denied, changed since preview).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="claude_config_error", hint=hint)
