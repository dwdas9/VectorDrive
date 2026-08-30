"""VectorDrive configuration and external data-directory resolution.

All databases, caches and config live outside the repository. This module
is the single place that decides *where* — everything else asks it, rather
than hard-coding a path.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

from vectordrive.pipeline.embed.base import DEFAULT_EMBED_BATCH_SIZE

APP_DIR_NAME = "VectorDrive"
CONFIG_FILENAME = "config.toml"
DB_FILENAME = "vectordrive.db"
ENV_VAR = "VECTORDRIVE_HOME"

DEFAULT_EMBEDDING_MODEL = "qwen3-embedding:8b"
DEFAULT_EMBEDDING_DIMS = 1024
DEFAULT_EMBEDDING_PROVIDER = "ollama"
DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
# Phase 1 "Create Markdown" primary OCR engine (see pipeline/ocr/qwen_vl_engine.py):
# a local vision-language model served by Ollama at DEFAULT_OLLAMA_ENDPOINT.
DEFAULT_PHASE1_MARKDOWN_VISION_MODEL = "qwen3-vl:8b-instruct"

# GUI OCR choices are intentionally narrow. Both tags are official Qwen3-VL
# instruction models accepted by the existing QwenVlOcrEngine; the GUI still
# shows only members of this list that are already installed in local Ollama.
# It never pulls a model merely because it appears here.
SUPPORTED_PHASE1_VISION_MODELS: tuple[str, ...] = (
    "qwen3-vl:4b-instruct",
    "qwen3-vl:8b-instruct",
)

def _default_data_home() -> Path:
    """Platform default for the data directory, before any env override."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    # Reasonable fallback for non-macOS dev/CI environments.
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def get_data_home() -> Path:
    """Resolve (and ensure the existence of) VectorDrive's external data dir.

    Honors the ``VECTORDRIVE_HOME`` environment variable if set; otherwise
    defaults to ``~/Library/Application Support/VectorDrive`` on macOS.
    Creates the directory and its ``cache``/``logs`` subdirectories if they
    don't already exist.
    """
    override = os.environ.get(ENV_VAR)
    home = Path(override).expanduser() if override else _default_data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "cache").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return home


def get_config_path(data_home: Path | None = None) -> Path:
    """Path to config.toml under the given (or resolved) data home."""
    return (data_home or get_data_home()) / CONFIG_FILENAME


def get_db_path(data_home: Path | None = None) -> Path:
    """Path to the shared vectordrive.db under the given (or resolved) data
    home — the single source of truth the CLI's index/search/status
    commands use to find the same database (rather than each hard-coding
    "vectordrive.db" separately).
    """
    return (data_home or get_data_home()) / DB_FILENAME


@dataclass
class OcrTierConfig:
    """Feature flags for the optional OCR tiers.

    Tier 1 (PyMuPDF native text) and Tier 2 (RapidOCR) are always available
    and unconditional. Tier 3 (PaddleOCR-VL) defaults on: a real-corpus
    benchmark (2026-08-23, this M3 Max) showed running it as the *primary*
    engine for every OCR page costs ~90x RapidOCR's per-page latency with
    no accuracy gain, but the existing tiered-escalation design (RapidOCR
    first, PaddleOCR-VL only when RapidOCR's confidence is low) pays that
    cost only on the pages that actually need it -- cheap enough to default
    on. A failed/missing PaddleOCR-VL install degrades gracefully to
    RapidOCR's own result (run_ocr_with_cache records the failure, never
    raises). Tier 4 (Tesseract, last resort) stays opt-in.
    """

    tier3_paddleocr_vl_enabled: bool = True
    tier4_tesseract_enabled: bool = False
    # Phase 1 "Create Markdown" primary OCR engine (QwenVlOcrEngine, tier2 of
    # ExtractionEngines): a local vision-language model served by Ollama, used
    # for every needs_ocr page during Markdown sidecar generation. Orthogonal
    # to tier3/4 above, which stay legacy indexing's engine stack
    # (build_index_engines) and are untouched by this field.
    phase1_markdown_vision_model: str = DEFAULT_PHASE1_MARKDOWN_VISION_MODEL


@dataclass
class StructureConfig:
    """Feature flag for PP-StructureV3 layout analysis (Stage 9). Off by
    default: structure analysis is a separate, optional pass over already-
    extracted pages, not a new OCR tier — enabling it never changes flat
    chunking behavior for a file until it (or a Full Rebuild) is
    reprocessed.
    """

    enabled: bool = False
    analyze_native_pages: bool = True


@dataclass(frozen=True)
class ExcludedFile:
    """A source file the user removed from VectorDrive (Review panel's
    "Remove from VectorDrive"). Recorded here — in durable, hand-editable
    config.toml, not the disposable derived database — so the exclusion
    survives a full rebuild / `vectordrive reset` and keeps future
    Markdown generation and indexing from re-adding the file.

    Identity is root-relative first (`root_path` + `relative_path`, stable
    across a folder relink because relink_folder() rewrites these), with
    `abs_path` kept only as a normalized fallback for files that were
    never attributed to a registered root.
    """

    root_path: str
    relative_path: str
    abs_path: str


@dataclass
class Config:
    """VectorDrive's persisted configuration (config.toml)."""

    indexed_folders: list[str] = field(default_factory=list)
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dims: int = DEFAULT_EMBEDDING_DIMS
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    ollama_endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    # Set by `doctor` once it runs the sqlite-vec Apple Silicon smoke test:
    # "sqlite_vec" | "numpy" | None (not yet determined).
    vector_backend: str | None = None
    # M10.5 requirement 3: how many missing document embeddings are sent
    # to the provider in one request, rather than one request per chunk.
    embedding_batch_size: int = DEFAULT_EMBED_BATCH_SIZE
    ocr: OcrTierConfig = field(default_factory=OcrTierConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    # Files the user removed from VectorDrive (Review panel). Durable so
    # the skip survives a rebuild; empty for every pre-existing config.
    excluded_files: list[ExcludedFile] = field(default_factory=list)

    def to_dict(self) -> dict:
        # TOML has no null type: omit unset fields (e.g. vector_backend
        # before `doctor` has run) rather than writing a literal null.
        data = {k: v for k, v in asdict(self).items() if v is not None}
        # Keep config.toml clean: only write the exclusions table when a
        # file has actually been removed (the common case is an empty list).
        if not data.get("excluded_files"):
            data.pop("excluded_files", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        ocr_data = data.get("ocr", {})
        structure_data = data.get("structure", {})
        excluded_raw = data.get("excluded_files", []) or []
        excluded_files: list[ExcludedFile] = []
        for entry in excluded_raw:
            if not isinstance(entry, dict):
                continue
            try:
                excluded_files.append(
                    ExcludedFile(
                        root_path=str(entry["root_path"]),
                        relative_path=str(entry["relative_path"]),
                        abs_path=str(entry["abs_path"]),
                    )
                )
            except (KeyError, TypeError):
                # A hand-edited/partial entry never crashes config load.
                continue
        return cls(
            indexed_folders=list(data.get("indexed_folders", [])),
            embedding_model=data.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
            embedding_dims=data.get("embedding_dims", DEFAULT_EMBEDDING_DIMS),
            embedding_provider=data.get("embedding_provider", DEFAULT_EMBEDDING_PROVIDER),
            ollama_endpoint=data.get("ollama_endpoint", DEFAULT_OLLAMA_ENDPOINT),
            vector_backend=data.get("vector_backend"),
            embedding_batch_size=data.get("embedding_batch_size", DEFAULT_EMBED_BATCH_SIZE),
            ocr=OcrTierConfig(
                tier3_paddleocr_vl_enabled=ocr_data.get("tier3_paddleocr_vl_enabled", True),
                tier4_tesseract_enabled=ocr_data.get("tier4_tesseract_enabled", False),
                phase1_markdown_vision_model=ocr_data.get(
                    "phase1_markdown_vision_model", DEFAULT_PHASE1_MARKDOWN_VISION_MODEL
                ),
            ),
            structure=StructureConfig(
                enabled=structure_data.get("enabled", False),
                analyze_native_pages=structure_data.get("analyze_native_pages", True),
            ),
            excluded_files=excluded_files,
        )


def load_config(path: Path | None = None) -> Config:
    """Load config.toml, creating a default one on disk if missing."""
    config_path = path or get_config_path()
    if not config_path.exists():
        cfg = Config()
        save_config(cfg, config_path)
        return cfg
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return Config.from_dict(data)


def save_config(config: Config, path: Path | None = None) -> None:
    """Persist `config` as TOML, creating parent directories if needed.

    v0.2 G1: writes atomically (tmp file in the same directory, fsync,
    then os.replace) instead of truncating config.toml in place. Before
    this, a crash mid-write — or the GUI and `vectordrive doctor` racing
    to save at the same moment — could leave a truncated, unparseable
    config.toml; a reader can now never observe a partial write.
    """
    from vectordrive.services.atomic_io import atomic_write_bytes

    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(config_path, tomli_w.dumps(config.to_dict()).encode("utf-8"))


def update_config(mutator, path: Path | None = None) -> Config:
    """Read-modify-write config.toml: load the current Config, call
    `mutator(config) -> Config | None` (a None return means "mutated in
    place"), and atomically save the result.

    This is the safe way for two independent writers (e.g. the GUI adding
    a folder while `vectordrive index` auto-registers another) to update
    config.toml without a lost-update race: each update_config() call
    re-reads the current file immediately before writing, so it always
    starts from the latest state on disk rather than a possibly-stale
    in-memory Config held since an earlier read.
    """
    config_path = path or get_config_path()
    current = load_config(config_path)
    result = mutator(current)
    new_config = result if result is not None else current
    save_config(new_config, config_path)
    return new_config


def path_is_inside(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` itself, or nested anywhere inside it."""
    child = child.resolve()
    parent = parent.resolve()
    return child == parent or parent in child.parents
