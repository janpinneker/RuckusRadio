"""Native Dateidialoge: jeder Dialog haengt an der Wurzel seines eigenen Threads.

Bibliothek und Sammlungen haben je ein FileDialogs mit eigenem Thread und eigener
Tk-Wurzel. Ein Dialog ohne `parent` nimmt Tkinters globale Standardwurzel - die des
Threads, der zuerst ein Tk() erzeugt hat. Aus dem anderen Thread scheitert er dann mit
"main thread is not in main loop" (Log 2026-10-03, Icon-Dialog nach einem Cover-Dialog).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard.filedialogs import FileDialogs  # noqa: E402

KINDS = ("sound", "icon", "pack", "audio_target", "target")


class FakeFiledialog:
    seen: list = []

    @classmethod
    def askopenfilename(cls, **kwargs):
        cls.seen.append(kwargs)
        return "C:/x/a.png"

    @classmethod
    def asksaveasfilename(cls, **kwargs):
        cls.seen.append(kwargs)
        return "C:/x/a.mp3"


def test_every_dialog_hangs_on_the_threads_own_root():
    root = object()
    for kind in KINDS:
        FakeFiledialog.seen.clear()
        FileDialogs._open(FakeFiledialog, {"default_name": "x"}, kind, parent=root)
        assert FakeFiledialog.seen and FakeFiledialog.seen[0].get("parent") is root, (kind, FakeFiledialog.seen)
    print("jeder Dialog haengt an der eigenen Wurzel: OK")


def test_two_dialog_threads_do_not_share_tks_default_root():
    """Live: two FileDialogs threads (as library + collections have), each asks once; the
    second must not reach into the first thread's Tk. Tk dialogs cannot open headless, so
    the probe swaps `_open` for a call into the parent's interpreter - which is exactly
    what a real dialog does first."""
    original = FileDialogs._open
    results = []

    def probe(filedialog, kwargs, kind, parent=None):
        import tkinter
        target = parent if parent is not None else tkinter._get_default_root("probe")
        return target.tk.call("info", "patchlevel")

    FileDialogs._open = staticmethod(probe)
    try:
        first, second = FileDialogs(timeout=10), FileDialogs(timeout=10)
        results.append(first.pick_icon_file())
        results.append(second.pick_icon_file())
    finally:
        FileDialogs._open = staticmethod(original)
    assert all(results), results  # None = the dialog thread logged "file dialog failed"
    print("zwei Dialog-Threads stoeren sich nicht: OK")


def main():
    test_every_dialog_hangs_on_the_threads_own_root()
    test_two_dialog_threads_do_not_share_tks_default_root()
    print("\nALL FILEDIALOGS CHECKS PASSED")


if __name__ == "__main__":
    main()
