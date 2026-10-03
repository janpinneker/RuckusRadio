"""Der App-Kern: Registratur, Warteschlange, Ereignisse, Fehlerhuelle, Lebenszyklus.

Hier steht bewusst KEINE Fachlogik. Fachmodule registrieren sich selbst:
  core.handle(Befehlsklasse, fn)  - fn(befehl) laeuft auf dem Kern-Thread
  core.add_state(name, fn, group="volatile") - fn() liefert ihren Teil der Momentaufnahme;
                                    group trennt klein/hauefig (volatile) von gross/selten
                                    (z.B. library), damit die Last pro Sekunde nicht mit
                                    der Bibliothek waechst. core.changed(group) meldet sie.
  core.on_start(fn), core.on_shutdown(fn) - Lebenszyklus, auf dem Kern-Thread
  core.before_shutdown(fn)        - laeuft sofort im Thread, der shutdown() ruft
Registrierung (handle, add_state, on_*, before_shutdown) muss abgeschlossen sein, bevor
start() laeuft - die Register sind nicht durch ein Lock geschuetzt.
Neue Bereiche (Musik, Blackbox) kommen als weitere Fachmodule dazu, ohne diese Datei
zu aendern; nur create_core() baut sie mit ein.

Threads (Spec §3.2): executor = Kern-Thread (Zustand, Config, Entscheidungen),
devices = Geraete-Thread (alles PortAudio/COM), workers = Datei/ffmpeg. Im Testmodus
(inline=True) laufen alle drei sofort im aufrufenden Thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .executors import CancellablePool, InlineExecutor, SerialExecutor, WorkerPool
from .protocol import PartChanged, PROTOCOL_VERSION, Notice, StateChanged
from .store import Store

log = logging.getLogger(__name__)

STATE_INTERVAL_S = 1 / 30  # StateChanged at most ~30 times per second
# Die Gruppe eines Zustandsteils ist ein freies Etikett (Musik und Stimme legen spaeter
# eigene an), aber kein leerer Name. Der Vorgabewert ist der kleine, haeufige Teil.
DEFAULT_GROUP = "volatile"
INTERNAL_ERROR = "Interner Fehler – Details stehen in ruckus.log."
SAVE_FAILED = ("Einstellungen konnten nicht gespeichert werden. "
               "Die bisherige Datei bleibt erhalten.")


class Core:
    def __init__(self, *, inline: bool = False, store_data: dict | None = None):
        self.inline = inline
        if inline:
            self.executor = InlineExecutor(self._task_failed)
            self.devices = InlineExecutor(self._task_failed)
            self.workers = InlineExecutor(self._task_failed)
            self.net = InlineExecutor(self._task_failed)
        else:
            self.executor = SerialExecutor("ruckus-core", self._task_failed)
            from .devices import init_thread_com
            self.devices = SerialExecutor("ruckus-devices", self._task_failed,
                                          on_start=init_thread_com)
            self.workers = WorkerPool(2, self._task_failed)
            self.net = CancellablePool(2, self._task_failed)
        self.store = Store(self.executor, data=store_data)
        self.store.on_save_failed = lambda _exc: self.notice(SAVE_FAILED, "hint")
        # Set by create_core(); services reach each other through these.
        self.engine = None
        self.playback = None
        self.jobs = None
        self.hotkeys = None
        self.library = None
        self.routing = None
        self.settings = None
        self.updates = None
        self.spotify = None
        self.spotify_player = None
        self.musicbus = None
        self._handlers: dict[type, Callable[[Any], None]] = {}
        self._state_parts: dict[str, Callable[[], Any]] = {}
        self._state_groups: dict[str, str] = {}
        self._start_steps: list[Callable[[], None]] = []
        self._shutdown_steps: list[Callable[[], None]] = []
        self._before_shutdown: list[Callable[[], None]] = []
        self._subscribers: list[Callable[[Any], None]] = []
        self._subscribers_lock = threading.Lock()
        self._accepting = True
        self._shutdown_lock = threading.Lock()
        self._shutdown_state: str | None = None  # None -> "running" -> "done"
        self._shutdown_result: bool | None = None
        self._state_timer = None
        self._state_interval = 0.0 if inline else STATE_INTERVAL_S

    # ---- registration ----

    def handle(self, command_type: type, fn: Callable[[Any], None]) -> None:
        if command_type in self._handlers:
            raise ValueError(f"{command_type.__name__} already has a handler")
        self._handlers[command_type] = fn

    def add_state(self, name: str, fn: Callable[[], Any], group: str = DEFAULT_GROUP) -> None:
        """`fn` is a PURE READ of cached data: no network, no disk, no devices. It runs
        on the core thread and must never block there."""
        if name in self._state_parts or name == "protocol":
            raise ValueError(f"state part {name!r} already exists")
        if not group:
            raise ValueError("a state group needs a name")
        self._state_parts[name] = fn
        self._state_groups[name] = group

    def on_start(self, fn: Callable[[], None]) -> None:
        self._start_steps.append(fn)

    def on_shutdown(self, fn: Callable[[], None]) -> None:
        self._shutdown_steps.append(fn)

    def before_shutdown(self, fn: Callable[[], None]) -> None:
        self._before_shutdown.append(fn)

    def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Events arrive on the core thread; an interface marshals them to its own."""
        with self._subscribers_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._subscribers_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ---- commands (any thread) ----

    def send(self, command: Any) -> None:
        if not self._accepting:
            log.info("core is shutting down, dropping %s", type(command).__name__)
            return
        self.executor.submit(self._dispatch, command)

    def _dispatch(self, command: Any) -> None:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise LookupError(f"no handler registered for {type(command).__name__}")
        handler(command)

    def _task_failed(self, exc: Exception) -> None:
        """Error hull for every thread: log, tell the interface, keep running."""
        log.error("task failed", exc_info=exc)
        self.executor.submit(self.notice, INTERNAL_ERROR, "error")

    # ---- events (core thread) ----

    def emit(self, event: Any) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                log.exception("event subscriber failed")

    def notice(self, text: str, level: str = "hint") -> None:
        self.emit(Notice(text, level))

    def state_changed(self) -> None:
        """Mark the snapshot dirty. Inline: publish now. Threaded: at most every
        STATE_INTERVAL_S, so a burst of changes becomes one StateChanged."""
        if self._state_interval <= 0:
            self._emit_state()
            return
        if self._state_timer is None:
            self._state_timer = self.executor.call_later(self._state_interval, self._emit_state)

    def _emit_state(self) -> None:
        self._state_timer = None
        self.emit(StateChanged(self.state()))

    def state(self, group: str | None = None) -> dict:
        """Full JSON-safe snapshot; core thread only (use get_state elsewhere). With
        `group`, only that part is built. One broken part must not break the whole
        snapshot: on exception its value is None and the failure is logged."""
        snapshot: dict[str, Any] = {"protocol": PROTOCOL_VERSION}
        for name, fn in self._state_parts.items():
            if group is not None and self._state_groups[name] != group:
                continue
            try:
                snapshot[name] = fn()
            except Exception:
                log.exception("state part %r failed", name)
                snapshot[name] = None
        return snapshot

    def changed(self, group: str = DEFAULT_GROUP) -> None:
        """Announce a change in one group. The library group must be announced this way
        in addition to state_changed(): a page must never learn about a new sound from
        the volatile part."""
        if group == DEFAULT_GROUP:
            self.state_changed()
            return
        self.executor.submit(self._emit_part, group)

    def _emit_part(self, group: str) -> None:
        self.emit(PartChanged(group, self.state(group)))

    def get_state(self, timeout: float = 2.0) -> dict:
        if self.executor.is_current():
            return self.state()
        if self._shutdown_state is not None:
            raise RuntimeError("the core is shutting down")
        if not self.executor.alive:
            raise RuntimeError("the core is not running")
        box: list[dict] = []
        ready = threading.Event()

        def take() -> None:
            try:
                box.append(self.state())
            finally:
                ready.set()

        self.executor.submit(take)
        if not ready.wait(timeout) or not box:
            raise TimeoutError("the core did not answer in time")
        return box[0]

    @property
    def alive(self) -> bool:
        return self.executor.alive

    # ---- lifecycle ----

    def start(self) -> None:
        """Run the registered start steps on the core thread, in registration order."""
        self.executor.submit(self._run_steps, list(self._start_steps), "start")

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Spec §5 order, `timeout` is the total budget for all of it: hooks off (caller
        thread) -> no new commands accepted -> workers get at most half the budget to
        finish their queued/running jobs (a hung ffmpeg must not starve the rest; their
        results still reach the still-running core thread) -> shutdown steps and
        store.flush() run on the core thread, with whatever budget remains -> device
        thread stops streams (only now, so steps can still queue device work such as
        handing the mic back) -> core thread ends. If the core thread truly ended in
        time, the store is flushed once more on the caller thread - safe, since no other
        thread touches it any more, and it catches a command that raced `send` vs.
        shutdown and saved after the steps.
        Refuses to run on the core thread of a threaded core (it would deadlock waiting
        for itself). Idempotent and thread-safe via an "in progress"/"done" state (not a
        lock held across the work): a re-entrant call (a hook calling shutdown() again)
        or a concurrent second caller returns False at once without waiting; once the
        first call has finished, later calls return its stored result."""
        if not self.inline and self.executor.is_current():
            raise RuntimeError("Core.shutdown() must not be called on the core thread")
        with self._shutdown_lock:
            if self._shutdown_state == "done":
                return self._shutdown_result
            if self._shutdown_state == "running":
                # Re-entrant (a hook called shutdown() again) or a concurrent second
                # caller: never wait on ourselves, never run the sequence twice.
                return False
            self._shutdown_state = "running"
        result = False
        try:
            result = self._do_shutdown(timeout)
        finally:
            # Even when a step above raises, later shutdown() calls must see "done"
            # (result False) instead of hanging on "running" forever.
            with self._shutdown_lock:
                self._shutdown_result = result
                self._shutdown_state = "done"
        return result

    def _do_shutdown(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        self._accepting = False
        self._run_steps(list(self._before_shutdown), "before-shutdown")
        # Ausgehende Aufrufe zuerst abbrechen: sie koennen nicht erzwungen werden, also
        # soll ihr Restbudget klein sein und den Rest nicht aufhalten.
        self.net.stop(min(remaining(), timeout / 4))

        finished = threading.Event()

        def steps() -> None:
            try:
                self._run_steps(list(self._shutdown_steps), "shutdown")
                self.store.flush()
            finally:
                finished.set()

        # A hung worker (e.g. ffmpeg) must not consume the whole budget: give it at
        # most half, so the shutdown steps (which may still queue device work, like
        # stopping streams and handing the mic back) and the final flush keep a share.
        ok = self.workers.stop(min(remaining(), timeout / 2))
        self.executor.submit(steps)
        wait_start = time.monotonic()
        if not finished.wait(remaining()):
            waited = time.monotonic() - wait_start
            log.warning("shutdown steps did not finish within %.1f s", waited)
        # Only stop the device thread once the steps have had their chance to queue
        # device work (stopping streams, handing the mic back) onto it.
        ok = self.devices.stop(remaining()) and ok
        core_ended = self.executor.stop(remaining())
        ok = core_ended and ok
        if core_ended:
            self.store.flush()
        return ok

    @staticmethod
    def _run_steps(steps: list[Callable[[], None]], phase: str) -> None:
        for fn in steps:
            try:
                fn()
            except Exception:
                log.exception("%s step %r failed", phase, fn)


def create_core(*, inline: bool = False, engine=None, backend=None, hotkey_manager=None,
                store_data: dict | None = None, autostart_module=None,
                update_source=None, update_dir=None, spotify_api=None,
                musicbus_factory=None) -> Core:
    """The assembled core. Service order is start order: playback (preload),
    hotkeys (register), library (loudness backfill), routing (devices), settings,
    updates, spotify, musicbus. `musicbus_factory` builds the capture bus (tests
    pass a fake - never a real WASAPI capture in the green run)."""

    from .access import Access
    from .appsettings import AppSettingsService
    from .hotkeyservice import HotkeyService
    from .library import LibraryService
    from .musicbus import MusicBusService
    from .playback import PlaybackService
    from .routing import DeviceBackend, RoutingService
    from .spotify import SpotifyService
    from .updates import UpdateService

    core = Core(inline=inline, store_data=store_data)
    if engine is None:
        from .audio import AudioEngine
        engine = AudioEngine(None, None, sink=None)
    core.engine = engine
    core.playback = PlaybackService(core)
    from .jobs import JobsService
    core.jobs = JobsService(core)
    core.access = Access(core.store)
    core.hotkeys = HotkeyService(core, hotkey_manager)
    core.library = LibraryService(core)
    from .collectionservice import CollectionsService
    core.collections = CollectionsService(core)
    core.routing = RoutingService(core, backend if backend is not None else DeviceBackend())
    if autostart_module is None:
        core.settings = AppSettingsService(core)
    else:
        core.settings = AppSettingsService(core, autostart_module)
    core.updates = UpdateService(core, update_source, update_dir)
    core.spotify = SpotifyService(core, api=spotify_api)
    from .spotify_player import SpotifyPlayerService
    core.spotify_player = SpotifyPlayerService(core, core.spotify)
    core.musicbus = MusicBusService(core, factory=musicbus_factory)
    return core
