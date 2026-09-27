"""Wo Arbeit laeuft.

SerialExecutor - ein benannter Thread mit Warteschlange und Timern. Kern-Thread und
Geraete-Thread sind je einer davon: alles darauf laeuft nacheinander, also ohne Locks
untereinander.
InlineExecutor - fuehrt submit() sofort im aufrufenden Thread aus; Timer laufen erst,
wenn ein Test advance() ruft. Achtung: dadurch laeuft verschachtelt, was im echten
Betrieb nacheinander liefe - Dienste setzen ihren Zustand deshalb, bevor sie Arbeit
weitergeben.
WorkerPool - Daemon-Threads fuer Datei-, ffmpeg- und Rechenarbeit (nie PortAudio).
Daemon, damit ein haengendes ffmpeg das Beenden der App nicht aufhaelt.
CancellablePool - eigener Ausgang fuer Netzaufrufe (Voicebox, Spotify, Updates) mit
kooperativem Abbruch. Getrennt vom WorkerPool, weil eine Anfrage von 300 s keine
Sound-Dekodierung aufhalten darf und umgekehrt. Siehe die Klasse unten.

Fehlerhuelle: eine Ausnahme in einer Aufgabe geht an on_error, der Thread lebt weiter.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

ErrorHandler = Callable[[Exception], None]

# Wakes an idle net thread without running anything (see CancellablePool.cancel_all).
WAKE = object()


def _log_error(exc: Exception) -> None:
    log.error("task failed", exc_info=exc)


def _run_guarded(fn: Callable, args: tuple, on_error: ErrorHandler) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - the thread must survive any task
        try:
            on_error(exc)
        except Exception:
            log.exception("error handler failed")


class Timer:
    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class SerialExecutor:
    def __init__(self, name: str, on_error: ErrorHandler = _log_error,
                 on_start: Callable[[], None] | None = None):
        self.name = name
        self._on_error = on_error
        # Runs first on the thread itself, e.g. COM setup the thread's work depends on.
        self._on_start = on_start
        self._queue: queue.Queue = queue.Queue()
        self._timers: list[tuple[float, int, Timer, Callable, tuple]] = []  # own thread only
        self._seq = itertools.count()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def is_current(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, fn: Callable, *args: Any) -> None:
        if self._stopping:
            log.debug("%s: ignoring work submitted after stop", self.name)
            return
        self._queue.put(("run", fn, args, None))

    def call_later(self, delay: float, fn: Callable, *args: Any) -> Timer:
        timer = Timer()
        if self._stopping:
            timer.cancel()
            return timer
        self._queue.put(("later", fn, args, (time.monotonic() + max(0.0, delay), timer)))
        return timer

    def stop(self, timeout: float = 5.0) -> bool:
        """Run what is already queued, drop pending timers, end the thread. True when
        it ended within `timeout`. Called on its own thread it cannot wait and
        returns False."""
        if not self._stopping:
            self._stopping = True
            self._queue.put(("stop", None, (), None))
        if self.is_current():
            return False
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        if self._on_start is not None:
            _run_guarded(self._on_start, (), self._on_error)
        while True:
            wait = None
            if self._timers:
                wait = max(0.0, self._timers[0][0] - time.monotonic())
            try:
                kind, fn, args, extra = self._queue.get(timeout=wait)
            except queue.Empty:
                self._run_due_timers()
                continue
            if kind == "stop":
                return
            if kind == "later":
                due, timer = extra
                heapq.heappush(self._timers, (due, next(self._seq), timer, fn, args))
            else:
                _run_guarded(fn, args, self._on_error)
            self._run_due_timers()

    def _run_due_timers(self) -> None:
        now = time.monotonic()
        while self._timers and self._timers[0][0] <= now:
            _due, _seq, timer, fn, args = heapq.heappop(self._timers)
            if not timer.cancelled:
                _run_guarded(fn, args, self._on_error)


class InlineExecutor:
    """Test double with the same interface: work runs at once, timers on advance()."""

    alive = True

    def __init__(self, on_error: ErrorHandler = _log_error):
        self._on_error = on_error
        self.now = 0.0
        self._timers: list[tuple[float, int, Timer, Callable, tuple]] = []
        self._seq = itertools.count()

    def is_current(self) -> bool:
        return True

    def submit(self, fn: Callable, *args: Any) -> None:
        _run_guarded(fn, args, self._on_error)

    def call_later(self, delay: float, fn: Callable, *args: Any) -> Timer:
        timer = Timer()
        heapq.heappush(self._timers, (self.now + max(0.0, delay), next(self._seq), timer, fn, args))
        return timer

    def advance(self, seconds: float) -> None:
        """Move the test clock and run every timer due by then, in due order - timers
        scheduled by those timers too, when they fall due within the window."""
        end = self.now + seconds
        while self._timers and self._timers[0][0] <= end:
            due, _seq, timer, fn, args = heapq.heappop(self._timers)
            self.now = due
            if not timer.cancelled:
                _run_guarded(fn, args, self._on_error)
        self.now = end

    def pending_timers(self) -> int:
        return sum(1 for entry in self._timers if not entry[2].cancelled)

    def stop(self, timeout: float = 5.0) -> bool:
        self._timers.clear()
        return True


class WorkerPool:
    def __init__(self, workers: int = 2, on_error: ErrorHandler = _log_error):
        self._on_error = on_error
        self._queue: queue.Queue = queue.Queue()
        self._stopping = False
        self._threads = [threading.Thread(target=self._run, name=f"ruckus-worker-{i}",
                                          daemon=True) for i in range(workers)]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable, *args: Any) -> None:
        if self._stopping:
            log.debug("worker pool: ignoring work submitted after stop")
            return
        self._queue.put((fn, args))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args = item
            _run_guarded(fn, args, self._on_error)

    def stop(self, timeout: float = 5.0) -> bool:
        """Let running and queued jobs finish, then end every worker. True when all
        ended within `timeout` (they are daemons, so a stuck one cannot block exit)."""
        if not self._stopping:
            self._stopping = True
            for _ in self._threads:
                self._queue.put(None)
        end = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))
        return not any(t.is_alive() for t in self._threads)


class CancellablePool:
    """A separate pool for outbound calls (Voicebox generation, Spotify, updates).

    Why separate: the worker pool has two threads and shares them with ffmpeg. A call
    that may take 300 seconds must not stand in front of a sound decode, and a sound
    decode must not delay a call.

    Cancellation is cooperative and never waits: `cancel_all()` sets a flag that the
    job sees through `cancelled()`, wakes the idle threads, and returns at once. A job
    wedged inside a blocking socket call cannot be interrupted from here; its caller
    passes a socket timeout, and the job checks `cancelled()` between steps. This is the
    Abbruchvertrag: `shutdown` waits for the pool only its share of the budget, and then
    a stuck call does not hold the app open.

    The flag is cleared by the next `submit`: a cancel is aimed at what runs now, and
    without the reset every later call would be dropped as if it were cancelled too.
    Consequence to know: a job queued while `cancel_all` ran is dropped, and a job
    submitted in the same instant loses the cancel of a still running job.
    """

    def __init__(self, workers: int = 2, on_error: ErrorHandler = _log_error):
        self._on_error = on_error
        self._jobs: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._stopping = False
        self._threads = [threading.Thread(target=self._run, name=f"ruckus-net-{i}", daemon=True)
                         for i in range(max(1, workers))]
        for thread in self._threads:
            thread.start()

    def cancelled(self) -> bool:
        """True once cancel_all() ran. A long job checks this between steps."""
        return self._cancel.is_set()

    def submit(self, fn: Callable, *args: Any) -> None:
        if self._stopping:
            log.debug("net pool: ignoring work submitted after stop")
            return
        if self._cancel.is_set():
            self._cancel.clear()  # a cancelled run ends here; the next job starts fresh
        self._jobs.put((fn, args))

    def cancel_all(self) -> None:
        """Sets the flag, wakes the idle threads, returns at once."""
        self._cancel.set()
        for _ in self._threads:
            self._jobs.put(WAKE)

    @property
    def alive(self) -> bool:
        return not self._stopping

    def stop(self, timeout: float = 5.0) -> bool:
        """Drop what is queued, ask the running jobs to give up, wait at most `timeout`.
        False means a call was still stuck; they are daemons, so that cannot block exit."""
        self._stopping = True
        self.cancel_all()
        for _ in self._threads:
            self._jobs.put(None)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                if self._stopping:
                    return
                continue
            if item is WAKE:
                continue
            fn, args = item
            if self._cancel.is_set():
                continue  # a cancelled run: queued work is dropped, the running job saw the flag
            _run_guarded(fn, args, self._on_error)
