"""Startwege, und die drei Faelle, die aus dem alten Brueckentest hierher wandern."""

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-startup-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.getLogger("soundboard").addHandler(logging.NullHandler())

from soundboard import main as app_main  # noqa: E402
from soundboard import webmain  # noqa: E402


def test_mode_from_arguments():
    assert app_main.parse_mode([]) == "web"
    assert app_main.parse_mode(["--webui"]) == "web"
    assert app_main.parse_mode(["--tk"]) == "tk"
    assert app_main.parse_mode(["--serve"]) == "serve"
    assert app_main.parse_mode(["--selftest"]) == "web", "--selftest wird vorher abgefangen"
    assert app_main.parse_mode(["--tk", "--serve"]) == "tk", "die alte Oberflaeche gewinnt"
    print("die Startwege werden aus den Argumenten gelesen: OK")


def test_origin_of():
    assert webmain.origin_of("http://127.0.0.1:47800/index.html") == "http://127.0.0.1:47800"
    assert webmain.origin_of("http://127.0.0.1:47800/assets/x.js?y=1") == "http://127.0.0.1:47800"
    assert webmain.origin_of("https://rareui.com/") == "https://rareui.com"
    assert webmain.origin_of(None) == ""
    assert webmain.origin_of("about:blank") == "about:"
    print("origin_of: OK")


def test_index_path_names_the_fix():
    original = webmain.dist_dir
    webmain.dist_dir = lambda: Path(_TMP) / "no-dist"
    try:
        webmain.index_path()
    except FileNotFoundError as exc:
        assert "npm run build" in str(exc)
    else:
        raise AssertionError("ein fehlendes webui/dist muss werfen")
    finally:
        webmain.dist_dir = original
    print("ein fehlendes webui/dist nennt die Abhilfe: OK")


def test_a_stale_build_is_reported_at_start():
    src = Path(_TMP) / "fresh-src"
    dist = Path(_TMP) / "fresh-dist"
    (src / "components").mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    (src / "components" / "App.tsx").write_text("x", encoding="utf-8")
    index = dist / "index.html"
    index.write_text("<html></html>", encoding="utf-8")

    # a build older than the source must be called out, with the rebuild command and the
    # file that triggered it (a rewrite of an unchanged file must explain itself)
    old = time.time() - 3600
    os.utime(index, (old, old))
    warning = webmain.stale_dist_warning(dist, src)
    assert warning and "npm run build" in warning and str(dist) in warning, warning
    assert str(src / "components" / "App.tsx") in warning, warning

    # a build at least as new as the source is fine
    now = time.time() + 60
    os.utime(index, (now, now))
    assert webmain.stale_dist_warning(dist, src) is None

    # edge case: dist is missing entirely
    missing = webmain.stale_dist_warning(Path(_TMP) / "no-dist", src)
    assert missing and "npm run build" in missing, missing

    # an installed exe has no source tree: never warn there
    assert webmain.stale_dist_warning(dist, Path(_TMP) / "no-src") is None

    # and the start really prints it (report_stale_dist is what webmain.run calls)
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    keep = Keep(level=logging.WARNING)
    logger = logging.getLogger("soundboard.webmain")
    original = webmain.stale_dist_warning
    webmain.stale_dist_warning = lambda: original(dist, src)
    logger.addHandler(keep)
    try:
        os.utime(index, (old, old))
        assert webmain.report_stale_dist() is not None
        assert any("npm run build" in r.getMessage() for r in records), records
    finally:
        webmain.stale_dist_warning = original
        logger.removeHandler(keep)
    print("eine veraltete webui/dist wird beim Start gemeldet: OK")


def test_a_failed_web_start_is_reported_and_falls_back():
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Keep(level=logging.ERROR)
    logging.getLogger("soundboard.main").addHandler(handler)
    try:
        shown = []

        def boom():
            raise FileNotFoundError("web interface not built: index.html is missing")

        assert app_main.run_webui(runner=boom, show=shown.append) is False
        assert len(shown) == 1 and "ohne --webui" in shown[0]
        assert records and records[-1].exc_info is not None  # Traceback in ruckus.log
        assert app_main.run_webui(runner=lambda: None, show=shown.append) is True
        assert len(shown) == 1
    finally:
        logging.getLogger("soundboard.main").removeHandler(handler)
    print("ein gescheiterter Web-Start meldet sich: OK")


def test_the_server_port_comes_from_the_config():
    from soundboard import config
    assert config._default_config()["server_port"] == 47800
    print("der Serverport steht in der Config: OK")


def test_an_update_run_brings_one_notice_with_whats_new():
    # the installer relaunches with --updated after a silent in-app update
    assert app_main.startup_notices([], "1.2.0") == []
    assert app_main.startup_notices(["--tk"], "1.2.0") == []
    [text] = app_main.startup_notices(["--updated"], "1.2.0")
    assert "1.2.0" in text
    assert "Web-Oberfläche" in text and "(klassisch)" in text, text
    [spotify] = app_main.startup_notices(["--updated"], "1.3.0")
    assert "1.3.0" in spotify and "Spotify" in spotify and "Premium" in spotify, spotify
    assert "Browser" in spotify, "the view link may steer too - say so"
    assert "Musik ins Mikrofon" in spotify, "the music bus is new in 1.3.0 too"
    [routing] = app_main.startup_notices(["--updated"], "1.4.0")
    assert "Playlists" in routing and "Alben" in routing, "F3: library, playlists, albums"
    assert "nur noch Spotify" in routing, "the music bus no longer carries game or PC sound"
    assert "Discord: Musik" in routing, "name the new dock button"
    assert "Seitenleiste" in routing and "Kürzen" in routing, "pins and the trim editor are new in 1.4.0"
    assert "gleicht" in routing, "the bus levels itself: the Spotify slider is only for your ears"
    [plain] = app_main.startup_notices(["--updated"], "9.9.9")
    assert "9.9.9" in plain and "Web-Oberfläche" not in plain, "no news without an entry"
    assert app_main.parse_mode(["--updated"]) == "web", "--updated changes no start path"
    print("nach einem Update ein Hinweis, mit Neuerungen falls vorhanden: OK")


def main():
    test_an_update_run_brings_one_notice_with_whats_new()
    test_mode_from_arguments()
    test_origin_of()
    test_index_path_names_the_fix()
    test_a_stale_build_is_reported_at_start()
    test_a_failed_web_start_is_reported_and_falls_back()
    test_the_server_port_comes_from_the_config()
    print("\nALL STARTUP CHECKS PASSED")


if __name__ == "__main__":
    main()
