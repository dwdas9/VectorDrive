"""Health checks for the GUI's Overview screen — calls the same individual
check_*() functions cli/doctor.py already defines, but never run_doctor()
itself, because run_doctor() persists the probed vector_backend into
config.toml as a side effect (cli/doctor.py:357: `cfg.vector_backend =
vector_backend; save_config(cfg)`). An Overview screen that silently
mutates config.toml every time it refreshes would surprise a user and
race with a concurrent `vectordrive doctor` — so Overview reads health
without writing anything, and only an explicit Settings action
(`redetect_vector_backend`, G9) is allowed to persist.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vectordrive.cli.doctor import (
    CheckResult,
    CheckStatus,
    check_data_dir_outside_repo,
    check_ollama,
    check_pymupdf,
    check_python_and_sqlite,
    check_python_docx,
    check_sqlite_vec_smoke_test,
    find_repo_root,
)
from vectordrive.config import Config


@dataclass(frozen=True)
class NextAction:
    """Exactly one recommendation for the Overview screen's "Next step"
    card, with enough for the GUI to render a button that goes straight
    to the right screen. `route=None` means the remedy is external to
    the app (e.g. "start Ollama") — the button just re-runs the checks.
    """

    message: str
    route: Literal["overview", "folders", "search", "claude", "settings"] | None
    button_label: str


@dataclass(frozen=True)
class HealthReport:
    checks: list[CheckResult]
    overall: CheckStatus


def run_health_checks(config: Config | None = None) -> HealthReport:
    """Run the required, fast health checks — never writes config.toml.

    Deliberately narrower than run_doctor(): skips the OCR-tier
    availability checks (rapidocr/paddleocr-vl/tesseract), which are
    about indexing capability, not "is VectorDrive healthy right now" —
    Overview shows Ollama/DB/MCP health per the approved plan's scope,
    not every doctor check.
    """
    cfg = config or Config()
    checks: list[CheckResult] = []
    checks.append(check_python_and_sqlite())
    vec_result, _vector_backend = check_sqlite_vec_smoke_test()
    checks.append(vec_result)
    checks.append(check_ollama(cfg.ollama_endpoint, cfg.embedding_model))
    checks.append(check_pymupdf())
    checks.append(check_python_docx())
    checks.append(check_data_dir_outside_repo(find_repo_root()))

    overall = CheckStatus.PASS
    for c in checks:
        if c.status is CheckStatus.FAIL and c.required:
            overall = CheckStatus.FAIL
            break
        if c.status is CheckStatus.WARN and overall is CheckStatus.PASS:
            overall = CheckStatus.WARN

    return HealthReport(checks=checks, overall=overall)


def compute_next_action(
    checks: list[CheckResult],
    *,
    db_available: bool = True,
    folder_count: int = 0,
    latest_run_exists: bool = False,
    failed_file_count: int = 0,
) -> NextAction | None:
    """Priority ladder → exactly one recommendation, or None ("Nothing to
    do — everything is healthy"). Each rung is checked in order; the
    first one that applies wins, matching the wireframe's "Ollama down →
    DB missing/corrupt → no folders added → never indexed → failures
    present → all healthy" order.

    MCP-unverified is deferred: services.claude_desktop /
    services.mcp_probe don't exist until G8, so that rung isn't in the
    ladder yet — it slots in between "failures present" and "all
    healthy" once G8 lands, not invented here ahead of the code that
    would make it meaningful.
    """
    ollama_check = next((c for c in checks if c.name == "ollama_reachable"), None)
    if ollama_check is not None and "Could not reach Ollama" in ollama_check.message:
        return NextAction(
            message="Ollama is unreachable — vector and hybrid search need it running.",
            route=None,
            button_label="Recheck",
        )

    if not db_available:
        return NextAction(
            message="The database needs attention before VectorDrive can be used normally.",
            route="settings",
            button_label="Open Settings",
        )

    if folder_count == 0:
        return NextAction(
            message="Add a folder to get started.",
            route="folders",
            button_label="Add a folder",
        )

    if not latest_run_exists:
        return NextAction(
            message="No indexing run yet.",
            route="folders",
            button_label="Index now",
        )

    if failed_file_count > 0:
        plural = "s" if failed_file_count != 1 else ""
        return NextAction(
            message=f"{failed_file_count} file{plural} failed in the last run.",
            route="folders",
            button_label="Review failures",
        )

    return None
