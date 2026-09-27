"""`--serve` has no window: the valid browser link must reach the console. It never goes
to the log (the key is in the link, and the log is what users attach to bug reports)."""

import os
import socket
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-webmain-serve-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soundboard import config, webmain  # noqa: E402
import core_fakes  # noqa: E402


class _Shown(Exception):
    """Raised by the fake console so the serve loop ends right after the link."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_message_names_the_link_and_what_it_may_do():
    text = webmain.serve_message("http://127.0.0.1:47800/#t=abc", fell_back_from=None)
    assert "http://127.0.0.1:47800/#t=abc" in text
    assert "abspielen" in text, "the link only plays and watches; say so"
    assert "Strg+C" in text
    assert "belegt" not in text
    print("the serve message carries the link and how to stop: OK")


def test_the_message_says_when_the_port_fell_back():
    text = webmain.serve_message("http://127.0.0.1:47801/#t=abc", fell_back_from=47800)
    assert "47800" in text and "belegt" in text
    assert "Spotify" in text, "the Spotify login needs the configured port"
    print("the serve message warns about a fallen-back port: OK")


def test_serve_prints_the_valid_view_link_to_the_console():
    port = _free_port()
    # The core saves its store to config.json (access.py creates the view key), and
    # webmain reads the port from there: hand the port in through the store.
    data = config._default_config()
    data["server_port"] = port
    core, _events = core_fakes.make_core(store_data=data)
    shown: list[str] = []

    def show(text):
        shown.append(text)
        raise _Shown

    try:
        webmain.run(core=core, serve_only=True, show=show)
    except _Shown:
        pass
    assert len(shown) == 1, shown
    url = webmain.announced_url()
    assert url.startswith(f"http://127.0.0.1:{port}/#t="), url
    assert url in shown[0], "the console shows exactly the announced link"
    token = url.split("#t=", 1)[1]
    assert token == core.store.data.get("view_token"), \
        "the console shows the view key (plays only), never the window's"
    print("--serve prints the valid view link to the console: OK")


def test_console_show_writes_to_the_given_stream():
    import io
    out = io.StringIO()
    assert webmain.console_show("Link: x", stream=out) is True
    assert out.getvalue() == "Link: x\n"
    print("console_show writes to a real stream: OK")


def test_console_show_without_any_console_stays_quiet():
    # The installed exe is windowed (build.spec console=False): sys.stdout is None.
    # Started from a terminal it borrows the parent's console; with none, it says False.
    saved_stdout, saved_parent = sys.stdout, webmain._parent_console
    webmain._parent_console = lambda: None
    try:
        sys.stdout = None
        result = webmain.console_show("Link: x")
    finally:
        sys.stdout, webmain._parent_console = saved_stdout, saved_parent
    assert result is False
    print("console_show without a console returns False, no crash: OK")


def test_console_show_borrows_the_parent_console_when_windowed():
    import io
    borrowed = io.StringIO()
    saved_stdout, saved_parent = sys.stdout, webmain._parent_console
    webmain._parent_console = lambda: borrowed
    try:
        sys.stdout = None
        result = webmain.console_show("Link: x")
    finally:
        sys.stdout, webmain._parent_console = saved_stdout, saved_parent
    assert result is True and borrowed.getvalue() == "Link: x\n"
    print("console_show borrows the parent console in the windowed exe: OK")


def test_startup_notices_are_held_for_the_window():
    port = _free_port()
    data = config._default_config()
    data["server_port"] = port
    core, _events = core_fakes.make_core(store_data=data)
    held: list[tuple[str, str]] = []
    original = webmain.BridgeCore.hold_notice
    webmain.BridgeCore.hold_notice = lambda self, text, level="info": held.append((text, level))

    def show(_text):
        raise _Shown

    try:
        webmain.run(core=core, serve_only=True, show=show, startup_notices=["aktualisiert"])
    except _Shown:
        pass
    finally:
        webmain.BridgeCore.hold_notice = original
    assert held == [("aktualisiert", "info")], held
    print("Start-Hinweise warten auf das Fenster: OK")


def main():
    test_startup_notices_are_held_for_the_window()
    test_console_show_writes_to_the_given_stream()
    test_console_show_without_any_console_stays_quiet()
    test_console_show_borrows_the_parent_console_when_windowed()
    test_the_message_names_the_link_and_what_it_may_do()
    test_the_message_says_when_the_port_fell_back()
    test_serve_prints_the_valid_view_link_to_the_console()
    print("\nALL WEBMAIN SERVE CHECKS PASSED")


if __name__ == "__main__":
    main()
