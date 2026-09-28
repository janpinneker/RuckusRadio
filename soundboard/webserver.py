"""Lokaler Server: webui/dist, /events (SSE) und die Befehlsendpunkte (Spec §3, §6).

Nur auf 127.0.0.1. An JEDER Route - auch an jeder angemeldeten - gelten zwei Prüfungen:
der Host-Header muss der eigene sein (das blockt DNS-Rebinding, sonst könnte eine fremde
Seite auf 127.0.0.1 zeigen) und der Origin-Header die eigene Herkunft.

Eine Ausnahme, gemessen und nicht geraten: **GET und HEAD dürfen ohne Origin kommen.**
Chrome 153 schickt den Origin-Header nur für andere Methoden - das eigene `fetch("/state")`
und `new EventSource("/events")` der Seite reisen ohne ihn, und JavaScript kann ihn nicht
setzen (verbotener Header). Der eigene Server wäre sonst für die eigene Seite nicht
erreichbar. Ein *vorhandener* fremder Origin wird weiter abgewiesen, und ein
seitenübergreifender GET nur dann durchgelassen, wenn er eine echte Navigation ist: genau
die Gestalt des Spotify-Rückwegs auf /callback (der Browser navigiert dorthin von
accounts.spotify.com und schickt dabei `Sec-Fetch-Site: cross-site`). POST bleibt streng.

Routen sind anmeldbar, weil das erste Fachmodul mit eigener Route schon feststeht: der
Spotify-Rückkanal braucht /callback. Ohne Routentabelle müsste dafür dieser Handler
umgebaut werden.

Der Schlüssel reist als Query-Parameter, weil EventSource keine eigenen Header setzen
kann. Deshalb wird der Zugriffspfad nie geloggt (log_message ist abgeschaltet) und das
Zugriffsprotokoll bleibt stumm.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .access import ROLE_WINDOW

log = logging.getLogger(__name__)

DEFAULT_PORT = 47800
PORT_ATTEMPTS = 20
HEARTBEAT_S = 15.0
LOCAL_HOSTS = ("127.0.0.1", "localhost")
TOKEN_HEADER = "X-Ruckus-Token"
MAX_BODY = 1 << 20  # a command is small; anything larger is refused

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def check_host(host: str | None, port: int) -> bool:
    """The Host header must name this very server: blocks DNS rebinding."""
    if not host:
        return False
    name, _, given = host.rpartition(":")
    if not name or not given.isdigit() or int(given) != port:
        return False
    return name.lower() in LOCAL_HOSTS


def check_origin(origin: str | None, port: int) -> bool:
    """The Origin header must be this very server; no origin at all is refused."""
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme != "http" or (parts.hostname or "").lower() not in LOCAL_HOSTS:
        return False
    try:
        given = parts.port
    except ValueError:  # "http://127.0.0.1:abc" - a check must not raise
        return False
    return given == port


def check_safe_origin(origin: str | None, port: int, *, site: str | None = None,
                      mode: str | None = None) -> bool:
    """GET/HEAD: no Origin is the normal case, a foreign one is still refused.

    A cross-site GET has to be a top-level navigation to pass; that is the shape of the
    Spotify return leg. A sneaky cross-site subresource request (``Sec-Fetch-Mode: cors``
    or ``no-cors``) is refused here. Every request still needs its key.
    """
    if origin:
        return check_origin(origin, port)
    if site == "cross-site" and mode != "navigate":
        return False
    return True


def safe_dist_path(dist: Path, url_path: str) -> Path | None:
    """A file inside `dist`, or None. No `..`, no backslash, no NUL, no directory."""
    path = unquote(urlsplit(url_path).path or "/")
    if "\x00" in path or "\\" in path:
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    target = dist.joinpath(*(parts or ["index.html"]))
    if target.is_dir():
        target = target / "index.html"
    if not target.suffix or not target.is_file():
        return None
    return target


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """A client that hangs up mid-request must not print a traceback.

    When a tab closes while the handler is still reading the request (or writing the
    answer), ``socketserver`` reaches ``handle_error`` with a ConnectionResetError /
    BrokenPipeError / OSError and prints the full traceback to stderr. That is not an
    error - the stream is cleaned up correctly - so it is logged at DEBUG only. Every
    other exception still goes through the normal traceback, so a real fault stays
    visible. Measured on Chrome 153: closing a tab produced one such traceback per
    request (SSE stream, /state, a static file).
    """

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, OSError)):
            log.debug("client %s went away: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


class SseServer:
    """One HTTP server: static files, the event stream, and the command endpoints."""

    def __init__(self, bridge, dist_dir: Path, *, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT):
        self.bridge = bridge
        self.dist_dir = Path(dist_dir)
        self.host = host
        self.port = port
        self.requested_port = port
        self.fell_back = False
        self._httpd = None
        self._lock = threading.Lock()
        self._streams: dict[str, queue.Queue] = {}
        self._routes: dict[str, object] = {}
        self._next_id = 0

    # ---- routes ----

    def add_route(self, path: str, handler) -> None:
        """`handler(handler_obj, query, body) -> (status, content_type, bytes)`.

        Gets the same Host and Origin checks as every other route; it does not need its
        own token check unless it wants one.
        """
        if not path.startswith("/"):
            raise ValueError(f"a route starts with '/': {path!r}")
        if path in self._routes:
            raise ValueError(f"route already registered: {path}")
        self._routes[path] = handler

    def remove_route(self, path: str) -> None:
        """Drop a one-shot route again (the OAuth callback does this after its single hit)."""
        self._routes.pop(path, None)

    # ---- clients ----

    def new_client_id(self) -> str:
        with self._lock:
            self._next_id += 1
            return f"c{self._next_id}"

    def client_for(self, token) -> str | None:
        client_id = self.new_client_id()
        if self.bridge.add_client(client_id, token) is None:
            return None
        return client_id

    def deliver(self, client_id: str, messages: list) -> None:
        with self._lock:
            stream = self._streams.get(client_id)
        if stream is not None:
            stream.put(messages)

    # ---- helpers ----

    @staticmethod
    def content_type(path: Path) -> str:
        return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

    @staticmethod
    def token_from(raw_path: str, headers) -> str | None:
        """From the header (fetch) or the query (EventSource cannot set one)."""
        header = headers.get(TOKEN_HEADER)
        if header:
            return header
        values = parse_qs(urlsplit(raw_path).query).get("token")
        return values[0] if values else None

    def url_for(self, token: str) -> str:
        return f"http://127.0.0.1:{self.port}/#t={token}"

    # ---- lifecycle ----

    def serve_forever(self) -> None:
        handler = self._make_handler()
        last_error = None
        for candidate in range(self.requested_port, self.requested_port + PORT_ATTEMPTS):
            try:
                self._httpd = QuietThreadingHTTPServer((self.host, candidate), handler)
            except OSError as exc:
                last_error = exc
                continue
            self.port = self._httpd.server_address[1]
            self.fell_back = self.port != self.requested_port
            if self.fell_back:
                log.warning("port %d was taken, the interface is on %d instead",
                            self.requested_port, self.port)
            break
        else:
            raise last_error
        self._httpd.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        for stream in list(self._streams.values()):
            stream.put(None)  # ends the SSE loop
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._httpd = None

    # ---- handler ----

    def _make_handler(self):
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "RuckusRadio"

            # -- never log a path: it carries the key --
            def log_message(self, fmt, *args):
                return

            # -- helpers --

            def _port(self) -> int:
                return server_self.port

            def _checked(self) -> bool:
                if not check_host(self.headers.get("Host"), self._port()):
                    self._deny(400, "bad host")
                    return False
                origin = self.headers.get("Origin")
                if self.command in ("GET", "HEAD"):
                    # our own page cannot send an Origin on a GET (see the module docstring)
                    ok = check_safe_origin(origin, self._port(),
                                           site=self.headers.get("Sec-Fetch-Site"),
                                           mode=self.headers.get("Sec-Fetch-Mode"))
                else:
                    ok = check_origin(origin, self._port())
                if not ok:
                    self._deny(403, "bad origin")
                    return False
                return True

            def _role(self) -> str | None:
                """The role behind this request, or None after answering with a refusal."""
                if not self._checked():
                    return None
                token = server_self.token_from(self.path, self.headers)
                role = server_self.bridge.role_for(token)
                if role is None:
                    self._deny(401, "missing or unknown key")
                    return None
                return role

            def _borrowed_client(self, token) -> str | None:
                """A client for one request only. /state, /send and /ready need no durable
                queue; keeping them in the bridge is what used to leak, and a per-request
                client never received the event stream's messages anyway."""
                return server_self.client_for(token)

            def _release_client(self, client_id) -> None:
                if client_id is not None:
                    # resume_hotkeys=False: this request only borrowed the client, its
                    # cleanup must not undo a suspension the page deliberately set.
                    server_self.bridge.remove_client(client_id, resume_hotkeys=False)

            def _deny(self, code: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, text: str, code: int = 200) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # -- routes --

            def do_GET(self):  # noqa: N802 - http.server's name
                path = urlsplit(self.path).path
                if path in server_self._routes:
                    self._custom(path, "GET")
                    return
                if path == "/events":
                    self._events()
                    return
                if path == "/state":
                    if self._role() is None:
                        return
                    client_id = self._borrowed_client(
                        server_self.token_from(self.path, self.headers))
                    if client_id is None:
                        self._deny(401, "refused")
                        return
                    try:
                        state = json.loads(server_self.bridge.get_state(client_id))
                        if "role" in state:
                            state["view_url"] = (
                                server_self.bridge.view_url(server_self.port)
                                if state["role"] == ROLE_WINDOW else None
                            )
                        self._json(json.dumps(state, ensure_ascii=False))
                    finally:
                        self._release_client(client_id)
                    return
                self._static("/index.html" if path in ("", "/") else self.path)

            def do_POST(self):  # noqa: N802 - http.server's name
                path = urlsplit(self.path).path
                if path in server_self._routes:
                    self._custom(path, "POST")
                    return
                if path not in ("/send", "/ready"):
                    self._deny(404, "not found")
                    return
                role = self._role()
                if role is None:
                    return
                body = self._body()
                if body is None:
                    return
                client_id = self._borrowed_client(
                    server_self.token_from(self.path, self.headers))
                if client_id is None:
                    self._deny(401, "refused")
                    return
                try:
                    if path == "/send":
                        self._json(json.dumps(server_self.bridge.send(client_id, body)))
                    else:
                        server_self.bridge.ready(client_id, body)
                        self._json("{}")
                finally:
                    self._release_client(client_id)

            def _body(self) -> str | None:
                raw = (self.headers.get("Content-Length") or "0").strip()
                if not raw.isdigit():  # "-1" would read until the client hangs up
                    self._deny(400, "bad length")
                    return None
                length = int(raw)
                if length > MAX_BODY:
                    self._deny(413, "too large")
                    return None
                return self.rfile.read(length).decode("utf-8") if length else ""

            def _custom(self, path: str, method: str):
                if not self._checked():
                    return
                body = self._body() if method == "POST" else ""
                if body is None:
                    return
                # `.get`: a one-shot route may be dropped between the dispatch check and
                # here (a callback arriving exactly while the login gives up)
                handler = server_self._routes.get(path)
                if handler is None:
                    self._deny(404, "not found")
                    return
                query = parse_qs(urlsplit(self.path).query)
                try:
                    status, content_type, payload = handler(self, query, body)
                except Exception:  # noqa: BLE001 - a broken route must not kill the server
                    log.exception("route %s failed", path)
                    self._deny(500, "route failed")
                    return
                self._send(status, content_type, payload)

            def _static(self, url_path: str):
                # spec §6: both checks at every route, the files included (DNS rebinding)
                if not self._checked():
                    return
                target = safe_dist_path(server_self.dist_dir, url_path)
                if target is None:
                    self._deny(404, "not found")
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", server_self.content_type(target))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")  # a meta CSP cannot say frame-ancestors
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _events(self):
                role = self._role()
                if role is None:
                    return
                token = server_self.token_from(self.path, self.headers)
                client_id = server_self.client_for(token)
                if client_id is None:
                    self._deny(401, "refused")
                    return
                # Opening the stream is this client's readiness signal: events flow right
                # away, without waiting for a separate /state on another connection.
                server_self.bridge.mark_ready(client_id)
                server_self.bridge.stream_opened(client_id)
                stream: queue.Queue = queue.Queue()
                with server_self._lock:
                    server_self._streams[client_id] = stream
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                # A stream has no length, so its end is the end of the connection: without
                # "close", HTTP/1.1 would wait for a next request on the socket and a stream
                # cut off by a rotated key would just hang open in the old tab. (The header
                # also sets close_connection; EventSource does not care about it.)
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        try:
                            messages = stream.get(timeout=HEARTBEAT_S)
                        except queue.Empty:
                            if server_self.bridge.role_for(token) is None:
                                break  # the view key was rotated (spec §7); this stream is done
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if messages is None:
                            break
                        payload = json.dumps(messages, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server_self._lock:
                        server_self._streams.pop(client_id, None)
                    server_self.bridge.remove_client(client_id)

        return Handler
