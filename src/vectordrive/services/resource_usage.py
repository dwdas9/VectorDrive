"""The GUI job-status card's "Ollama model memory" signal.

macOS has no free, no-sudo API for real-time GPU utilization% (the only
real option, `powermetrics`, requires sudo — a poor fit for an app that
never asks for elevated privileges). What's genuinely available: Ollama's
own `/api/ps` endpoint reports the currently-loaded embedding model's
VRAM footprint (`size_vram`, bytes). This is a memory footprint, not a
load percentage — the GUI deliberately labels it "Ollama model memory",
never "GPU usage", to avoid implying a number this isn't.

Reuses the existing httpx dependency; adds no new one.
"""
from __future__ import annotations

import httpx


def get_ollama_model_memory(endpoint: str, timeout: float = 1.0) -> int | None:
    """Bytes of VRAM the currently-loaded Ollama model(s) occupy, or None
    if Ollama isn't reachable or nothing is loaded. None, not 0 — 0 would
    misleadingly read as "using no GPU" rather than "no data available".
    Never raises: a resource-card display glitch must never surface as
    an app error. Short timeout, polled only while a job is active (see
    gui/web/job-status.js) — never blocks the GUI bridge thread for long.
    """
    try:
        response = httpx.get(f"{endpoint}/api/ps", timeout=timeout)
        response.raise_for_status()
        models = response.json().get("models") or []
    except Exception:  # noqa: BLE001 - best-effort signal, never fatal
        return None
    if not models:
        return None
    total = sum(m.get("size_vram", 0) for m in models)
    return total or None
