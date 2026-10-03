"""/favicon.ico: the page ships the app icon, the server hands it out (night run A1.5).

Every start logged a 404 for /favicon.ico. Vite copies `webui/public/` into `webui/dist/`, so
the icon lives in `webui/public/favicon.ico` (the same file as `assets/icon.ico`) and the
page links it. The server test copies it into a temporary dist, never into `webui/dist`.
"""

import http.client
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-favicon-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

from soundboard import webserver  # noqa: E402

PUBLIC_ICON = ROOT / "webui" / "public" / "favicon.ico"


class _NoCore:
    def subscribe(self, cb):
        return lambda: None

    def send(self, cmd):
        pass

    def get_state(self, timeout=2.0):
        return {"protocol": 1}


class _NoAccess:
    def role_for(self, token):
        return None


def test_the_page_ships_the_app_icon():
    assert PUBLIC_ICON.is_file(), "webui/public/favicon.ico fehlt"
    assert PUBLIC_ICON.read_bytes() == (ROOT / "assets" / "icon.ico").read_bytes()
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="/favicon.ico"' in html
    print("Seite liefert das App-Icon mit: OK")


def test_the_server_answers_favicon_with_the_icon():
    from soundboard.bridgecore import BridgeCore

    dist = Path(_TMP) / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    shutil.copyfile(PUBLIC_ICON, dist / "favicon.ico")
    bridge = BridgeCore(_NoCore(), lambda client_id, messages: None, access=_NoAccess())
    server = webserver.SseServer(bridge, dist, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while server.port == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/favicon.ico")
        response = conn.getresponse()
        body = response.read()
        conn.close()
        assert response.status == 200, response.status
        assert response.getheader("Content-Type") == "image/x-icon"
        assert body == PUBLIC_ICON.read_bytes()
    finally:
        server.stop()
        thread.join(timeout=3)
    print("/favicon.ico liefert 200 mit dem Icon: OK")


def main():
    test_the_page_ships_the_app_icon()
    test_the_server_answers_favicon_with_the_icon()
    print("\nALL FAVICON CHECKS PASSED")


if __name__ == "__main__":
    main()
