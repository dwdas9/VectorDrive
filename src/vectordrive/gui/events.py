"""The GUI's event transport: a thread-safe, monotonically-sequenced ring
buffer. JS polls `api.poll_events(cursor)` every 250ms rather than
receiving a push from Python.

Why polling: docs/gui.md's G0 findings proved `evaluate_js()` from a
worker thread *does* work reliably on this backend, but polling is kept
as the primary transport anyway — it's trivially correct, testable
without a live window (this module has zero pywebview dependency), and
resilient to a dropped or slow poll (the cursor just catches up). Push
remains an available future latency optimization, not a requirement.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_MAXLEN = 2000


@dataclass(frozen=True)
class Event:
    seq: int
    job_id: str | None
    kind: str
    ts: str
    payload: dict[str, Any]

    def to_dict(self) -> dict:
        return {"seq": self.seq, "job_id": self.job_id, "kind": self.kind, "ts": self.ts, "payload": self.payload}


class EventBus:
    """A ring buffer of the last `maxlen` events. Sequence numbers keep
    incrementing even past eviction, so a caller whose cursor falls
    behind the buffer's oldest retained event can detect the gap (its
    next `since()` call simply returns everything currently retained,
    starting after its cursor) rather than silently missing history.
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._buffer: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def emit(self, kind: str, payload: dict, *, job_id: str | None = None) -> Event:
        event = Event(
            seq=next(self._counter),
            job_id=job_id,
            kind=kind,
            ts=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        with self._lock:
            self._buffer.append(event)
        return event

    def since(self, cursor: int) -> list[Event]:
        """Every retained event with seq > cursor, oldest first."""
        with self._lock:
            return [e for e in self._buffer if e.seq > cursor]

    def latest_seq(self) -> int:
        with self._lock:
            return self._buffer[-1].seq if self._buffer else 0
