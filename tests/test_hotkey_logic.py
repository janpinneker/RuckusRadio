"""Hotkey normalization, conflict detection, plain-key rejection, and the capture
dialog's thread hand-off. Registering/rebinding lives in the core (hotkeyservice,
see test_hotkeyservice_logic); the app runs on the fake core, so no real global hook
is ever installed by tests."""

import logging
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-test-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core_fakes  # noqa: E402
from soundboard.gui import RuckusRadioApp  # noqa: E402
from soundboard.hotkeys import (  # noqa: E402
    find_hotkey_conflict,
    hotkey_error,
    typing_warning,
    is_plain_key,
    normalize_hotkey,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Collect the app's log records instead of printing them (the checks below
# provoke failures on purpose and assert they were logged).
LOGGED: list = []


class _CaptureLog(logging.Handler):
    def emit(self, record):
        LOGGED.append(record)


logging.getLogger("soundboard").addHandler(_CaptureLog())
logging.getLogger("soundboard").propagate = False


def test_normalize_hotkey():
    # canonical priority order: ctrl, alt, shift, windows (matches keyboard.get_hotkey_name)
    assert normalize_hotkey("ctrl+alt+1") == "ctrl+alt+1"
    assert normalize_hotkey("alt+ctrl+1") == "ctrl+alt+1"
    assert normalize_hotkey("  CTRL + ALT + 1 ") == "ctrl+alt+1"
    assert normalize_hotkey("Ctrl+Alt+Backspace") == "ctrl+alt+backspace"
    assert normalize_hotkey("f1") == "f1"
    assert normalize_hotkey("shift+ctrl+alt+9") == "ctrl+alt+shift+9"
    assert normalize_hotkey("windows+shift+alt+ctrl+5") == "ctrl+alt+shift+windows+5"
    print("normalize_hotkey: OK")


def test_is_plain_key():
    assert is_plain_key("a") is True
    assert is_plain_key("1") is True
    assert is_plain_key("A") is True
    assert is_plain_key("ctrl+1") is False
    assert is_plain_key("f1") is False
    assert is_plain_key("shift+a") is False
    assert is_plain_key("ctrl") is False
    print("is_plain_key: OK")


def test_find_hotkey_conflict():
    sounds = [
        {"id": "s1", "name": "Airhorn", "hotkey": "ctrl+alt+1"},
        {"id": "s2", "name": "Bruh", "hotkey": None},
    ]
    assert find_hotkey_conflict("alt+ctrl+1", sounds, None, "ctrl+alt+backspace") == "Airhorn"
    assert find_hotkey_conflict("alt+ctrl+1", sounds, "s1", "ctrl+alt+backspace") is None
    assert find_hotkey_conflict("ctrl+alt+backspace", sounds, None, "ctrl+alt+backspace") == "Alle stoppen"
    assert find_hotkey_conflict("alt+ctrl+backspace", sounds, None, "ctrl+alt+backspace") == "Alle stoppen"
    assert find_hotkey_conflict("ctrl+2", sounds, None, "ctrl+alt+backspace") is None
    print("find_hotkey_conflict: OK")


def test_hotkey_error():
    sounds = [{"id": "s1", "name": "Airhorn", "hotkey": "ctrl+shift+1"}]
    assert hotkey_error("a", sounds, "s1", "ctrl+alt+backspace") is None, "only a warning now"
    assert hotkey_error("ctrl+alt+backspace", sounds, "s1", "ctrl+alt+backspace") == (
        "„CTRL+ALT+BACKSPACE“ ist schon „Alle stoppen“ zugewiesen. Wähl eine andere Kombination."
    )
    msg = hotkey_error("shift+ctrl+1", sounds, "s2", "ctrl+alt+backspace")
    assert msg == "„CTRL+SHIFT+1“ ist schon „Airhorn“ zugewiesen. Wähl eine andere Kombination.", msg
    # same sound editing its own existing hotkey -> not a conflict with itself
    assert hotkey_error("ctrl+shift+1", sounds, "s1", "ctrl+alt+backspace") is None
    assert hotkey_error("ctrl+2", sounds, "s2", "ctrl+alt+backspace") is None
    print("hotkey_error: OK")


def test_typing_warnings_never_refuse():
    from soundboard import hotkeys as hk
    plain, modifier_only, altgr = hk.PLAIN_KEY_MESSAGE, hk.MODIFIER_ONLY_MESSAGE, hk.ALTGR_MESSAGE

    def err(combo):
        assert hotkey_error(combo, [], None, "ctrl+alt+backspace") is None, combo
        return typing_warning(combo)

    for combo in ("space", "enter", "tab", ".", "5", "num 5"):
        assert err(combo) == plain, (combo, err(combo))
    for combo in ("shift", "ctrl+alt", "alt gr", "ctrl+alt+shift"):
        assert err(combo) == modifier_only, (combo, err(combo))
    for combo in ("ctrl+alt+q", "alt+ctrl+7", "ctrl+alt+plus", "ctrl+alt+ß",
                  "ctrl+alt gr+q", "alt gr+ctrl+q", "alt gr+q", "alt gr+1"):
        assert err(combo) == altgr, (combo, err(combo))
    for combo in ("ctrl+shift+1", "f9", "f24", "ctrl+alt+shift+1", "ctrl+alt+f5",
                  "windows+ctrl+alt+q", "ctrl+q", "alt+1", "shift+f2"):
        assert err(combo) is None, (combo, err(combo))
    # the old stop-all combo: backspace types no character, so no warning
    assert typing_warning("ctrl+alt+backspace") is None
    print("typing side effects only warn, every combo is allowed: OK")


def test_ctrl_alt_delete_is_reserved_for_windows():
    from soundboard.hotkeys import RESERVED_HOTKEYS, hotkey_error
    assert "ctrl+alt+delete" in RESERVED_HOTKEYS
    assert hotkey_error("alt+ctrl+delete", [], None, "alt+delete") == "Strg+Alt+Entf gehört Windows. Wähl eine andere Kombination."
    print("Strg+Alt+Entf is never assigned: OK")


def _settle(app, ms=150):
    import time
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.update()
        time.sleep(0.01)


def test_capture_dialog_marshals_via_call_in_ui(app):
    import threading

    import soundboard.widgets as widgets_mod

    captured_cb = []
    orig_capture = widgets_mod.hotkeys.capture_hotkey_async
    widgets_mod.hotkeys.capture_hotkey_async = lambda cb: captured_cb.append(cb)
    try:
        app.add_sound(name="Tröte", audio_path=FIXTURES / "test_tone.mp3", icon_path=None)
        dialog = widgets_mod.HotkeyCaptureDialog(app, app, app.sounds()[-1])
        real_after = dialog.after
        dialog.after = lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("capture callback must not call after() from the hook thread"))
        t = threading.Thread(target=captured_cb[-1], args=("shift+ctrl+4",))
        t.start()
        t.join()
        dialog.after = real_after
        app._pump_ui()
        assert dialog.keycap_label.cget("text") == "CTRL+SHIFT+4", dialog.keycap_label.cget("text")
        assert dialog._captured == "ctrl+shift+4"
        # never raises into the keyboard thread, even when marshalling fails
        app.call_in_ui = lambda *_a: (_ for _ in ()).throw(RuntimeError("boom"))
        captured_cb[-1]("ctrl+shift+5")
        assert LOGGED[-1].getMessage() == "hotkey capture callback failed"
        del app.call_in_ui
        _settle(app)  # let the dialog's delayed grab_set run before closing it
        dialog._cancel()
        print("capture dialog marshals via call_in_ui: OK")
    finally:
        widgets_mod.hotkeys.capture_hotkey_async = orig_capture


if __name__ == "__main__":
    test_normalize_hotkey()
    test_is_plain_key()
    test_find_hotkey_conflict()
    test_hotkey_error()
    test_typing_warnings_never_refuse()
    test_ctrl_alt_delete_is_reserved_for_windows()
    core, _events = core_fakes.make_core()
    shared_app = RuckusRadioApp(core)
    shared_app.withdraw()
    core.start()
    test_capture_dialog_marshals_via_call_in_ui(shared_app)
    _settle(shared_app)
    shared_app.destroy()
    print("\nALL HOTKEY LOGIC CHECKS PASSED")
