"""Der lokale Server: nur 127.0.0.1, Host und Origin geprueft, Routen anmeldbar."""

import contextlib
import dataclasses
import http.client
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import typing
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-webserver-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

from soundboard import access  # noqa: E402
from soundboard import protocol as p  # noqa: E402
from soundboard import webserver  # noqa: E402


class FakeCore:
    def __init__(self):
        self.sent = []
        self.subscribers = []

    def subscribe(self, cb):
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)

    def send(self, cmd):
        self.sent.append(cmd)

    def get_state(self, timeout=2.0):
        return {"protocol": 1}

    def emit(self, event):
        for cb in list(self.subscribers):
            cb(event)


class FakeAccess:
    def __init__(self):
        self.window_token = "window-key"
        self.view_token = "view-key"

    def role_for(self, token):
        if token == self.window_token:
            return access.ROLE_WINDOW
        if token == self.view_token:
            return access.ROLE_VIEW
        return None

    def may_send(self, role, command):
        return access.Access.may_send(self, role, command)

    def rotate_view_token(self):
        self.view_token += "2"
        return self.view_token


class Harness:
    def __init__(self):
        from soundboard.bridgecore import BridgeCore
        (Path(_TMP) / "dist" / "assets").mkdir(parents=True, exist_ok=True)
        (Path(_TMP) / "dist" / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (Path(_TMP) / "dist" / "assets" / "app.js").write_text("1", encoding="utf-8")
        (Path(_TMP) / "geheim.txt").write_text("nein", encoding="utf-8")
        self.core = FakeCore()
        self.bridge = BridgeCore(self.core, self._deliver, access=FakeAccess())
        self.boxes: list = []
        self.server = webserver.SseServer(self.bridge, Path(_TMP) / "dist", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5.0
        while self.server.port == 0 and time.monotonic() < deadline:
            time.sleep(0.02)

    def _deliver(self, client_id, messages):
        # Abweichung von der Plan-Vorlage: sie sammelte die Buendel nur in `boxes` ein,
        # statt sie an den Server weiterzugeben - so konnte der Ereignisstrom nie ein
        # Ereignis bekommen und der letzte Test lief in die Zeitueberschreitung.
        self.boxes.append((client_id, messages))
        self.server.deliver(client_id, messages)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.port}"

    def fetch(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def reset(self, method, path):
        """Answer a request, then rip the connection down with an RST.

        That is the shape Agent A measured: a tab that closes just as the handler waits
        for the next request on the keep-alive connection. The server's next read gets a
        ConnectionResetError - the very `self._sock.recv_into(b)` traceback.
        """
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        sock.sendall(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.port}\r\n\r\n"
                     .encode("utf-8"))
        sock.settimeout(2)
        try:
            while sock.recv(4096):  # drain the answer; the stream keeps the socket open
                pass
        except (socket.timeout, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()

    def stop(self):
        self.server.stop()
        self.thread.join(timeout=3)


def test_host_and_origin_must_be_our_own():
    assert webserver.check_host("127.0.0.1:47800", 47800)
    assert webserver.check_host("localhost:47800", 47800)
    for bad in ("evil.com:47800", "evil.com", "127.0.0.1:9999", None, ""):
        assert not webserver.check_host(bad, 47800), bad
    assert webserver.check_origin("http://127.0.0.1:47800", 47800)
    assert webserver.check_origin("http://localhost:47800", 47800)
    for bad in ("http://evil.com", "https://127.0.0.1:47800", "http://127.0.0.1:9999", None):
        assert not webserver.check_origin(bad, 47800), bad
    # a broken port must be a refusal, not a ValueError out of a security check
    assert not webserver.check_origin("http://127.0.0.1:abc", 47800)
    print("nur eigener Host und eigene Herkunft: OK")


def test_static_paths_cannot_escape():
    # Abweichung von der Plan-Vorlage: sie prueft hier Pfade, aber `safe_dist_path`
    # liefert nur existierende Dateien zurueck - und `dist/` entsteht erst in `Harness`.
    # Ohne diese Zeilen scheitert schon die erste Zusicherung.
    dist = Path(_TMP) / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("1", encoding="utf-8")
    assert webserver.safe_dist_path(dist, "/") == dist / "index.html"
    assert webserver.safe_dist_path(dist, "/assets/app.js") == dist / "assets" / "app.js"
    for bad in ("/../geheim.txt", "/..%2Fgeheim.txt", "/assets/../../geheim.txt",
                "/nichts", "/..%5Cgeheim.txt", "/a\x00b"):
        assert webserver.safe_dist_path(dist, bad) is None, bad
    print("kein Pfad verlaesst webui/dist: OK")


def test_the_page_needs_no_key_but_state_and_send_do():
    h = Harness()
    try:
        status, body = h.fetch("GET", "/")
        assert status == 200 and body == b"<html>ok</html>", (status, body)
        status, _ = h.fetch("GET", "/state", headers={"Origin": h.origin})
        assert status == 401, status
        status, body = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                              headers={"Origin": h.origin})
        state = json.loads(body)
        assert status == 200 and state["protocol"] == 1 and state["role"] == "window"
        assert state["view_url"] == f"{h.origin}/#t={h.bridge.view_token}"
    finally:
        h.stop()
    print("die Seite laedt ohne Schluessel, /state braucht einen: OK")


def test_a_get_may_come_without_an_origin_but_a_foreign_one_is_refused():
    h = Harness()
    try:
        # Chrome 153 sends Origin only for methods other than GET/HEAD: the page's own
        # /state and /events arrive without one (measured, see webserver's docstring).
        status, body = h.fetch("GET", f"/state?token={h.bridge.window_token}")
        state = json.loads(body)
        assert status == 200 and state["protocol"] == 1 and state["role"] == "window", (status, body)
        status, _ = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                            headers={"Origin": "http://evil.com"})
        assert status == 403, status
        # a sneaky cross-site GET (a subresource, not a navigation) stays out ...
        status, _ = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
        assert status == 403, status
        # ... while a top-level navigation is the shape of the Spotify return leg
        status, _ = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                     "Sec-Fetch-Dest": "document"})
        assert status == 200, status
        # a command still needs the strict check: no Origin at all is refused
        status, _ = h.fetch("POST", f"/send?token={h.bridge.window_token}",
                            json.dumps(p.to_json(p.StopAll())),
                            {"Content-Type": "application/json"})
        assert status == 403, status
        assert h.core.sent == []
    finally:
        h.stop()
    print("eigener GET ohne Origin erlaubt, fremde Herkunft abgewiesen, POST streng: OK")


def test_static_files_get_the_same_host_and_origin_checks():
    # Spec §6: both checks at every route. Before, /assets/... answered any Host - with
    # DNS rebinding a foreign page could read the bundle.
    h = Harness()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
        conn.request("GET", "/assets/app.js", headers={"Host": f"evil.com:{h.server.port}"})
        assert conn.getresponse().status == 400
        conn.close()
        status, _ = h.fetch("GET", "/assets/app.js",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
        assert status == 403, "a foreign page must not embed our files"
        status, _ = h.fetch("GET", "/", headers={"Origin": "http://evil.com"})
        assert status == 403, status
        # the page itself still loads: typed in, from the window, from the Steam overlay
        conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
        conn.request("GET", "/", headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
        response = conn.getresponse()
        assert response.status == 200 and response.read() == b"<html>ok</html>"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("X-Frame-Options") == "DENY", "no foreign page may frame us"
        conn.close()
        status, _ = h.fetch("GET", "/assets/app.js",
                            headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors"})
        assert status == 200, status
    finally:
        h.stop()
    print("statische Dateien: dieselben Host- und Herkunftspruefungen: OK")


def _post_raw(h, path, length_header, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
    conn.putrequest("POST", path)
    conn.putheader("Content-Length", length_header)
    for name, value in (headers or {}).items():
        conn.putheader(name, value)
    conn.endheaders()
    try:
        return conn.getresponse().status
    finally:
        conn.close()


def test_a_broken_content_length_is_refused_and_checks_come_first():
    h = Harness()
    try:
        own = {"Origin": h.origin}
        path = f"/send?token={h.bridge.window_token}"
        # before: "abc" raised out of the handler, "-1" read until the client hung up
        assert _post_raw(h, path, "abc", own) == 400
        assert _post_raw(h, path, "-1", own) == 400
        h.server.add_route("/callback", lambda handler, query, body: (200, "text/plain", b"ok"))
        # a registered route checks Host/Origin before it reads (or sizes) any body
        assert _post_raw(h, "/callback", str(webserver.MAX_BODY + 1),
                         {"Origin": "http://evil.com"}) == 403
        assert h.core.sent == []
    finally:
        h.stop()
    print("kaputte Content-Length abgewiesen, Pruefung vor dem Lesen: OK")


def test_the_view_may_not_delete_over_http_but_may_play():
    h = Harness()
    try:
        headers = {"Origin": h.origin, "Content-Type": "application/json"}
        status, data = h.fetch("POST", f"/send?token={h.bridge.view_token}",
                              json.dumps(p.to_json(p.DeleteSound("s1"))), headers)
        assert status == 200 and json.loads(data)["ok"] is False, (status, data)
        assert h.core.sent == []
        status, data = h.fetch("POST", f"/send?token={h.bridge.view_token}",
                              json.dumps(p.to_json(p.Play("s1", 0.5))), headers)
        assert status == 200 and json.loads(data)["ok"] is True, (status, data)
        assert h.core.sent == [p.Play("s1", 0.5)]
    finally:
        h.stop()
    print("die Ansicht darf per HTTP nur abspielen: OK")


def _sample_value(hint: object) -> object:
    """Ein harmloser Beispielwert fuer einen Feldtyp (`_check` verlangt nur JSON-Sicherheit)."""
    if isinstance(hint, str):  # ein Literal-Inhalt, kein Typname
        return "x"
    origin = typing.get_origin(hint) or hint
    if origin is str:
        return "x"
    if origin is bool:
        return True
    if origin is int:
        return 1
    if origin is float:
        return 0.5
    if origin is dict:
        return {}
    if origin is list:
        return []
    if origin is type(None):
        return None
    args = typing.get_args(hint)
    return _sample_value(args[0]) if args else "x"


def sample_command(cls: type) -> object:
    """Ein Beispielbefehl je Klasse, generisch aus den Feldtypen gebaut.

    Absichtlich generisch statt als gepflegte Liste: eine Liste laedt genau den Fehler
    wieder ein, den dieser Test verhindern soll - ein neuer Befehl, der darin fehlt.
    """
    hints = typing.get_type_hints(cls)
    values = {}
    for field in dataclasses.fields(cls):
        if (field.default is not dataclasses.MISSING
                or field.default_factory is not dataclasses.MISSING):
            continue
        values[field.name] = _sample_value(hints.get(field.name))
    return cls(**values)


def test_no_command_beyond_playback_and_music_reaches_the_view():
    """Erzwungen statt aufgezaehlt: fuer JEDEN Befehl der Registratur gilt ueber HTTP,
    dass die Ansicht genau PLAYBACK + MUSIC darf und das Fenster alles, ausser den
    pfadtragenden Befehlen (die weist die Bruecke fuer beide Rollen ab).

    Das ist die zweite Verteidigungslinie hinter der Oberflaeche: selbst wenn eine Seite
    einmal zu viel anzeigt, fuehrt der Klick zu nichts. Und weil hier ueber
    `COMMAND_CAPABILITY` gelaufen wird, kann ein neuer Befehl nicht still durchrutschen.
    """
    h = Harness()
    path_commands = (p.AddSound, p.SetSoundIcon, p.ExportSounds, p.ImportPack)
    view_caps = access.ROLE_CAPABILITIES[access.ROLE_VIEW]
    try:
        headers = {"Origin": h.origin, "Content-Type": "application/json"}
        checked = 0
        for cls, capability in sorted(access.COMMAND_CAPABILITY.items(),
                                      key=lambda pair: pair[0].__name__):
            body = json.dumps(p.to_json(sample_command(cls)))
            before = len(h.core.sent)

            status, data = h.fetch("POST", f"/send?token={h.bridge.view_token}", body, headers)
            answer = json.loads(data)
            expected_view = capability in view_caps
            assert status == 200 and answer["ok"] is expected_view, (cls.__name__, status, data)
            if not expected_view:
                assert len(h.core.sent) == before, f"{cls.__name__} erreichte den Kern trotz Ansicht"
                assert answer["error"], (cls.__name__, data)

            status, data = h.fetch("POST", f"/send?token={h.bridge.window_token}", body, headers)
            answer = json.loads(data)
            expected_window = not issubclass(cls, path_commands)
            assert status == 200 and answer["ok"] is expected_window, (cls.__name__, status, data)
            checked += 1
        assert checked >= 30, checked
    finally:
        h.stop()
    print(f"{checked} Befehle: Ansicht nur PLAYBACK+MUSIC, Fenster alles ausser Pfad-Befehlen: OK")


def test_state_carries_role_and_view_url_and_a_rotated_key_kills_the_stream():
    h = Harness()
    try:
        status, body = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                              headers={"Origin": h.origin})
        state = json.loads(body)
        assert status == 200
        assert state["role"] == "window"
        assert state["view_url"].endswith("#t=" + h.bridge.view_token)
        status, body = h.fetch("GET", f"/state?token={h.bridge.view_token}",
                              headers={"Origin": h.origin})
        view_state = json.loads(body)
        assert view_state["role"] == "view" and view_state["view_url"] is None
        old = h.bridge.view_token
        h.bridge.rotate_view_token()
        assert h.bridge.role_for(old) is None  # der alte Schluessel ist tot
    finally:
        h.stop()
    print("/state nennt Rolle und Link, ein erneuerter Schluessel toetet den alten: OK")


def test_an_extra_route_can_be_registered():
    h = Harness()
    try:
        seen: list[str] = []

        def callback(handler, query, body):
            seen.append(query.get("code", [""])[0])
            return 200, "text/plain; charset=utf-8", b"ok"

        h.server.add_route("/callback", callback)
        # the real return leg is a browser navigation from accounts.spotify.com: it carries
        # no Origin, but it is a navigation and not a subresource request
        status, body = h.fetch("GET", "/callback?code=abc",
                              headers={"Sec-Fetch-Site": "cross-site",
                                       "Sec-Fetch-Mode": "navigate",
                                       "Sec-Fetch-Dest": "document"})
        assert status == 200 and body == b"ok", (status, body)
        assert seen == ["abc"], seen
        # a foreign origin is refused even on a registered route
        status, _ = h.fetch("GET", "/callback?code=abc", headers={"Origin": "http://evil.com"})
        assert status == 403, status
        # a one-shot route can be dropped again; a late request gets a plain 404
        h.server.remove_route("/callback")
        status, _ = h.fetch("GET", "/callback?code=abc")
        assert status == 404, status
        assert seen == ["abc"], seen
        h.server.add_route("/callback", callback)
        try:
            h.server.add_route("/callback", callback)
        except ValueError:
            pass
        else:
            raise AssertionError("the same path must not be claimed twice")
    finally:
        h.stop()
    print("eine Route laesst sich anmelden und wird genauso geprueft: OK")


def test_the_event_stream_pushes_events():
    h = Harness()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
        conn.request("GET", f"/events?token={h.bridge.window_token}",
                     headers={"Origin": h.origin})
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith("text/event-stream")
        deadline = time.monotonic() + 3.0
        while not h.bridge._clients and time.monotonic() < deadline:
            time.sleep(0.02)
        [client_id] = list(h.bridge._clients)
        h.bridge.get_state(client_id)
        h.core.emit(p.SoundAdded("a"))
        h.bridge.flush()
        line = response.fp.readline().decode("utf-8")
        while not line.startswith("data: "):
            line = response.fp.readline().decode("utf-8")
        payload = json.loads(line[len("data: "):])
        assert payload[0]["type"] == "SoundAdded", payload
        conn.close()
    finally:
        h.stop()
    print("der Ereignisstrom schickt Ereignisse: OK")


def test_a_held_notice_arrives_on_the_window_stream():
    h = Harness()
    try:
        h.bridge.hold_notice("Port 47800 war belegt.", "info")
        conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
        conn.request("GET", f"/events?token={h.bridge.window_token}",
                     headers={"Origin": h.origin})
        response = conn.getresponse()
        assert response.status == 200
        deadline = time.monotonic() + 3.0
        while not h.bridge._clients and time.monotonic() < deadline:
            time.sleep(0.02)
        h.bridge.flush()
        line = response.fp.readline().decode("utf-8")
        while not line.startswith("data: "):
            line = response.fp.readline().decode("utf-8")
        payload = json.loads(line[len("data: "):])
        assert payload[0]["type"] == "Notice", payload
        assert payload[0]["data"]["text"] == "Port 47800 war belegt.", payload
        conn.close()
    finally:
        h.stop()
    print("ein Start-Hinweis kommt im Fenster-Strom an: OK")


def test_request_clients_are_reaped_and_the_stream_is_ready_on_open():
    h = Harness()
    try:
        # no stream yet: one-shot requests must not pile up clients
        for _ in range(20):
            status, _ = h.fetch("GET", f"/state?token={h.bridge.window_token}",
                                headers={"Origin": h.origin})
            assert status == 200
            h.fetch("POST", f"/send?token={h.bridge.window_token}",
                    json.dumps(p.to_json(p.StopAll())),
                    {"Origin": h.origin, "Content-Type": "application/json"})
        # the server answers first and releases the borrowed client right after, in its
        # own thread - under load the last release can trail the answer by a moment
        deadline = time.monotonic() + 2.0
        while h.bridge._clients and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.bridge._clients == {}, h.bridge._clients

        # the stream's own client is ready as soon as it opens: no separate /state needed
        conn = http.client.HTTPConnection("127.0.0.1", h.server.port, timeout=5)
        conn.request("GET", f"/events?token={h.bridge.window_token}",
                     headers={"Origin": h.origin})
        response = conn.getresponse()
        assert response.status == 200
        deadline = time.monotonic() + 3.0
        while not h.bridge._clients and time.monotonic() < deadline:
            time.sleep(0.02)
        [client_id] = list(h.bridge._clients)
        assert h.bridge._clients[client_id].ready is True, "the stream must be ready"
        h.core.emit(p.SoundAdded("a"))
        h.bridge.flush()
        line = response.fp.readline().decode("utf-8")
        while not line.startswith("data: "):
            line = response.fp.readline().decode("utf-8")
        assert json.loads(line[len("data: "):])[0]["type"] == "SoundAdded"
        conn.close()
    finally:
        h.stop()
    print("Einmal-Clients werden abgeraeumt, der Strom ist sofort bereit: OK")


def test_a_reset_connection_is_quiet_but_a_real_fault_is_not():
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    keep = Keep(level=logging.DEBUG)
    logger = logging.getLogger("soundboard.webserver")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(keep)
    server = webserver.QuietThreadingHTTPServer(("127.0.0.1", 0),
                                                webserver.QuietThreadingHTTPServer)
    try:
        # the three shapes Agent A measured all arrive as OSError subclasses: no
        # traceback, only a DEBUG line
        for exc in (ConnectionResetError(10054, "verbindung zurueckgesetzt"),
                    BrokenPipeError(32, "rohr gebrochen"),
                    OSError(9, "anderer Socket-Fehler")):
            try:
                raise exc
            except OSError:
                server.handle_error(None, ("127.0.0.1", 1))
        assert records, "the debug note must not disappear entirely"
        assert all(r.levelno < logging.WARNING for r in records), records
        assert any("went away" in r.getMessage() for r in records)

        # a genuine fault still reaches the traceback - only OSError is swallowed
        stderr = io.StringIO()
        try:
            raise ValueError("echter Fehler")
        except ValueError:
            with contextlib.redirect_stderr(stderr):
                server.handle_error(None, ("127.0.0.1", 1))
        assert "echter Fehler" in stderr.getvalue(), stderr.getvalue()
    finally:
        server.server_close()
        logger.removeHandler(keep)
        logger.setLevel(old_level)
    print("abbrechende Clients schweigen, echte Fehler bleiben: OK")


def test_a_client_that_resets_creates_no_traceback():
    h = Harness()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            # the same three cases: the SSE stream, a /state answer and a static file
            h.reset("GET", f"/state?token={h.bridge.window_token}")
            h.reset("GET", "/assets/app.js")
            h.reset("GET", f"/events?token={h.bridge.window_token}")
            time.sleep(0.3)  # let the server threads reach their read
    finally:
        h.stop()
    text = stderr.getvalue()
    assert "Traceback" not in text and "Exception occurred" not in text, text
    print("abbrechende Clients erzeugen keinen Traceback: OK")


def test_a_real_route_error_stays_in_the_log():
    h = Harness()
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    keep = Keep(level=logging.ERROR)
    logger = logging.getLogger("soundboard.webserver")
    logger.addHandler(keep)
    try:
        def boom(handler, query, body):
            raise RuntimeError("kaputte Route")

        h.server.add_route("/kaputt", boom)
        status, body = h.fetch("GET", "/kaputt")
        assert status == 500 and b"route failed" in body, (status, body)
        assert any(r.levelno == logging.ERROR and r.exc_info for r in records), records
    finally:
        logger.removeHandler(keep)
        h.stop()
    print("ein echter Routenfehler bleibt im Log: OK")


def main():
    test_host_and_origin_must_be_our_own()
    test_static_paths_cannot_escape()
    test_the_page_needs_no_key_but_state_and_send_do()
    test_a_get_may_come_without_an_origin_but_a_foreign_one_is_refused()
    test_static_files_get_the_same_host_and_origin_checks()
    test_a_broken_content_length_is_refused_and_checks_come_first()
    test_the_view_may_not_delete_over_http_but_may_play()
    test_no_command_beyond_playback_and_music_reaches_the_view()
    test_state_carries_role_and_view_url_and_a_rotated_key_kills_the_stream()
    test_an_extra_route_can_be_registered()
    test_the_event_stream_pushes_events()
    test_a_held_notice_arrives_on_the_window_stream()
    test_request_clients_are_reaped_and_the_stream_is_ready_on_open()
    test_a_reset_connection_is_quiet_but_a_real_fault_is_not()
    test_a_client_that_resets_creates_no_traceback()
    test_a_real_route_error_stays_in_the_log()
    print("\nALL WEBSERVER CHECKS PASSED")


if __name__ == "__main__":
    main()
