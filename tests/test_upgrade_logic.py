"""Upgrade 1.4.0 -> 1.5.0 (Nachtlauf B3): a config.json in the exact 1.4.0 shape loads
without losing a sound, hotkey, trim, pin or output, gains the library fields, and the
clear-text secrets.json moves into secrets.dat - also across a second start.

The keys below are the key set of a real 1.4.0 config (checked 2026-10-03 against a copy
in a temp dir); the values are made up. Runs only in a temp RUCKUS_DATA_DIR."""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-upgrade-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_fakes  # noqa: E402
from soundboard import config  # noqa: E402

TOKEN = "refresh-token-from-1.4"


def _sound(n: int, **extra) -> dict:
    sid = f"00000000-0000-4000-8000-{n:012d}"
    sound = {"id": sid, "name": f"Sound {n}", "file": f"sounds/{sid}.mp3", "icon": f"icons/{sid}.png",
             "hotkey": None, "volume": 1.0, "duration": 2.5, "loudness": -16.0, "plays": n}
    sound.update(extra)
    return sound


CONFIG_140 = {
    "version": 2, "autostart": False, "onboarding_completed": True,
    "default_mic": "Mikrofon (USB)", "default_mic_gain": 1.0, "microphone_name": "Mikrofon (USB)",
    "monitor_device": "Kopfhörer", "discord_output": "CABLE Input", "voicemeeter_device_name": None,
    "outputs": {"CABLE Input": {"mic": True, "mic_gain": 1.0, "music": True, "sounds": True, "sounds_gain": 0.8},
                "__monitor__": {"mic": False, "mic_gain": 0.0, "sounds": True, "sounds_gain": 0.4, "music": False}},
    "ducking_enabled": True, "ducking_db": -12.0, "sounds_offset_db": 0.0, "music_offset_db": -3.0,
    "musicbus_enabled": True, "musicbus_gain": 1.0, "klangbild_targets": {"effect": -16, "music": -20},
    "server_port": 47800, "view_token": "view-token-abc", "spotify_client_id": "client-id",
    "spotify_device_id": "device-1", "stop_all_hotkey": "ctrl+ß", "stop_all_migrated": True,
    "sidebar_pins": [{"kind": "spotify-playlist", "id": "37i9dQZF1DXcBWIGoYBM5M", "name": "Today"}],
    "sounds": [
        _sound(1, hotkey="ctrl+alt+1"),
        _sound(2, trim={"start": 0.5, "end": 1.5}, category="music"),
        _sound(3, volume=0.7),
    ],
}


def _write(name: str, data) -> None:
    Path(_TMP, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_a_140_config_and_its_secrets_survive_the_upgrade():
    _write("config.json", CONFIG_140)
    _write("secrets.json", {"spotify_token": TOKEN})
    before = copy.deepcopy(CONFIG_140)

    c, _ = core_fakes.make_core(store_data=config.load_config())
    data = c.store.data
    assert [s["id"] for s in data["sounds"]] == [s["id"] for s in before["sounds"]]
    for old, new in zip(before["sounds"], data["sounds"]):
        for key, value in old.items():
            assert new.get(key) == value, (old["id"], key, value, new.get(key))
        assert (new["folder_id"], new["tags"], new["favorite"], new["cover"]) == (None, [], False, None)
    for key in ("sidebar_pins", "outputs", "stop_all_hotkey", "view_token", "server_port",
                "klangbild_targets", "spotify_client_id"):
        assert data[key] == before[key], key
    assert data["folders"] == [] and data["playlists"] == []
    library = c.state()["library"]
    assert library["folders"] == [] and library["playlists"] == []

    assert c.store.secret("spotify_token") == TOKEN
    assert not Path(_TMP, "secrets.json").exists(), "the clear text goes after the takeover"
    assert TOKEN.encode() not in Path(_TMP, "secrets.dat").read_bytes()
    c.store.save_now()

    again, _ = core_fakes.make_core(store_data=config.load_config())
    assert again.store.secret("spotify_token") == TOKEN, "the second start reads secrets.dat"
    assert [s["id"] for s in again.store.data["sounds"]] == [s["id"] for s in before["sounds"]]
    assert again.store.data["sounds"][0]["hotkey"] == "ctrl+alt+1"
    print("eine 1.4.0-config und ihre Geheimnisse ueberstehen das Update: OK")


def main():
    test_a_140_config_and_its_secrets_survive_the_upgrade()
    print("\nALL UPGRADE CHECKS PASSED")


if __name__ == "__main__":
    main()
