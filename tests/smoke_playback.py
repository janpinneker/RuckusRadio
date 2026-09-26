"""Real-audio smoke against the real, threaded core (default output = monitor; no
virtual cable -> monitor-only). Builds the app via soundboard.main (no global
hotkeys) on a temp data dir, adds 2 fixture sounds through the core, fires
overlapping plays, screenshots the ringed tiles, checks rings clear after the ~2s
tones end, checks stop-all mid-playback, screenshots the volume dialog, then
closes through core.shutdown().
Usage: venv/Scripts/python.exe tests/smoke_playback.py"""

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ["RUCKUS_DATA_DIR"] = tempfile.mkdtemp(prefix="ruckus-smoke-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import ImageGrab  # noqa: E402

from soundboard import protocol as p  # noqa: E402
from soundboard.main import build_app  # noqa: E402
from soundboard.widgets import VolumeDialog  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
OUT = Path(__file__).parent / "output"
WAIT_S = 15
errors: list[str] = []


def grab(widget, path: Path):
    widget.update()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    img = ImageGrab.grab(bbox=(x, y, x + widget.winfo_width(), y + widget.winfo_height()), all_screens=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print("saved", path, img.size)


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        errors.append(msg)


def main():
    app = build_app(with_hotkeys=False)
    app.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
    engine = app.core.engine
    print("status:", app.dock.status_label.cget("text"))
    app.attributes("-topmost", True)
    ids: dict[str, str] = {}

    def wait_for(cond, then, what, started=None):
        """The core is threaded: poll the snapshot/engine from the Tk loop."""
        started = started or time.monotonic()
        if cond():
            then()
        elif time.monotonic() - started > WAIT_S:
            check(False, f"timed out waiting for: {what}")
            finish()
        else:
            app.after(100, wait_for, cond, then, what, started)

    def by_name(name):
        return next((s for s in app.sounds() if s["name"] == name), None)

    def step_add():
        app.add_sound("Airhorn", FIXTURES / "test_tone.mp3", None)
        app.add_sound("Bruh", FIXTURES / "test_tone.mp4", FIXTURES / "test_image.png")
        wait_for(lambda: by_name("Airhorn") and by_name("Bruh"), step_added, "two sounds added")

    def step_added():
        ids["a"], ids["b"] = by_name("Airhorn")["id"], by_name("Bruh")["id"]
        app.set_hotkey(by_name("Airhorn"), "ctrl+alt+1")
        wait_for(lambda: engine.is_loaded(ids["a"]) and engine.is_loaded(ids["b"]),
                 step_play, "both sounds preloaded")

    def step_play():
        check(by_name("Airhorn").get("hotkey") == "ctrl+alt+1", "hotkey saved through the core")
        app.play_sound(ids["a"])
        app.after(120, app.play_sound, ids["b"])
        app.after(200, app.play_sound, ids["a"])  # same sound overlapping itself
        app.after(700, step_ringed)

    def step_ringed():
        check(engine.playing_ids() == {ids["a"], ids["b"]}, "engine tracks both playing ids")
        check(len(engine._active) == 3, f"3 overlapping playbacks active (got {len(engine._active)})")
        check(app.tile_for(ids["a"]).disc.state_name == "playing"
              and app.tile_for(ids["b"]).disc.state_name == "playing", "both tiles ringed")
        grab(app, OUT / "playing.png")
        app.after(2600, step_finished)

    def step_finished():
        check(not app.playing_ids and not engine.playing_ids(),
              f"rings cleared after tones finished (app={app.playing_ids}, engine={engine.playing_ids()})")
        check(app.tile_for(ids["a"]).disc.state_name != "playing", "tile A ring off")
        app.play_sound(ids["a"])
        app.play_sound(ids["b"])
        app.after(300, step_stop)

    def step_stop():
        app.stop_all()
        wait_for(lambda: not engine.playing_ids() and not app.playing_ids, step_stopped,
                 "stop-all clears everything")

    def step_stopped():
        check(all(app.tile_for(s).disc.state_name != "playing" for s in ids.values()), "stop-all clears rings")
        dlg = VolumeDialog(app, app, by_name("Bruh"))
        dlg.attributes("-topmost", True)
        dlg.slider.set(1.5)
        dlg._on_slide(1.5)  # dB
        app.after(600, step_dialog, dlg)

    def step_dialog(dlg):
        grab(dlg, OUT / "volume_dialog.png")
        dlg._preview()
        wait_for(lambda: engine.is_playing(ids["b"]), lambda: step_apply(dlg), "Probehören plays the sound")

    def step_apply(dlg):
        dlg._apply()
        wait_for(lambda: by_name("Bruh")["volume"] != 1.0, step_applied, "Übernehmen saved the volume")

    def step_applied():
        check(by_name("Bruh")["volume"] > 1.0, f"Übernehmen saved volume {by_name('Bruh')['volume']:.2f}")
        app.after(2500, finish)

    def finish():
        app.stop_all()
        check(app.core.shutdown(), "core.shutdown finished in time")
        app.destroy()

    app.after(1200, step_add)
    app.mainloop()
    if errors:
        print("\nSMOKE FAILED:\n" + "\n".join(errors))
        sys.exit(1)
    print("\nSMOKE PASSED")


if __name__ == "__main__":
    main()
