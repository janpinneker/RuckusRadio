"""Visual check: launch the window on the fake core (tests/core_fakes) with a temp
data dir, screenshot it, exit.
Usage: python tests/screenshot_redesign.py [--empty] [--search QUERY] [--out PATH]"""

import argparse
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

FIXTURES = core_fakes.FIXTURES
OUT = Path(__file__).parent / "output"
SEED = [
    ("Airhorn", "ctrl+alt+1", False),
    ("Bruh", "ctrl+alt+2", True),
    ("Sad Trombone", None, False),
    ("Tactical Nuke Incoming", "ctrl+alt+shift+n", True),
    ("Wow", None, False),
    ("Rizz", "f9", False),
    ("Ez Clap", None, True),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--empty", action="store_true")
    ap.add_argument("--search", default="")
    ap.add_argument("--playing", action="store_true")
    ap.add_argument("--size", default="")
    ap.add_argument("--out", default=str(OUT / "redesign.png"))
    args = ap.parse_args()

    c, _ = core_fakes.make_core()
    app = RuckusRadioApp(c)
    c.start()
    app.refresh_state()
    if args.size:
        app.geometry(args.size)
    if not args.empty:
        for name, hotkey, with_image in SEED:
            app.add_sound(name=name, audio_path=FIXTURES / "test_tone.mp3",
                           icon_path=FIXTURES / "test_image.png" if with_image else None)
            if hotkey:
                app.set_hotkey(app.sounds()[-1], hotkey)
        if args.playing:
            sid = app.sounds()[1]["id"]
            app.play_sound(sid)  # the fake engine never ends it: the ring stays on
    if args.search:
        app.board.set_query(args.search)

    def grab():
        app.lift()
        app.attributes("-topmost", True)
        app.update()
        app.after(400, shoot)

    def shoot():
        x, y = app.winfo_rootx(), app.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height()), all_screens=True)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        img.save(args.out)
        print("saved", args.out, img.size)
        app.attributes("-topmost", False)
        app.destroy()

    app.after(1500, grab)
    app.mainloop()


if __name__ == "__main__":
    main()
