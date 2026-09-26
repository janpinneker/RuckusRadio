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

from soundboard import config, core, hotkeyservice, protocol as p  # noqa: E402
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
    assert set(manager.registered) == {"ctrl+alt+backspace", "ctrl+shift+1"}
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [
        hotkeyservice.INVALID.format(label="Kaputt")]
    print("start registers stop-all and every valid hotkey, skips the broken one: OK")


def test_callbacks_send_commands_from_any_thread():
    c, events, manager, service, received = setup()
    c.start()
    worker = threading.Thread(target=manager.registered["ctrl+shift+1"])
    worker.start()
    worker.join()
    manager.registered["ctrl+alt+backspace"]()
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
    assert set(manager.registered) == {"ctrl+alt+backspace", "ctrl+shift+1"}
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


def main():
    test_start_registers_stop_all_and_every_valid_hotkey()
    test_callbacks_send_commands_from_any_thread()
    test_set_and_remove_hotkey_persist_and_rebind()
    test_suspend_resume_and_forget()
    test_shutdown_unregisters_everything_first()
    test_no_manager_is_fine()
    test_shutdown_unregisters_hotkeys_reregistered_by_a_command_still_queued()
    print("\nALL HOTKEYSERVICE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
