"""Transportunabhängige Brücke zwischen App-Kern und Oberfläche (Spec §3).

Kennt nur protocol.py und access.py. Der Server ruft send/get_state/ready und liefert
die Bündel über `deliver(client_id, messages)` aus. Es gibt keine zweite Codeschiene:
das Fenster spricht denselben Weg wie der Browser.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import protocol
from .access import ROLE_WINDOW, Access
from .protocol import Notice, ProtocolError

log = logging.getLogger(__name__)

MAX_RATE = 30.0
# A client that never asks for its state must not grow without bound: keep the newest
# events. A well-behaved client calls get_state (or opens the stream) right away and
# flushes long before this matters; the cap only protects against an abandoned client.
MAX_PENDING = 1000
REJECTED = "Die Oberfläche hat eine ungültige Nachricht geschickt: {}"
FORBIDDEN = "Dieser Zugang darf nur abspielen und ansehen. Verwaltung im Fenster."
_FILE_PATH_COMMANDS = (protocol.AddSound, protocol.SetSoundIcon,
                       protocol.ExportSounds, protocol.ImportPack, protocol.SetCollectionCover)


class Client:
    """One connected view: role, its own queue, and whether it may receive yet."""

    def __init__(self, client_id: str, role: str):
        self.client_id = client_id
        self.role = role
        self.pending: list[dict] = []
        self.ready = False
        self.info: dict = {}


class BridgeCore:
    def __init__(self, core, deliver: Callable[[str, list], None], *, access=None,
                 max_rate: float = MAX_RATE, clock: Callable[[], float] = time.monotonic):
        self._core = core
        self._deliver = deliver
        self._access = (access if access is not None
                        else getattr(core, "access", None) or Access(core.store))
        self._interval = 1.0 / max_rate
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, Client] = {}
        self._hotkeys_suspended = False  # only the window role sets this
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_flush = float("-inf")
        self.on_ready: Callable[[str, dict], None] | None = None
        self._held: list[dict] = []  # startup notices, for the first window stream
        self._unsubscribe = core.subscribe(self._queue)  # before core.start()

    # ---- keys ----

    @property
    def window_token(self) -> str:
        return self._access.window_token

    @property
    def view_token(self) -> str:
        return self._access.view_token

    def rotate_view_token(self) -> str:
        return self._access.rotate_view_token()

    def role_for(self, token) -> str | None:
        return self._access.role_for(token)

    def view_url(self, port: int) -> str:
        """The bookmark link for the view role (spec §7): play and browse, nothing else."""
        return f"http://127.0.0.1:{port}/#t={self._access.view_token}"

    # ---- clients ----

    def add_client(self, client_id: str, token) -> Client | None:
        """None when the key is unknown; a refused connection never becomes a client."""
        role = self._access.role_for(token)
        if role is None:
            log.warning("connection refused: unknown or missing key")
            return None
        with self._lock:
            client = Client(client_id, role)
            self._clients[client_id] = client
        return client

    def remove_client(self, client_id: str, *, resume_hotkeys: bool = True) -> None:
        """Drop a client. A dying window client must not leave the hotkeys suspended
        (`resume_hotkeys=True`, the default); a request that only borrowed a client for
        one call passes False so its cleanup cannot undo a deliberate suspension."""
        with self._lock:
            client = self._clients.pop(client_id, None)
            release = (resume_hotkeys and self._hotkeys_suspended
                       and client is not None and client.role == ROLE_WINDOW)
            if release:
                self._hotkeys_suspended = False
        if release:
            self._core.send(protocol.ResumeHotkeys())

    def client_unready(self, client_id: str) -> None:
        """The page navigated home: hold its events until it calls get_state again."""
        with self._lock:
            client = self._clients.get(client_id)
            if client is not None:
                client.ready = False

    def _client(self, client_id: str) -> Client | None:
        with self._lock:
            return self._clients.get(client_id)

    def viewer_count(self) -> int:
        """Pages that asked for the state and are listening (Spotify polls only for them)."""
        with self._lock:
            return sum(1 for client in self._clients.values() if client.ready)

    # ---- client -> core ----

    def send(self, client_id: str, text: Any) -> dict:
        client = self._client(client_id)
        if client is None:
            return {"ok": False, "error": "unknown client"}
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("kind") != "command":
                kind = data.get("kind") if isinstance(data, dict) else type(data).__name__
                raise ProtocolError(f"only commands are accepted, got {kind!r}")
            command = protocol.from_json(data)
            if not isinstance(command, protocol.Command):
                raise ProtocolError(f"{type(command).__name__} is not a command")
            if isinstance(command, _FILE_PATH_COMMANDS):
                raise ProtocolError(f"{type(command).__name__} carries a file path; "
                                    "the interface never sends file paths")
        except (TypeError, ValueError) as exc:  # json errors and ProtocolError are ValueErrors
            log.warning("rejected message from the interface: %s", exc)
            self._notice(client_id, REJECTED.format(exc), "hint")
            return {"ok": False, "error": str(exc)}
        if not self._access.may_send(client.role, command):
            log.info("role %s may not send %s", client.role, type(command).__name__)
            self._notice(client_id, FORBIDDEN, "hint")
            return {"ok": False, "error": FORBIDDEN}
        if isinstance(command, protocol.SuspendHotkeys):
            with self._lock:
                self._hotkeys_suspended = True
        elif isinstance(command, protocol.ResumeHotkeys):
            with self._lock:
                self._hotkeys_suspended = False
        self._core.send(command)
        return {"ok": True}

    def mark_ready(self, client_id: str) -> None:
        """Let this client receive events. The first state read and the opening of the
        event stream are both a fresh page, so either one releases a suspension the old
        page left behind."""
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return
            client.ready = True
            release = self._hotkeys_suspended and client.role == ROLE_WINDOW
            if release:
                self._hotkeys_suspended = False
        if release:
            self._core.send(protocol.ResumeHotkeys())

    def get_state(self, client_id: str) -> str:
        client = self._client(client_id)
        if client is None:
            return json.dumps({"error": "unknown client"}, ensure_ascii=False)
        self.mark_ready(client_id)
        try:
            state = self._core.get_state()
            # The client's role always wins: if the core's own state ever gained a "role" key,
            # `{"role": ..., **state}` would let that stale value overwrite the real one.
            return json.dumps({**state, "role": client.role}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - the page shows it, the app keeps running
            log.warning("state for the interface failed: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def ready(self, client_id: str, info: Any = "{}") -> None:
        client = self._client(client_id)
        if client is None:
            return
        try:
            parsed = json.loads(info) if info else {}
        except (TypeError, ValueError):
            parsed = {}
        client.info = parsed if isinstance(parsed, dict) else {}
        if self.on_ready is not None:
            self.on_ready(client_id, client.info)

    def hold_notice(self, text: str, level: str = "info") -> None:
        """A notice from before any page exists (port fell back, "updated to X"). The page
        gets its own client id per stream, so it waits for the first window stream."""
        with self._lock:
            self._held.append(protocol.to_json(Notice(text, level)))

    def stream_opened(self, client_id: str) -> None:
        """An event stream opened: the first window stream takes the held notices."""
        with self._lock:
            client = self._clients.get(client_id)
            if client is None or client.role != ROLE_WINDOW or not self._held:
                return
            client.pending.extend(self._held)
            self._held = []
        self._wake.set()

    def _notice(self, client_id: str, text: str, level: str = "hint") -> None:
        """A rejected message is answered to its sender, never broadcast."""
        message = protocol.to_json(Notice(text, level))
        with self._lock:
            client = self._clients.get(client_id)
            if client is not None:
                client.pending.append(message)
        self._wake.set()

    # ---- core -> client ----

    def _queue(self, event: Any) -> None:  # any thread
        try:
            message = protocol.to_json(event)
        except ProtocolError:
            log.exception("event cannot be sent to the interface")
            return
        key = protocol.coalesce_key(message)
        with self._lock:
            for client in self._clients.values():
                if key is not None:
                    client.pending = [m for m in client.pending
                                      if protocol.coalesce_key(m) != key]
                client.pending.append(message)
                if len(client.pending) > MAX_PENDING:
                    del client.pending[:len(client.pending) - MAX_PENDING]
        self._wake.set()

    def flush(self) -> int:
        """Hand every ready client its pending events. Returns how many messages were
        handed over; 0 while nobody is ready or nothing is pending."""
        with self._lock:
            batches: dict[str, list[dict]] = {}
            for client_id, client in self._clients.items():
                if client.ready and client.pending:
                    batches[client_id] = client.pending
                    client.pending = []
        if not batches:
            return 0
        self._last_flush = self._clock()
        delivered = 0
        for client_id, messages in batches.items():
            try:
                self._deliver(client_id, messages)
            except Exception:  # noqa: BLE001 - a closing page must not stop the pump
                log.exception("could not deliver %d event(s) to %s", len(messages), client_id)
            else:
                delivered += len(messages)
        return delivered

    def _run_pump(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            if self._stop.is_set():
                break
            wait = self._interval - (self._clock() - self._last_flush)
            if wait > 0:
                self._stop.wait(wait)
            self._wake.clear()
            self.flush()
        self.flush()  # whatever arrived while closing

    def start_pump(self) -> threading.Thread:
        thread = threading.Thread(target=self._run_pump, name="bridgecore-pump", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._unsubscribe()
        self._stop.set()
        self._wake.set()
