"""Visual check: screenshot the 5 assistant steps and the main window's rail, on the
fake core (tests/core_fakes: a virtual cable is always "found", no audio hardware).
Writes tests/output/onboarding-<k>.png and tests/output/rail-assistant.png."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["RUCKUS_DATA_DIR"] = tempfile.mkdtemp(prefix="ruckus-shot-")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import ImageGrab  # noqa: E402

import core_fakes  # noqa: E402
from soundboard.gui import RuckusRadioApp  # noqa: E402
from soundboard.onboarding import OnboardingWindow  # noqa: E402

FIXTURES = core_fakes.FIXTURES
OUT = Path(__file__).parent / "output"


def grab(win, path: Path) -> None:
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    img = ImageGrab.grab(bbox=(x, y, x + win.winfo_width(), y + win.winfo_height()), all_screens=True)
    img.save(path)
    print("saved", path, img.size)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c, _ = core_fakes.make_core()
    app = RuckusRadioApp(c)
    app.withdraw()
    c.start()
    app.refresh_state()
    win = OnboardingWindow(app, on_done=lambda _c: None)
    win.attributes("-topmost", True)

    def prepare(k):
        if k == 5:
            app.add_sound(name="Airhorn", audio_path=FIXTURES / "test_tone.mp3",
                          icon_path=FIXTURES / "test_image.png")
            app.set_hotkey(app.sounds()[0], "ctrl+alt+1")
        win.show_step(k)

    def shoot(k):
        prepare(k)
        win.after(500, lambda: (grab(win, OUT / f"onboarding-{k}.png"),
                                win.after(100, found if k == 3 else shoot, k + 1) if k < 5
                                else win.after(100, rail)))

    def found(k):
        # step 3 was shot before the check; the fake backend always finds the cable
        win.check_voicemeeter()
        win.after(400, lambda: (grab(win, OUT / "onboarding-3-found.png"), win.after(100, shoot, k)))

    def rail():
        win.destroy()
        app.deiconify()
        app.attributes("-topmost", True)
        app.after(900, lambda: (grab(app, OUT / "rail-assistant.png"), app.destroy()))

    app.after(1200, shoot, 1)
    app.mainloop()


if __name__ == "__main__":
    main()
