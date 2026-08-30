"""Content hashing — the skip-unchanged and cache key foundation for the
whole pipeline: file -> extracted_doc -> ocr_result -> chunks -> embeddings.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_DEFAULT_CHUNK_SIZE = 1 << 20  # 1 MiB


def sha256_file(path: Path, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> str:
    """Stream-hash a file's contents (sha256, hex digest)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Hash in-memory bytes (sha256, hex digest).

    Used to key the OCR cache by page/image identity: for a standalone
    JPG/PNG this is the file's own bytes; for a rendered PDF page it's the
    rendered image bytes (so a DPI change correctly busts the cache).
    """
    return hashlib.sha256(data).hexdigest()
