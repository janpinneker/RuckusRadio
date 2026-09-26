"""`RuckusRadio.exe --webui`: the same core as main.py, drawn in a pywebview window
instead of Tk (spec B0 §6). Tk and the web interface never run at the same time;
main.py holds the single-instance lock around this.

Security: only webui/dist from the app itself is loaded (served by pywebview's local
server), any navigation to another origin is sent back home, external links open in
the default browser, dev tools are off.

Not yet here (B2): the first-run assistant and the "really quit while you carry the
microphone?" question - the Tk interface keeps both."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .core import create_core
from .hotkeys import HotkeyManager
from .webbridge import JsApi, WebBridge

log = logging.getLogger(__name__)

TITLE = "Ruckus Radio"
BACKGROUND = "#101014"  # theme.BG: no white flash before the page paints


def dist_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "webui" / "dist"


def index_path() -> Path:
    index = dist_dir() / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"web interface not built: {index} is missing (run `npm ci && npm run build` in webui/)")
    return index


def origin_of(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else f"{parts.scheme}:"


def run(core=None, on_window=None) -> None:
    import webview  # only the web interface needs pywebview (and pythonnet)

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = False

    if core is None:
        core = create_core(hotkey_manager=HotkeyManager())
    bridge = WebBridge(core)  # subscribes before core.start()
    window = webview.create_window(
        TITLE, url=str(index_path()), js_api=JsApi(bridge),
        width=1280, height=820, min_size=(960, 620), background_color=BACKGROUND,
    )
    bridge.attach(window)

    home: dict[str, str] = {}

    def keep_home() -> None:
        url = window.get_current_url()
        if "url" not in home:
            home["url"], home["origin"] = url, origin_of(url)
            return
        if origin_of(url) != home["origin"]:
            log.warning("blocked navigation to %s", url)
            bridge.page_reloaded()  # the reload below re-runs the page's own startup
            window.load_url(home["url"])

    window.events.loaded += keep_home
    if on_window is not None:
        on_window(window, bridge, core)

    core.start()
    bridge.start_pump()
    try:
        webview.start(debug=False, http_server=True, private_mode=False,
                      storage_path=str(config.get_app_data_dir() / "webview"))
    finally:
        bridge.close()
        core.shutdown()  # hooks off, sounds stopped, microphone handed back, config flushed
