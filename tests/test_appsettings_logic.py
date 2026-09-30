"""App-Einstellungen und der fertig zusammengebaute Kern."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-appsettings-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import access, appsettings, config, protocol as p  # noqa: E402
import core_fakes  # noqa: E402


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_complete_onboarding_and_autostart():
    c, events = core_fakes.make_core()
    c.send(p.CompleteOnboarding())
    assert on_disk()["onboarding_completed"] is True
    c.send(p.SetAutostart(True))
    assert c.settings._autostart.calls == [True]
    assert on_disk()["autostart"] is True
    assert c.state()["settings"]["autostart"] is True
    print("onboarding flag and autostart are stored: OK")


def test_autostart_failure_keeps_the_old_value():
    autostart = core_fakes.FakeAutostart(ok=False)
    c, events = core_fakes.make_core(autostart=autostart)
    c.send(p.SetAutostart(True))
    assert c.store.data["autostart"] is False
    assert core_fakes.of_type(events, p.Notice)[-1] == p.Notice(appsettings.AUTOSTART_FAILED)
    assert core_fakes.of_type(events, p.StateChanged), "the interface resets its checkbox"
    print("a refused autostart change keeps the old value and says so: OK")


def test_a_reset_config_is_reported_on_start():
    c, events = core_fakes.make_core()
    c.store.was_reset = True
    c.start()
    assert p.Notice(appsettings.CONFIG_RESET) in core_fakes.of_type(events, p.Notice)
    print("a config reset at load time is reported on start: OK")


def test_regenerate_view_token_rotates_and_reports():
    c, events = core_fakes.make_core()
    before = c.access.view_token
    c.send(p.RegenerateViewToken())
    assert c.access.view_token != before
    assert c.store.data[access.VIEW_TOKEN_KEY] == c.access.view_token
    assert p.Notice(appsettings.VIEW_TOKEN_RENEWED) in core_fakes.of_type(events, p.Notice)
    assert core_fakes.of_type(events, p.StateChanged), "die Seite laedt den neuen Link"
    print("der Ansichtsschluessel laesst sich erneuern und wird gemeldet: OK")


def test_assembled_core_handles_every_command_and_state_is_json():
    c, events = core_fakes.make_core()
    commands = {cls for cls in p._REGISTRY.values() if issubclass(cls, p.Command)}
    assert commands == set(c._handlers), commands ^ set(c._handlers)
    c.start()
    c.send(p.AddSound(str(core_fakes.FIXTURES / "test_tone.mp3"), "Airhorn"))
    state = c.state()
    assert set(state) == {"protocol", "playback", "jobs", "sounds", "devices", "settings",
                          "updates", "spotify", "spotify_player", "musicbus"}, set(state)
    json.dumps(p.to_json(p.StateChanged(state)))
    sound_id = state["sounds"][0]["id"]
    c.send(p.Play(sound_id))
    assert c.engine.plays and c.engine.plays[-1][0] == sound_id
    assert c.shutdown() is True
    print("the assembled core handles every command and its state is JSON: OK")


PIN_A = {"kind": "spotify-playlist", "id": "pl1", "name": "Zocken", "image": "https://i.scdn.co/a.jpg"}
PIN_B = {"kind": "spotify-album", "id": "al1", "name": "Wege", "image": None}


def test_sidebar_pins_are_stored_and_published():
    c, events = core_fakes.make_core()
    assert c.state()["settings"]["sidebar_pins"] == []
    c.send(p.SetSidebarPins((PIN_A, PIN_B)))
    assert on_disk()["sidebar_pins"] == [PIN_A, PIN_B]
    assert c.state()["settings"]["sidebar_pins"] == [PIN_A, PIN_B]
    c.send(p.SetSidebarPins((PIN_B,)))
    assert c.state()["settings"]["sidebar_pins"] == [PIN_B]
    print("sidebar pins are stored, published and replaceable: OK")


def test_sidebar_pins_refuse_bad_input():
    c, events = core_fakes.make_core()
    c.send(p.SetSidebarPins((PIN_A,)))
    bad = [
        ({"kind": "virus", "id": "x", "name": "x", "image": None},),
        ({"kind": "folder", "id": "", "name": "x", "image": None},),
        ({"kind": "folder", "id": "x", "name": 5, "image": None},),
        ({"kind": "folder", "id": "x", "name": "x", "image": "file:///c:/secret"},),
        ("not a dict",),
        (PIN_A, dict(PIN_A)),  # duplicate
    ]
    for pins in bad:
        c.send(p.SetSidebarPins(pins))
        assert c.state()["settings"]["sidebar_pins"] == [PIN_A], pins
    print("malformed, duplicate or local-file pins are refused: OK")


def test_sidebar_pins_limit():
    c, events = core_fakes.make_core()
    pins = tuple({"kind": "folder", "id": f"f{i}", "name": f"F{i}", "image": None} for i in range(21))
    c.send(p.SetSidebarPins(pins))
    assert c.state()["settings"]["sidebar_pins"] == []
    assert core_fakes.of_type(events, p.Notice)[-1] == p.Notice(appsettings.PINS_FULL)
    c.send(p.SetSidebarPins(pins[:20]))
    assert len(c.state()["settings"]["sidebar_pins"]) == 20
    print("more than 20 pins are refused with a notice: OK")


def main():
    test_complete_onboarding_and_autostart()
    test_autostart_failure_keeps_the_old_value()
    test_a_reset_config_is_reported_on_start()
    test_regenerate_view_token_rotates_and_reports()
    test_sidebar_pins_are_stored_and_published()
    test_sidebar_pins_refuse_bad_input()
    test_sidebar_pins_limit()
    test_assembled_core_handles_every_command_and_state_is_json()
    print("\nALL APPSETTINGS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
