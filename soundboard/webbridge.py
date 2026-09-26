"""Bridge between the pywebview window and the app core (spec B0 §6).

JS -> Python: JsApi.send(json) takes only protocol commands (kind == "command",
decoded by protocol.from_json). Anything else is rejected with a Notice for the
page - a bad message never raises into pywebview.

Python -> JS: core events are queued (any thread) and delivered by one pump thread
as a JSON array to window.__ruckusEvent, at most `max_rate` calls per second. Within
one batch only the newest StateChanged survives: it carries the whole snapshot.

The bridge knows protocol.py and nothing else of the core."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import protocol
from .protocol import Notice, ProtocolError

log = logging.getLogger(__name__)

MAX_RATE = 30.0
REJECTED = "Die Oberfläche hat eine ungültige Nachricht geschickt: {}"
_JS_CALL = "window.__ruckusEvent && window.__ruckusEvent({})"
# "Die Oberflaeche uebergibt nie Dateipfade": these commands carry a path and are
# rejected at the trust boundary even though nothing on today's page sends them.
_FILE_PATH_COMMANDS = (protocol.AddSound, protocol.SetSoundIcon, protocol.ExportSounds, protocol.ImportPack)


class WebBridge:
    def __init__(self, core, *, max_rate: float = MAX_RATE, clock: Callable[[], float] = time.monotonic):
        self._core = core
        self._interval = 1.0 / max_rate
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._window = None
        self._last_flush = float("-inf")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._page_ready = threading.Event()  # set by get_state(): the page has a bridge to receive events
        # The page suspends hotkeys while its capture field is open. If it reloads or
        # dies before resuming, the fresh page's get_state() resumes them.
        self._hotkeys_suspended = False
        self.ready_info: dict | None = None
        self.on_ready: Callable[[dict], None] | None = None
        self._unsubscribe = core.subscribe(self._queue)  # before core.start(): no start notice is lost

    def attach(self, window) -> None:
        self._window = window
        self._wake.set()

    # ---- JS -> Python (pywebview's JS-API threads) ----

    def send(self, text: Any) -> dict:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("kind") != "command":
                kind = data.get("kind") if isinstance(data, dict) else type(data).__name__
                raise ProtocolError(f"only commands are accepted, got {kind!r}")
            command = protocol.from_json(data)
            if not isinstance(command, protocol.Command):
                raise ProtocolError(f"{type(command).__name__} is not a command")
            if isinstance(command, _FILE_PATH_COMMANDS):
                raise ProtocolError(f"{type(command).__name__} carries a file path; the interface never sends file paths")
        except (TypeError, ValueError) as exc:  # json errors and ProtocolError are ValueErrors
            log.warning("rejected message from the web interface: %s", exc)
            self._queue(Notice(REJECTED.format(exc), "hint"))
            return {"ok": False, "error": str(exc)}
        if isinstance(command, protocol.SuspendHotkeys):
            self._hotkeys_suspended = True
        elif isinstance(command, protocol.ResumeHotkeys):
            self._hotkeys_suspended = False
        self._core.send(command)
        return {"ok": True}

    def get_state(self) -> str:
        self._page_ready.set()  # the page has a bridge now: flush() may start delivering events to it
        if self._hotkeys_suspended:  # get_state is the page's startup call: nothing is capturing
            self._hotkeys_suspended = False
            self._core.send(protocol.ResumeHotkeys())
        try:
            return json.dumps(self._core.get_state(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - the page shows it, the window keeps running
            log.warning("state for the web interface failed: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def ready(self, info: Any = "{}") -> None:
        try:
            parsed = json.loads(info) if info else {}
        except (TypeError, ValueError):
            parsed = {}
        self.ready_info = parsed if isinstance(parsed, dict) else {}
        if self.on_ready is not None:
            self.on_ready(self.ready_info)

    # ---- core -> JS ----

    def _queue(self, event: Any) -> None:  # any thread
        try:
            msg = protocol.to_json(event)
        except ProtocolError:
            log.exception("event cannot be sent to the web interface")
            return
        with self._lock:
            if msg["type"] == "StateChanged":
                self._pending = [m for m in self._pending if m["type"] != "StateChanged"]
            self._pending.append(msg)
        self._wake.set()

    def flush(self) -> int:
        """Deliver everything queued in one evaluate_js call. Returns the number of
        events handed over (0 while no window is attached, or before the page has
        called get_state() at least once: they stay queued either way)."""
        if self._window is None or not self._page_ready.is_set():
            return 0
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return 0
        self._last_flush = self._clock()
        try:
            self._window.evaluate_js(_JS_CALL.format(json.dumps(batch, ensure_ascii=False)))
        except Exception:  # noqa: BLE001 - window closing or page reloading
            log.exception("could not deliver %d event(s) to the web interface", len(batch))
        return len(batch)

    def _run_pump(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            if self._stop.is_set():
                break
            wait = self._interval - (self._clock() - self._last_flush)
            if wait > 0:
                self._stop.wait(wait)  # holds the rate at max_rate; wakes early on close()
            self._wake.clear()
            self.flush()
        self.flush()  # whatever arrived while closing

    def start_pump(self) -> threading.Thread:
        thread = threading.Thread(target=self._run_pump, name="webbridge-pump", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._unsubscribe()
        self._stop.set()
        self._wake.set()

    def page_reloaded(self) -> None:
        """Call when the page navigates home again (e.g. a blocked navigation sends it
        back): events wait again until the new page has called get_state()."""
        self._page_ready.clear()


class JsApi:
    """Exactly what the page may call. pywebview exposes every public attribute of
    the js_api object (and their public methods), so the bridge stays private."""

    def __init__(self, bridge: WebBridge):
        self._bridge = bridge

    def send(self, text):
        return self._bridge.send(text)

    def get_state(self):
        return self._bridge.get_state()

    def ready(self, info="{}"):
        self._bridge.ready(info)
        return None
