"""Entry point: `python -m soundboard.main`.

The app core (soundboard.core.create_core) owns everything that is not drawing:
it resolves the audio devices, builds the mixer, registers the hotkeys, preloads
sounds and backfills loudness in its start steps. The window subscribes to the
core *before* core.start(), so no start notice (config reset, no output device)
is lost, then reads the first snapshot. Closing goes through core.shutdown()
(hooks off, sounds stopped, microphone handed back, config flushed) before the
window is destroyed. A named mutex keeps a second instance from fighting over
the cables and the microphone.

Until `onboarding_completed` is set, the first-run assistant opens instead of the
board (audio + hotkeys are already live so its sound/hotkey steps work); closing
it early shows the board and it returns on the next start.

`--webui` opens the pywebview window of soundboard.webmain instead of Tk (probe build B0)."""

from __future__ import annotations

import logging
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from soundboard import config, measure, singleinstance
from soundboard.core import create_core
from soundboard.gui import RuckusRadioApp
from soundboard.hotkeys import HotkeyManager


log = logging.getLogger(__name__)

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3


def configure_logging(data_dir: Path) -> RotatingFileHandler:
    """<data dir>/ruckus.log (1 MB x 3): the windowed exe has no console, so this
    file is the only place errors end up. Mirrors to stderr only when one exists."""
    handler = RotatingFileHandler(data_dir / "ruckus.log", maxBytes=LOG_MAX_BYTES,
                                  backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        root.addHandler(logging.StreamHandler())
    return handler


ALREADY_RUNNING = "Ruckus Radio läuft schon – schau in der Taskleiste nach."

WEBUI_FAILED = """Die Web-Oberfläche konnte nicht starten:
{}

Starte Ruckus Radio ohne --webui."""


def _show_error(text: str) -> None:
    root = tk.Tk()  # hidden parent, as for ALREADY_RUNNING
    root.withdraw()
    messagebox.showerror("Ruckus Radio", text, parent=root)
    root.destroy()


def run_webui(runner=None, show=None) -> bool:
    """`--webui` (probe build B0). A missing webui/dist, pythonnet or WebView2 - or any
    other failure - is logged and shown; the windowed exe never ends silently."""
    try:
        if runner is None:
            from soundboard import webmain

            runner = lambda: webmain.run(on_window=measure.attach_web)  # noqa: E731
        runner()
        return True
    except Exception as exc:  # noqa: BLE001 - every start failure must reach the user
        log.exception("web interface failed to start")
        (show or _show_error)(WEBUI_FAILED.format(exc))
        return False


def build_app(core=None, with_hotkeys: bool = True) -> RuckusRadioApp:
    if core is None:
        core = create_core(hotkey_manager=HotkeyManager() if with_hotkeys else None)
    app = RuckusRadioApp(core)  # subscribes before the core starts
    app.report_callback_exception = lambda *exc_info: log.error("Tk callback failed", exc_info=exc_info)
    app.protocol("WM_DELETE_WINDOW", lambda: on_close(app))
    core.start()
    app.refresh_state()
    return app


def on_close(app: RuckusRadioApp) -> None:
    # While Ruckus Radio carries the microphone, quitting silently takes the mic away
    # from the voice chat mid-call. Offer to minimise instead (no tray icon: that would
    # cost an extra dependency in the onefile build - the window stays in the taskbar).
    mixer = (app.snapshot.get("devices") or {}).get("mixer") or {}
    if mixer.get("mic_active"):
        if not messagebox.askyesno(
            "Ruckus Radio beenden?",
            "Discord und Steam hören dein Mikrofon nur, solange Ruckus Radio läuft.\n\n"
            "Wirklich beenden? „Nein“ minimiert das Fenster stattdessen.",
            parent=app,
        ):
            app.iconify()
            return
    app.shutdown_and_close()  # hooks off, sounds stopped, mic handed back, config flushed


def show_first_screen(app: RuckusRadioApp) -> None:
    if "settings" not in app.snapshot:
        app.refresh_state()  # a slow start: ask once more
    settings = app.snapshot.get("settings")
    if settings is None:
        log.warning("no core state yet, not deciding about the assistant")
        return  # never reopen the assistant just because the core answered late
    if not settings.get("onboarding_completed"):
        app.open_onboarding()


SELFTEST_RESULT_PATH = Path(tempfile.gettempdir()) / "ruckus-selftest.txt"


def run_selftest() -> int:
    """`--selftest`: proves ffmpeg is resolved and decoding works, without any
    bundled fixture (the onefile build ships no test data). Generates a 0.2s
    sine WAV, converts it to MP3 via extract_audio (the same path add_sound
    uses for non-MP3 input), decodes the MP3, and writes the result to
    %TEMP%\\ruckus-selftest.txt since a windowed onefile exe has no console."""
    import math
    import struct
    import wave

    from soundboard.audio import FFMPEG_PATH, decode_audio, extract_audio

    tmp = Path(tempfile.gettempdir())
    wav_path = tmp / "ruckus-selftest-input.wav"
    mp3_path = tmp / "ruckus-selftest-input.mp3"
    samplerate = 48000
    duration_s = 0.2
    n_frames = int(samplerate * duration_s)

    try:
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(samplerate)
            for i in range(n_frames):
                value = int(32767 * 0.3 * math.sin(2 * math.pi * 440.0 * i / samplerate))
                w.writeframesraw(struct.pack("<h", value))

        extract_audio(wav_path, mp3_path, fmt="mp3")
        decoded = decode_audio(mp3_path)
        if decoded.samples.shape[0] == 0:
            raise RuntimeError("decoded 0 frames")

        ffmpeg_used = FFMPEG_PATH or "system PATH"
        message = f"SELFTEST OK {ffmpeg_used}"
        SELFTEST_RESULT_PATH.write_text(message, encoding="utf-8")
        print(message)
        return 0
    except Exception as exc:  # noqa: BLE001 - report every failure mode the same way
        message = f"SELFTEST FAILED {exc!r}"
        SELFTEST_RESULT_PATH.write_text(message, encoding="utf-8")
        print(message, file=sys.stderr)
        return 1
    finally:
        wav_path.unlink(missing_ok=True)
        mp3_path.unlink(missing_ok=True)


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        sys.exit(run_selftest())
    configure_logging(config.get_app_data_dir())
    lock = singleinstance.acquire()
    if lock is None:
        log.info("second instance refused")
        root = tk.Tk()  # hidden parent: without it Tk shows an empty "tk" window
        root.withdraw()
        messagebox.showinfo("Ruckus Radio", ALREADY_RUNNING, parent=root)
        root.destroy()
        return
    try:
        if "--webui" in sys.argv[1:]:
            log.info("Ruckus Radio starting (web interface)")
            run_webui()
            return
        log.info("Ruckus Radio starting")
        app = build_app()
        measure.attach_tk(app)  # no-op unless RUCKUS_MEASURE_DIR is set (probe build B0)
        show_first_screen(app)
        app.mainloop()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
