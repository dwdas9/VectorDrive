"""A dedicated single-generation worker for local chat.

Chat is independent of the mutating indexing/maintenance queue: a long
index run must not delay a response, and a slow model must not block a
database backup.  Events still share the GUI's EventBus so the existing
polling bridge remains the only transport into JavaScript.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal

from vectordrive.gui.events import EventBus
from vectordrive.services.errors import VectorDriveError

ChatFn = Callable[[Callable[[str, dict], None], Callable[[], bool]], dict | None]
ChatStatus = Literal["running", "done", "failed", "cancelled"]
MAX_CHAT_RECORDS = 20


@dataclass
class ChatRecord:
    chat_id: str
    status: ChatStatus = "running"
    content: str = ""
    sources: list[dict] = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "status": self.status,
            "content": self.content,
            "sources": [dict(source) for source in self.sources],
            "cancelled": self.cancelled,
            "error": self.error,
            "error_code": self.error_code,
        }


class ChatRunner:
    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or EventBus()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vectordrive-chat")
        self._lock = threading.Lock()
        self._active_chat_id: str | None = None
        self._cancel_event: threading.Event | None = None
        self._cancel_hook: Callable[[], None] | None = None
        # Session-only recovery records let the webview reconcile a response
        # even if its event cursor falls behind the bounded shared ring.  The
        # small bound also keeps unusually large model answers from piling up.
        self._records: dict[str, ChatRecord] = {}

    def start(self, fn: ChatFn) -> str:
        chat_id = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            if self._active_chat_id is not None:
                raise VectorDriveError(
                    "A chat response is already being generated.",
                    code="chat_busy",
                    hint="Stop the current response before sending another message.",
                )
            self._active_chat_id = chat_id
            self._cancel_event = cancel_event
            self._cancel_hook = None
            while len(self._records) >= MAX_CHAT_RECORDS:
                self._records.pop(next(iter(self._records)))
            self._records[chat_id] = ChatRecord(chat_id=chat_id)

        def emit(kind: str, payload: dict) -> None:
            with self._lock:
                record = self._records.get(chat_id)
                if record is not None:
                    if kind == "chat_sources":
                        sources = payload.get("sources")
                        if isinstance(sources, list):
                            record.sources = [dict(source) for source in sources if isinstance(source, dict)]
                    elif kind == "chat_delta":
                        text = payload.get("text")
                        if isinstance(text, str):
                            record.content += text
                    elif kind == "chat_done":
                        content = payload.get("content")
                        if isinstance(content, str):
                            record.content = content
                        record.cancelled = bool(payload.get("cancelled"))
                        record.status = "cancelled" if record.cancelled else "done"
                    elif kind == "chat_failed":
                        record.status = "failed"
                        record.error = str(payload.get("error") or "Chat failed.")
                        record.error_code = str(payload.get("error_code") or "internal")
            self.bus.emit(kind, {"chat_id": chat_id, **payload}, job_id=chat_id)

        def run() -> None:
            emit("chat_started", {})
            try:
                result = fn(emit, cancel_event.is_set)
            except Exception as exc:  # noqa: BLE001 - worker failures cross the bridge as events
                if cancel_event.is_set():
                    emit("chat_done", {"cancelled": True})
                else:
                    code = exc.code if isinstance(exc, VectorDriveError) else "internal"
                    emit("chat_failed", {"error": str(exc), "error_code": code})
            else:
                final_payload = result if isinstance(result, dict) else {}
                emit("chat_done", {**final_payload, "cancelled": cancel_event.is_set()})
            finally:
                with self._lock:
                    if self._active_chat_id == chat_id:
                        self._active_chat_id = None
                        self._cancel_event = None
                        self._cancel_hook = None

        self._executor.submit(run)
        return chat_id

    def active_chat_id(self) -> str | None:
        with self._lock:
            return self._active_chat_id

    def get(self, chat_id: str) -> ChatRecord | None:
        with self._lock:
            record = self._records.get(chat_id)
            if record is None:
                return None
            # Return an independent snapshot so the bridge never observes a
            # worker mutation halfway through serialization.
            return ChatRecord(
                chat_id=record.chat_id,
                status=record.status,
                content=record.content,
                sources=[dict(source) for source in record.sources],
                cancelled=record.cancelled,
                error=record.error,
                error_code=record.error_code,
            )

    def cancel(self, chat_id: str) -> bool:
        with self._lock:
            if self._active_chat_id != chat_id or self._cancel_event is None:
                return False
            self._cancel_event.set()
            hook = self._cancel_hook
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - cancellation remains best-effort
                pass
        return True

    def cancel_active(self) -> bool:
        with self._lock:
            if self._active_chat_id is None or self._cancel_event is None:
                return False
            self._cancel_event.set()
            hook = self._cancel_hook
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - cancellation remains best-effort
                pass
        return True

    def set_cancel_hook(self, hook: Callable[[], None]) -> None:
        """Register the active transport's close function.

        If cancellation won the race before the worker finished creating its
        HTTP client, invoke the hook immediately so a blocked first-token read
        is still interrupted.
        """
        with self._lock:
            if self._active_chat_id is None or self._cancel_event is None:
                return
            self._cancel_hook = hook
            already_cancelled = self._cancel_event.is_set()
        if already_cancelled:
            try:
                hook()
            except Exception:  # noqa: BLE001 - cancellation remains best-effort
                pass

    def wait_until_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.active_chat_id() is not None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def shutdown(self) -> None:
        self.cancel_active()
        self._executor.shutdown(wait=False, cancel_futures=True)
