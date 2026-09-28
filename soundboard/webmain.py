"""`RuckusRadio.exe`: the local server plus a pywebview window that shows it.

The window is a SHELL. It loads the same URL the browser loads, so the page speaks one
transport everywhere (spec §1). There is no pywebview JS API any more: everything goes
through the server, which means the window passes the same Host, Origin and key checks
as any other client.

`--serve` (serve_only) skips the window entirely.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config, webserver
from .bridgecore import BridgeCore
from .protocol import UpdateReady
from .updates import UPDATE_LAUNCH_FAILED, launch_installer

log = logging.getLogger(__name__)

TITLE = "Ruckus Radio"
BACKGROUND = "#101014"  # theme.BG: no white flash before the page paints
announced: dict[str, str] = {}


def announced_url() -> str:
    return announced.get("url", "")


def dist_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "webui" / "dist"


def src_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "webui" / "src"


def index_path() -> Path:
    index = dist_dir() / "index.html"
    if not index.is_file():
        raise FileNotFoundError(
            f"web interface not built: {index} is missing (run `npm ci && npm run build` in webui/)")
    return index


REBUILD = "cd webui && npm run build"


def newest_source_file(src: Path) -> tuple[Path, float] | None:
    """The newest file under `src` and its mtime, or None when it is not there.

    An installed exe ships no source tree; then there is nothing to compare against.
    """
    if not src.is_dir():
        return None
    newest = None
    for path in src.rglob("*"):
        if path.is_file():
            mtime = path.stat().st_mtime
            if newest is None or mtime > newest[1]:
                newest = (path, mtime)
    return newest


def stale_dist_warning(dist: Path | None = None, src: Path | None = None) -> str | None:
    """Warn when webui/dist is older than webui/src - nothing else builds it locally.

    Agent A walked straight into this: the built page still spoke the removed pywebview
    bridge, the server saw no contact, and no one said a word. Only build.ps1 rebuilds
    for a release; a local start shows whatever sits there. Returns the warning text, or
    None when the build is current (or there is no source tree to compare).

    The message names the file that triggered it. That matters, because the mtime check
    can fire on a file whose CONTENT did not change: tools/gen_protocol_ts.py rewrites
    protocol.gen.json whenever it runs (build.ps1 asks it to), even when the protocol is
    identical. Naming the file lets such a case explain itself instead of confusing.
    """
    dist = dist_dir() if dist is None else Path(dist)
    src = src_dir() if src is None else Path(src)
    index = dist / "index.html"
    if not index.is_file():
        return (f"Die Web-Oberfläche ist nicht gebaut: {index} fehlt, Ruckus Radio kann sie "
                f"nicht anzeigen. Bauen mit: {REBUILD}")
    newest = newest_source_file(src)
    if newest is None:
        return None
    newest_path, newest_mtime = newest
    built = index.stat().st_mtime
    if built >= newest_mtime:
        return None
    stamp = lambda value: time.strftime('%Y-%m-%d %H:%M', time.localtime(value))  # noqa: E731
    return (f"Die Web-Oberfläche in {dist} ist älter als der Quelltext in {src}: "
            f"index.html ist von {stamp(built)}, die jüngste Datei {newest_path} von "
            f"{stamp(newest_mtime)}. Ruckus Radio zeigt dir damit eine veraltete Oberfläche "
            f"(der Hinweis kann auch von einer nur neu geschriebenen, inhaltsgleichen Datei "
            f"kommen - die genannte Datei zeigt es). Bitte neu bauen mit: {REBUILD}")


def report_stale_dist() -> str | None:
    """Say it at start, in clear words - and never let it block the start."""
    try:
        warning = stale_dist_warning()
    except OSError as exc:  # a warning must not be able to stop the app
        log.debug("could not check the interface build: %s", exc)
        return None
    if warning:
        log.warning("%s", warning)
    return warning


def origin_of(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else f"{parts.scheme}:"


def server_port() -> int:
    try:
        return int(config.load_config().get("server_port") or webserver.DEFAULT_PORT)
    except (TypeError, ValueError):
        return webserver.DEFAULT_PORT


def build_server(bridge: BridgeCore):
    index_path()  # fail early and with a clear message when webui/dist is missing
    return webserver.SseServer(bridge, dist_dir(), port=server_port())


def make_update_handler(core, launch, close):
    """UpdateReady -> start the installer, then close the host the same way a normal
    quit does (spec: same flow as the Tk window's launch_update/UpdateReady branch).
    Factored out so it is testable without pywebview: `launch(installer_path) -> bool`
    and `close()` are injected. Every other event is ignored (Core.subscribe delivers
    every event type to every subscriber)."""
    def handle(event) -> None:
        if not isinstance(event, UpdateReady):
            return
        if launch(event.installer_path):
            close()
        else:
            core.notice(UPDATE_LAUNCH_FAILED.format(path=event.installer_path), "error")
    return handle


def serve_message(url: str, fell_back_from: int | None) -> str:
    """What `--serve` prints: there is no window, so the console is the only place the
    link can go. Never the log - the key is part of the link."""
    lines = ["Ruckus Radio läuft ohne Fenster.",
             f"Browser-Link (abspielen und zusehen): {url}"]
    if fell_back_from is not None:
        lines.append(f"Port {fell_back_from} war belegt; die Spotify-Anmeldung braucht "
                     f"{fell_back_from}.")
    lines.append("Beenden mit Strg+C.")
    return "\n".join(lines)


def _parent_console():
    """The installed exe is windowed (build.spec console=False), so sys.stdout is None.
    Started from a terminal, borrow that terminal's console; otherwise there is none."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if not kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        return None
    try:
        return open("CONOUT$", "w", encoding=f"cp{kernel32.GetConsoleOutputCP()}",
                    errors="replace")
    except (OSError, LookupError):
        return None


def console_show(text: str, stream=None) -> bool:
    """Print to the console if there is one; False when there is nowhere to print."""
    stream = stream or sys.stdout or _parent_console()
    if stream is None:
        return False
    print(text, file=stream, flush=True)
    return True


def run(core=None, on_window=None, serve_only: bool = False, show=console_show,
        startup_notices=()) -> None:
    from .core import create_core
    from .hotkeys import HotkeyManager

    report_stale_dist()  # a local start builds nothing; say when the page is old
    if core is None:
        core = create_core(hotkey_manager=HotkeyManager())
    bridge = BridgeCore(core, lambda client_id, messages: server.deliver(client_id, messages),
                        access=None)  # logs in as the window role below
    server = build_server(bridge)
    # The Spotify return leg is a route of this server (spec §11), not a second listener.
    spotify = getattr(core, "spotify", None)
    if spotify is not None:
        spotify.attach_server(server)
    player = getattr(core, "spotify_player", None)
    if player is not None:
        player.attach_viewers(bridge.viewer_count)  # poll Spotify only while a page watches
    bridge.add_client("window", bridge.window_token)
    threading.Thread(target=server.serve_forever, name="ruckus-server", daemon=True).start()
    _wait_for_port(server)
    # held for the page's first window stream: the page never runs as client "window"
    if server.fell_back:
        bridge.hold_notice(f"Port {server.requested_port} war belegt; die Oberfläche "
                           f"läuft auf {server.port}. Die Spotify-Anmeldung braucht "
                           f"{server.requested_port}.", "info")
    for text in startup_notices:
        bridge.hold_notice(text, "info")
    announced["url"] = server.url_for(bridge.view_token)

    try:
        if serve_only:
            core.start()
            bridge.start_pump()
            show(serve_message(announced["url"],
                               server.requested_port if server.fell_back else None))
            stop_serving = threading.Event()
            unsubscribe = core.subscribe(make_update_handler(core, launch_installer, stop_serving.set))
            try:
                while not stop_serving.is_set():
                    time.sleep(1.0)
            finally:
                unsubscribe()
            return
        run_window(bridge, server, core, on_window)
    finally:
        bridge.close()
        server.stop()
        core.shutdown()


def _wait_for_port(server, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    if server.port == 0:
        raise RuntimeError("the local server did not bind")


def run_window(bridge, server, core, on_window) -> None:
    import webview  # only the window path needs pywebview (and pythonnet)

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = False
    allowed = f"http://127.0.0.1:{server.port}"
    window = webview.create_window(
        TITLE, url=server.url_for(bridge.window_token), width=1280, height=820,
        min_size=(960, 620), background_color=BACKGROUND,
    )
    keep_home(window, allowed, bridge)
    # before core.start(): an UpdateReady that races the very first StateChanged must
    # still close the window (same reasoning as gui.py subscribing before core.start()).
    unsubscribe = core.subscribe(make_update_handler(core, launch_installer, window.destroy))
    if on_window is not None:
        on_window(window, bridge, core)
    core.start()
    bridge.start_pump()
    try:
        webview.start(debug=False, private_mode=False,
                      storage_path=str(config.get_app_data_dir() / "webview"))
    finally:
        unsubscribe()
        window.destroy()


def keep_home(window, allowed_origin: str, bridge) -> None:
    """Send any navigation away from the interface back home."""
    home: dict[str, str] = {}

    def on_loaded() -> None:
        url = window.get_current_url()
        if "url" not in home:
            home["url"], home["origin"] = url, origin_of(url)
            return
        if origin_of(url) not in ("", allowed_origin, home["origin"]):
            log.warning("blocked navigation away from the interface")
            bridge.client_unready("window")
            window.load_url(home["url"])

    window.events.loaded += on_loaded
