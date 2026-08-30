"""The GUI's single background worker: exactly one long-running mutating
operation at a time (index / backup / restore / vacuum / purge), matching
services.locking's single-index-writer guarantee. Search and MCP probes
use their own short-lived executor (added when G7/G8 need it) so a
running index doesn't block those screens — but those run against a
read-only connection, so they can never race a writer anyway.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from vectordrive.gui.events import EventBus

JobStatus = Literal["pending", "running", "done", "failed", "cancelled", "paused"]

# A job body receives (emit, is_cancelled): emit(kind, payload) pushes an
# event tagged with this job's id; is_cancelled() is the should_cancel
# predicate a long-running service call (e.g. services.index_service.
# run_index's should_cancel param) should poll between units of work.
JobFn = Callable[[Callable[[str, dict], None], Callable[[], bool]], Any]


def _safe_for_bridge(value: Any) -> Any:
    """Best-effort: drop a job result down to something JSON-shaped for
    the eventual bridge across to JS, without importing dataclasses.asdict
    machinery here (services already return plain dicts/dataclasses; we
    only need a couple of common shapes, not a general serializer).
    """
    if hasattr(value, "__dict__"):
        return {k: _safe_for_bridge(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: _safe_for_bridge(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_for_bridge(v) for v in value]
    return value


@dataclass
class JobRecord:
    job_id: str
    status: JobStatus = "pending"
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    pausable: bool = False
    _cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "result": _safe_for_bridge(self.result) if self.status == "done" else None,
            "error": self.error,
            "error_code": self.error_code,
        }


class JobRunner:
    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or EventBus()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vectordrive-job")
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        # Tracks the one job actually executing right now (this runner has
        # a single worker — see the module docstring). Used by gui/app.py's
        # shutdown path to know whether a "Cancelling…" wait is needed at
        # all, without scanning `_jobs` for a "running" status under lock.
        self._active_job_id: str | None = None

    def submit(self, fn: JobFn, *, pausable: bool = False) -> str:
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(job_id=job_id, pausable=pausable)
        with self._lock:
            self._jobs[job_id] = record

        def _emit(kind: str, payload: dict) -> None:
            self.bus.emit(kind, payload, job_id=job_id)

        def _run() -> None:
            with self._lock:
                self._active_job_id = job_id
                record.status = "running"
            _emit("started", {})
            try:
                result = fn(_emit, record._cancel_event.is_set)
            except Exception as exc:  # noqa: BLE001 — a job failure must never crash the worker thread
                from vectordrive.services.errors import VectorDriveError

                with self._lock:
                    record.status = "failed"
                    record.error = str(exc)
                    record.error_code = exc.code if isinstance(exc, VectorDriveError) else "internal"
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                _emit("failed", {"error": record.error, "error_code": record.error_code})
                return

            # The request flag cannot tell us whether a service stopped at a
            # safe checkpoint: it may have completed just as Pause was
            # clicked.  Pausable services own that truth and return an
            # explicit `cancelled` outcome only after durable state is safe.
            if isinstance(result, dict):
                service_cancelled = result.get("cancelled") is True
            else:
                service_cancelled = getattr(result, "cancelled", False) is True

            with self._lock:
                # Keep cancellation as a transport outcome, not as part of
                # the paused job's business result (callers historically
                # inspect the completed-file counts directly).
                record.result = (
                    {key: value for key, value in result.items() if key != "cancelled"}
                    if isinstance(result, dict) and service_cancelled else result
                )
                if record.pausable:
                    record.status = "paused" if service_cancelled else "done"
                elif record._cancel_event.is_set():
                    record.status = "cancelled"
                else:
                    record.status = "done"
                final_status = record.status
                if self._active_job_id == job_id:
                    self._active_job_id = None
            _emit(final_status, {"result": _safe_for_bridge(result)})

        self._executor.submit(_run)
        return job_id

    def active_job_id(self) -> str | None:
        """The job currently executing on the single worker, if any."""
        with self._lock:
            return self._active_job_id

    def active_pausable_job_id(self) -> str | None:
        """Return the active indexing/markdown job, if one is running.

        The GUI uses this to avoid showing a stale durable-run resume card
        alongside the authoritative live job card.  Short health checks and
        other non-pausable jobs must not hide resumable work.
        """
        with self._lock:
            if self._active_job_id is None:
                return None
            record = self._jobs.get(self._active_job_id)
            if record is None or not record.pausable:
                return None
            return self._active_job_id

    def has_pausable_work(self) -> bool:
        """Whether a pausable job is running or queued on the worker."""
        with self._lock:
            return any(
                record.pausable and record.status in ("pending", "running")
                for record in self._jobs.values()
            )

    def has_active_work(self) -> bool:
        """Whether ANY job — pausable or not — is pending or running.

        Broader than active_job_id() (only the job actually executing on
        the single worker) and has_pausable_work() (pausable jobs only):
        this also covers a job that has been submit()'d but has not yet
        started running, closing the pre-start window a destructive
        maintenance action must not slip through.
        """
        with self._lock:
            return any(
                record.status in ("pending", "running")
                for record in self._jobs.values()
            )

    def wait_until_idle(self, timeout: float) -> bool:
        """Block up to `timeout` seconds for the active job (if any) to
        finish. Returns True once idle, False if still active when the
        deadline passes — the caller (gui/app.py's shutdown path) treats
        a False here as "gave it a reasonable chance, moving on regardless"
        rather than as an error.
        """
        deadline = time.monotonic() + timeout
        while self.active_job_id() is not None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return False
            record._cancel_event.set()
            return True

    def pause(self, job_id: str) -> bool:
        """Request a cooperative pause at the worker's next safe boundary."""
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or not record.pausable or record.status not in ("pending", "running"):
                return False
            record._cancel_event.set()
            return True

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
