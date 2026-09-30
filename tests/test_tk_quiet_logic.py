"""Test windows must never take the screen: every Tk window a test builds is invisible,
has no taskbar button and cannot force itself to the front (Jan plays fullscreen games
while suites run). Also guards that every test building real Tk windows installs it."""

import re
import sys
import tkinter as tk
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import tk_quiet  # noqa: E402

# a test file that builds the app, a dialog or a bare Tk window must install tk_quiet
WINDOW_MARKERS = re.compile(r"RuckusRadioApp\(|build_app\(|OnboardingWindow\(|Dialog\(|tk\.Tk\(|Toplevel\(")


def test_windows_are_invisible_and_never_forced_forward():
    tk_quiet.install()
    root = tk.Tk()
    top = tk.Toplevel(root)
    try:
        for win in (root, top):
            assert float(win.attributes("-alpha")) == 0.0, "window must be fully transparent"
            assert int(win.attributes("-toolwindow")) == 1, "no taskbar button"
        forced: list[str] = []
        root.tk.createcommand("focus", lambda *a: forced.append("focus"))
        root.tk.createcommand("raise", lambda *a: forced.append("raise"))
        top.focus_force()
        top.lift()
        top.tkraise()
        assert forced == [], f"focus_force/lift reached Tk: {forced}"
        print("test windows are transparent, taskbar-free and never forced forward: OK")
    finally:
        root.destroy()


def test_install_is_idempotent():
    tk_quiet.install()
    tk_quiet.install()
    root = tk.Tk()
    try:
        assert float(root.attributes("-alpha")) == 0.0
    finally:
        root.destroy()
    print("installing twice is harmless: OK")


def test_every_window_building_test_installs_tk_quiet():
    missing = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if WINDOW_MARKERS.search(text) and "tk_quiet.install()" not in text:
            missing.append(path.name)
    assert not missing, f"tests building Tk windows without tk_quiet.install(): {missing}"
    print("every window-building test installs tk_quiet: OK")


if __name__ == "__main__":
    test_windows_are_invisible_and_never_forced_forward()
    test_install_is_idempotent()
    test_every_window_building_test_installs_tk_quiet()
