"""Hotkeys am Kern: Callbacks legen nur Befehle in die Warteschlange."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-hotkeys-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, core, hotkeys, hotkeyservice, protocol as p  # noqa: E402
import core_fakes  # noqa: E402


def setup():
    c, events = core_fakes.bare_core()
    manager = core_fakes.FakeHotkeys()
    service = hotkeyservice.HotkeyService(c, manager)
    c.store.data["sounds"] = [
        {"id": "a", "name": "Airhorn", "file": "sounds/a.mp3", "icon": "icons/a.png",
         "hotkey": "ctrl+shift+1", "volume": 1.0},
        {"id": "b", "name": "Kaputt", "file": "sounds/b.mp3", "icon": "icons/b.png",
         "hotkey": "bad", "volume": 1.0},
    ]
    received = []
    c.handle(p.Play, received.append)
    c.handle(p.StopAll, received.append)
    return c, events, manager, service, received


def test_start_registers_stop_all_and_every_valid_hotkey():
    c, events, manager, service, received = setup()
    c.start()
    assert set(manager.registered) == {"ctrl+ß", "ctrl+shift+1"}
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [
        hotkeyservice.INVALID.format(label="Kaputt")]
    print("start registers stop-all and every valid hotkey, skips the broken one: OK")


def test_callbacks_send_commands_from_any_thread():
    c, events, manager, service, received = setup()
    c.start()
    worker = threading.Thread(target=manager.registered["ctrl+shift+1"])
    worker.start()
    worker.join()
    manager.registered["ctrl+ß"]()
    assert received == [p.Play("a"), p.StopAll()], received
    print("a hotkey callback sends Play / StopAll to the core: OK")


def test_set_and_remove_hotkey_persist_and_rebind():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetHotkey("a", "f9"))
    assert "f9" in manager.registered and "ctrl+shift+1" not in manager.registered
    assert config.find_sound(c.store.data, "a")["hotkey"] == "f9"
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert config.find_sound(saved, "a")["hotkey"] == "f9"
    c.send(p.RemoveHotkey("a"))
    assert "f9" not in manager.registered
    assert config.find_sound(c.store.data, "a")["hotkey"] is None
    c.send(p.SetHotkey("gone", "f10"))
    assert "f10" not in manager.registered, "unknown sound: nothing happens"
    print("set/remove hotkey persist and rebind: OK")


def test_suspend_resume_and_forget():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SuspendHotkeys())
    assert manager.registered == {}
    c.send(p.ResumeHotkeys())
    assert set(manager.registered) == {"ctrl+ß", "ctrl+shift+1"}
    service.forget(config.find_sound(c.store.data, "a"))
    assert "ctrl+shift+1" not in manager.registered
    print("suspend/resume for the capture dialog, forget on delete: OK")


def test_shutdown_unregisters_everything_first():
    c, events, manager, service, received = setup()
    c.start()
    c.shutdown()
    assert manager.registered == {}
    print("shutdown unregisters every hotkey: OK")


def test_no_manager_is_fine():
    c, events = core_fakes.bare_core()
    hotkeyservice.HotkeyService(c, None)
    c.store.data["sounds"] = [{"id": "a", "name": "A", "file": "f", "icon": "i",
                               "hotkey": "f9", "volume": 1.0}]
    c.start()
    c.send(p.SetHotkey("a", "f10"))
    c.send(p.SuspendHotkeys())
    assert config.find_sound(c.store.data, "a")["hotkey"] == "f10"
    print("without a hotkey manager every command still works: OK")


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.005)


def test_shutdown_unregisters_hotkeys_reregistered_by_a_command_still_queued():
    """before_shutdown unregisters at once, but a ResumeHotkeys already queued behind
    a slow command can re-register hotkeys before the core thread stops accepting
    work - the on_shutdown step must clear them again."""
    c = core.Core(inline=False, store_data=config._default_config())
    manager = core_fakes.FakeHotkeys()
    service = hotkeyservice.HotkeyService(c, manager)
    c.store.data["sounds"] = [{"id": "a", "name": "A", "file": "f", "icon": "i",
                               "hotkey": "f9", "volume": 1.0}]
    release = threading.Event()
    blocking = threading.Event()

    def slow(_cmd):
        blocking.set()
        release.wait(3.0)

    c.handle(p.Rescan, slow)
    c.start()
    wait_until(lambda: "f9" in manager.registered)

    c.send(p.Rescan())
    blocking.wait(3.0)
    c.send(p.ResumeHotkeys())  # queued behind the blocked Rescan, still runs before shutdown ends

    results: list = []

    def do_shutdown():
        results.append(c.shutdown(timeout=5.0))

    shutdown_thread = threading.Thread(target=do_shutdown)
    shutdown_thread.start()
    wait_until(lambda: not c._accepting)  # shutdown has started (before_shutdown ran)
    release.set()
    shutdown_thread.join(5.0)
    assert results == [True], results
    assert manager.registered == {}, manager.registered
    print("shutdown unregisters hotkeys re-registered by a command still queued: OK")


def test_set_hotkey_refuses_the_stop_all_combo_and_reserved_combo():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetHotkey("a", "ctrl+ß"))
    c.send(p.SetHotkey("a", "ctrl+alt+delete"))
    assert config.find_sound(c.store.data, "a")["hotkey"] == "ctrl+shift+1", \
        "the sound's hotkey must be unchanged"
    assert "ctrl+ß" in manager.registered, "stop-all must still be registered"
    manager.registered["ctrl+ß"]()
    assert received[-1] == p.StopAll(), "stop-all must still be registered and firing"
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert any("Alle stoppen" in t for t in texts)
    assert hotkeys.RESERVED_MESSAGE in texts
    print("set_hotkey refuses the stop-all combo and Strg+Alt+Entf: OK")


def test_set_stop_all_hotkey_rebinds_and_persists():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetStopAllHotkey("ctrl+shift+s"))
    assert "ctrl+shift+s" in manager.registered and "ctrl+ß" not in manager.registered
    assert c.store.data["stop_all_hotkey"] == "ctrl+shift+s"
    manager.registered["ctrl+shift+s"]()
    assert received[-1] == p.StopAll()
    print("SetStopAllHotkey rebinds stop-all and saves it: OK")


def test_set_stop_all_hotkey_refuses_reserved_and_taken_combos():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetStopAllHotkey("ctrl+alt+delete"))
    c.send(p.SetStopAllHotkey("shift+ctrl+1"))  # Airhorn has ctrl+shift+1
    assert c.store.data["stop_all_hotkey"] == "ctrl+ß"
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert hotkeyservice.STOP_ALL_RESERVED in texts
    assert hotkeyservice.STOP_ALL_TAKEN.format(name="Airhorn") in texts
    print("stop-all refuses Strg+Alt+Entf and combos of sounds: OK")


def _stop_all_unchanged(c, manager, received):
    assert c.store.data["stop_all_hotkey"] == "ctrl+ß", c.store.data["stop_all_hotkey"]
    assert "ctrl+ß" in manager.registered, "the old stop-all combo must stay live"
    manager.registered["ctrl+ß"]()
    assert received[-1] == p.StopAll()


def test_set_stop_all_hotkey_refuses_empty_and_modifier_only():
    c, events, manager, service, received = setup()
    c.start()
    for bad in ("ctrl++", "", "  ", "ctrl+alt", "shift"):
        c.send(p.SetStopAllHotkey(bad))
    _stop_all_unchanged(c, manager, received)
    assert "ctrl" not in manager.registered, "Ctrl alone must never fire StopAll"
    assert set(manager.registered) == {"ctrl+ß", "ctrl+shift+1"}, manager.registered
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert texts.count(hotkeyservice.STOP_ALL_MODIFIER_ONLY) == 5, texts
    print("stop-all refuses empty and modifier-only combos (ctrl++ is not Ctrl): OK")


def test_set_stop_all_hotkey_refuses_altgr():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetStopAllHotkey("alt gr+1"))
    c.send(p.SetStopAllHotkey("altgr+s"))
    c.send(p.SetStopAllHotkey("ctrl+alt gr"))
    _stop_all_unchanged(c, manager, received)
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert texts.count(hotkeyservice.STOP_ALL_ALTGR) == 3, texts
    print("stop-all refuses AltGr (spec C9): OK")


def test_set_stop_all_hotkey_validates_before_saving():
    c, events, manager, service, received = setup()
    c.start()
    c.send(p.SetStopAllHotkey("ctrl+bad"))
    _stop_all_unchanged(c, manager, received)
    if config.config_path().exists():
        saved = json.loads(config.config_path().read_text(encoding="utf-8"))
        assert saved.get("stop_all_hotkey") != "ctrl+bad", "an unparsable combo must not be saved"
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert hotkeyservice.STOP_ALL_UNUSABLE in texts, texts
    assert hotkeyservice.INVALID.format(label="Alle stoppen") not in texts, texts
    print("stop-all is validated before it is saved; the old combo stays: OK")


def test_stop_all_that_fails_to_register_points_to_settings():
    c, events, manager, service, received = setup()
    c.store.data["stop_all_hotkey"] = "bad"
    c.start()
    texts = [n.text for n in core_fakes.of_type(events, p.Notice)]
    assert hotkeyservice.STOP_ALL_INVALID in texts, texts
    assert "Einstellungen" in hotkeyservice.STOP_ALL_INVALID
    assert hotkeyservice.INVALID.format(label="Alle stoppen") not in texts
    print("a stop-all combo that fails to register points to Einstellungen: OK")


def test_non_string_hotkeys_from_a_hand_edited_config_are_no_hotkey():
    c, events, manager, service, received = setup()
    c.store.data["stop_all_hotkey"] = 7
    c.store.data["sounds"][1]["hotkey"] = ["ctrl+1"]
    c.start()
    assert set(manager.registered) == {"ctrl+shift+1"}, manager.registered
    assert core_fakes.of_type(events, p.Notice) == [], "no-hotkey values are not errors"
    assert hotkeys.normalize_hotkey(5) == ""
    assert hotkeys.normalize_hotkey(None) == ""
    assert hotkeys.find_hotkey_conflict("ctrl+1", [{"id": "x", "name": "X", "hotkey": 5}],
                                        None, 3) is None
    print("non-string hotkeys from a hand-edited config count as no hotkey: OK")


def main():
    test_start_registers_stop_all_and_every_valid_hotkey()
    test_callbacks_send_commands_from_any_thread()
    test_set_and_remove_hotkey_persist_and_rebind()
    test_suspend_resume_and_forget()
    test_shutdown_unregisters_everything_first()
    test_no_manager_is_fine()
    test_shutdown_unregisters_hotkeys_reregistered_by_a_command_still_queued()
    test_set_hotkey_refuses_the_stop_all_combo_and_reserved_combo()
    test_set_stop_all_hotkey_rebinds_and_persists()
    test_set_stop_all_hotkey_refuses_reserved_and_taken_combos()
    test_set_stop_all_hotkey_refuses_empty_and_modifier_only()
    test_set_stop_all_hotkey_refuses_altgr()
    test_set_stop_all_hotkey_validates_before_saving()
    test_stop_all_that_fails_to_register_points_to_settings()
    test_non_string_hotkeys_from_a_hand_edited_config_are_no_hotkey()
    print("\nALL HOTKEYSERVICE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
