"""Ein Fortschrittsbegriff für alle langen Arbeiten (Spec §11).

Drei Stellen brauchen ihn heute schon: das Lautheits-Nachmessen, der Update-Download
und später die Voicebox-Generierung. Ohne gemeinsamen Begriff entstehen drei Sonderwege
und drei Stellen in der Oberfläche.

Lebt im flüchtigen Zustandsteil: Jobs kommen und gehen, die Bibliothek wächst. Die Liste
ist begrenzt, damit sie über einen langen Spielabend nicht selbst zum Leck wird.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from .protocol import JobChanged

log = logging.getLogger(__name__)

MAX_JOBS = 20  # running jobs are never dropped; the oldest finished one goes first
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"


class JobsService:
    def __init__(self, core):
        self._core = core
        self._jobs: list[dict] = []
        self._cancel_hooks: dict[str, Callable[[], None]] = {}
        core.add_state("jobs", self.snapshot)
        core.on_shutdown(self._on_shutdown)

    # ---- reading ----

    def snapshot(self) -> dict:
        """Pure read: the list is built on the core thread and only handed out as a copy."""
        return {"jobs": [dict(job) for job in self._jobs]}

    def get(self, job_id: str) -> dict | None:
        return next((job for job in self._jobs if job["id"] == job_id), None)

    # ---- writing ----

    def start(self, kind: str, label: str, *, on_cancel: Callable[[], None] | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": kind, "label": label, "state": RUNNING,
               "fraction": None, "text": "", "ok": None}
        self._jobs.append(job)
        if on_cancel is not None:
            self._cancel_hooks[job_id] = on_cancel
        self._prune_overflow()
        self._publish(job)
        return job_id

    def progress(self, job_id: str, *, fraction: float | None = None,
                 text: str | None = None) -> None:
        job = self.get(job_id)
        if job is None or job["state"] != RUNNING:
            return
        if fraction is not None:
            job["fraction"] = max(0.0, min(1.0, float(fraction)))
        if text is not None:
            job["text"] = text
        self._publish(job)

    def finish(self, job_id: str, ok: bool = True, text: str | None = None) -> None:
        job = self.get(job_id)
        if job is None or job["state"] != RUNNING:
            return
        job["state"] = DONE
        job["ok"] = bool(ok)
        if ok:
            job["fraction"] = 1.0
        if text is not None:
            job["text"] = text
        self._cancel_hooks.pop(job_id, None)
        self._prune_overflow()
        self._publish(job)

    def cancel(self, job_id: str) -> bool:
        """True when this call cancelled it; the hook is what actually stops the work."""
        job = self.get(job_id)
        if job is None or job["state"] != RUNNING:
            return False
        job["state"] = CANCELLED
        job["ok"] = False
        hook = self._cancel_hooks.pop(job_id, None)
        self._publish(job)
        if hook is not None:
            try:
                hook()
            except Exception:  # a broken hook must not break the cancel itself
                log.exception("cancel hook for job %s failed", job_id)
        return True

    def prune(self) -> None:
        """Drop everything that is not running (used after a long idle stretch)."""
        self._jobs = [job for job in self._jobs if job["state"] == RUNNING]
        live = {job["id"] for job in self._jobs}
        self._cancel_hooks = {jid: hook for jid, hook in self._cancel_hooks.items() if jid in live}

    # ---- internals ----

    def _publish(self, job: dict) -> None:
        self._core.state_changed()
        self._core.emit(JobChanged(dict(job)))

    def _prune_overflow(self) -> None:
        """Running jobs are never dropped; the oldest finished one goes first, so a long
        session cannot grow this list without bound."""
        if len(self._jobs) <= MAX_JOBS:
            return
        for index, job in enumerate(self._jobs):
            if job["state"] != RUNNING:
                self._jobs.pop(index)
                self._cancel_hooks.pop(job["id"], None)
                return

    def _on_shutdown(self) -> None:
        for job in list(self._jobs):
            if job["state"] == RUNNING:
                self.cancel(job["id"])
