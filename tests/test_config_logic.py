"""Config-Migration auf den outputs-Schluessel, ohne die echte %APPDATA%-Datei."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="ruckus-config-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config  # noqa: E402


def test_fresh_config_has_outputs():
    fresh = config._default_config()
    assert fresh["outputs"] == {}, fresh
    assert "output_mode" not in fresh
    assert "mic_passthrough" not in fresh
    assert "monitor_volume" not in fresh, "the dock's Mithören slider is gone; the " \
        "Kopfhörer row on Einstellungen is the only place this volume lives now"
    print("fresh config has outputs: OK")


def test_old_config_keeps_working():
    """A config.json written before this feature must not crash and must not lose values."""
    old = {
        "version": 1,
        "onboarding_completed": True,
        "output_mode": "auto",
        "mic_passthrough": True,
        "mic_gain": 0.8,
        "monitor_device": "default",
        "monitor_volume": 0.056,
        "sounds": [],
    }
    merged = config._with_defaults(old)
    monitor = merged["outputs"][config.MONITOR_KEY]
    assert monitor["sounds"] is True
    # Version 2 resets the headphone level too (it was hand-tuned for unnormalized
    # audio, same as the per-sound volumes), so the migrated monitor_volume does not
    # survive - the monitor row ends up at the app default instead.
    assert monitor["sounds_gain"] == config.DEFAULT_MONITOR_OUTPUT["sounds_gain"], monitor
    assert monitor["mic"] is False, "hearing yourself is off unless asked for"
    assert monitor["mic_gain"] == 0.0
    # The old global values survive as the default for cables that appear later.
    assert abs(merged["default_mic_gain"] - 0.8) < 1e-9, merged
    assert merged["default_mic"] is True
    # The dock's old "Mithören" slider is gone: the value is copied into the monitor
    # row first, then reset to the app default there by _migrate_levels (checked
    # above) - but the top-level key itself must not survive.
    assert "monitor_volume" not in merged, merged
    print("old config keeps working: OK")


def test_migration_is_idempotent():
    old = {"version": 1, "mic_gain": 0.8, "monitor_volume": 0.5, "sounds": []}
    once = config._with_defaults(old)
    assert "monitor_volume" not in once, once
    once["outputs"][config.MONITOR_KEY]["sounds_gain"] = 0.25
    twice = config._with_defaults(once)
    assert twice["outputs"][config.MONITOR_KEY]["sounds_gain"] == 0.25, \
        "a second load must not overwrite what the user changed"
    assert "monitor_volume" not in twice, twice
    print("migration is idempotent: OK")


def test_load_config_oserror_keeps_app_startable():
    path = config.config_path()
    path.write_text("{}", encoding="utf-8")
    with patch("soundboard.config.open", side_effect=OSError("config locked")):
        loaded = config.load_config()
    assert loaded == config._default_config(), loaded
    assert path.exists(), "a read failure must not overwrite the original config"
    print("load_config handles read OSError: OK")


def test_output_settings_defaults():
    cfg = config._default_config()
    cable = config.output_settings(cfg, "CABLE Output (VB-Audio Virtual Cable)")
    assert cable == {"mic": True, "mic_gain": 1.0, "sounds": True, "sounds_gain": 1.0,
                     "music": True}, cable
    monitor = config.output_settings(cfg, config.MONITOR_KEY, is_monitor=True)
    assert monitor["mic"] is False and monitor["sounds"] is True, monitor
    # Reading must not write.
    assert cfg["outputs"] == {}, cfg["outputs"]
    print("output_settings defaults: OK")


def test_music_is_per_cable_and_never_on_the_headphones():
    """Spec audio-routing §4: Kabel ohne Schluessel = Musik an (nichts aendert sich
    still), Kopfhoerer immer aus - auch wenn eine alte/kaputte Config es anders sagt."""
    cfg = config._default_config()
    cfg["outputs"] = {"CABLE Output": {"mic": True, "mic_gain": 1.0, "sounds": True,
                                       "sounds_gain": 1.0},
                      config.MONITOR_KEY: {"sounds": True, "music": True}}
    assert config.output_settings(cfg, "CABLE Output")["music"] is True, "alter Eintrag: an"
    assert config.output_settings(cfg, config.MONITOR_KEY, is_monitor=True)["music"] is False
    got = config.set_output_settings(cfg, "CABLE Output", music=False)
    assert got["music"] is False and cfg["outputs"]["CABLE Output"]["music"] is False
    assert config.set_output_settings(cfg, config.MONITOR_KEY, music=True)["music"] is False, \
        "Kopfhoerer bleiben ohne Musik"
    print("music per cable, never on the headphones: OK")


def test_music_follows_sounds_when_only_an_old_entry_without_music_is_stored():
    """Final-Fix F1: ein gespeicherter Eintrag ohne den Schluessel `music` darf die
    Musik nicht immer auf True stellen - sie folgt `sounds`, sonst schaltet ein
    Kabel, dessen Sounds abgeschaltet waren, nach dem Update ploetzlich Musik ein."""
    cfg = config._default_config()
    cfg["outputs"] = {
        "CABLE Output": {"mic": True, "mic_gain": 1.0, "sounds": False, "sounds_gain": 1.0},
        "Hi-Fi Cable Output": {"mic": True, "mic_gain": 1.0, "sounds": True, "sounds_gain": 1.0},
    }
    assert config.output_settings(cfg, "CABLE Output")["music"] is False, \
        "alter Eintrag, sounds False, kein music: music folgt sounds"
    assert config.output_settings(cfg, "Hi-Fi Cable Output")["music"] is True, \
        "alter Eintrag, sounds True, kein music: music folgt sounds"
    # Kein Eintrag ueberhaupt (brandneues Kabel): True, unveraendert.
    assert config.output_settings(cfg, "Neues Kabel")["music"] is True
    # Ein gespeicherter Eintrag MIT music gewinnt weiterhin, unabhaengig von sounds.
    cfg["outputs"]["CABLE Output"]["music"] = True
    assert config.output_settings(cfg, "CABLE Output")["music"] is True, \
        "ein gespeichertes music gewinnt ueber die sounds-Ableitung"
    print("music folgt sounds bei altem Eintrag ohne music: OK")


def test_voicemeeter_starts_without_the_mic():
    """VoiceMeeter mischt selbst. Wuerde Ruckus Radio dort das Mikrofon greifen, naehme
    es VoiceMeeter das Mikrofon weg - deshalb ist es aus, bis der Nutzer es anschaltet."""
    cfg = config._default_config()
    vm = config.output_settings(cfg, "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)",
                                is_voicemeeter=True)
    assert vm["mic"] is False, vm
    assert vm["sounds"] is True, "the sounds are the whole point of the target"
    cable = config.output_settings(cfg, "CABLE Output (VB-Audio Virtual Cable)")
    assert cable["mic"] is True, "a plain cable has nobody else to mix for it"
    # Once the user switches it on, that choice wins over the default.
    config.set_output_settings(cfg, "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)", mic=True)
    again = config.output_settings(cfg, "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)",
                                   is_voicemeeter=True)
    assert again["mic"] is True, again
    print("voicemeeter starts without the mic: OK")


def test_set_output_settings_clamps():
    cfg = config._default_config()
    got = config.set_output_settings(cfg, "CABLE Output", sounds_gain=3.0, mic=False)
    assert got["sounds_gain"] == config.MAX_OUTPUT_GAIN, got
    assert got["mic"] is False
    assert cfg["outputs"]["CABLE Output"]["sounds_gain"] == config.MAX_OUTPUT_GAIN
    assert config.set_output_settings(cfg, "CABLE Output", mic_gain=-1.0)["mic_gain"] == 0.0
    assert config.set_output_settings(cfg, "CABLE Output", sounds_gain=float("nan"))["sounds_gain"] == 1.0
    print("set_output_settings clamps: OK")


def test_version_2_resets_the_hand_tuned_cable_gains():
    """Vor Version 2 standen die Kabel-Sounds auf 2-6 %, weil nichts normalisiert war.
    Mit Lautheitsmessung wuerden sie damit fast unhoerbar - also einmal neutral."""
    cable_key = "CABLE Output (VB-Audio Virtual Cable)"
    old = {
        "version": 1,
        "outputs": {
            cable_key: {"mic": True, "mic_gain": 0.85, "sounds": True, "sounds_gain": 0.06},
            config.MONITOR_KEY: {"mic": False, "mic_gain": 0.0, "sounds": True,
                                 "sounds_gain": 0.11},
        },
        "sounds": [],
    }
    merged = config._with_defaults(old)
    cable = merged["outputs"][cable_key]
    assert cable["mic_gain"] == 1.0 and cable["sounds_gain"] == 1.0, cable
    assert cable["mic"] is True and cable["sounds"] is True, "switches are kept"
    assert merged["outputs"][config.MONITOR_KEY]["sounds_gain"] == 0.5, \
        "the headphones were hand-tuned for unnormalized audio too and are reset " \
        "to the app default, same as the cables"
    assert merged["version"] == 2
    assert merged["sounds_offset_db"] == -6.0
    assert merged["ducking_enabled"] is True and merged["ducking_db"] == -6.0
    merged["outputs"][cable_key]["sounds_gain"] = 0.7
    again = config._with_defaults(merged)
    assert again["outputs"][cable_key]["sounds_gain"] == 0.7, "version 2 is never reset again"
    assert config._default_config()["version"] == 2
    print("version 2 resets hand-tuned cable gains once: OK")


def test_version_2_resets_per_sound_volumes():
    """v1 users hand-tuned per-sound volume (0.02-0.5) to tame unnormalized songs.
    Stacked with v2's loudness normalization, those volumes would bury the sound
    20-40 dB under the voice - so every sound's volume is reset to neutral too,
    once, alongside the cable and headphone gains."""
    old = {
        "version": 1,
        "outputs": {
            "CABLE Output (VB-Audio Virtual Cable)": {
                "mic": True, "mic_gain": 0.85, "sounds": True, "sounds_gain": 0.06},
            "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)": {
                "mic": True, "mic_gain": 1.0, "sounds": True, "sounds_gain": 0.06},
            config.MONITOR_KEY: {"mic": False, "mic_gain": 0.4568, "sounds": True,
                                 "sounds_gain": 0.109},
        },
        "sounds": [
            {"id": "a", "name": "x", "file": "sounds/a.mp3", "icon": "icons/a.png",
             "hotkey": None, "volume": 0.02},
            {"id": "b", "name": "y", "file": "sounds/b.mp3", "icon": "icons/b.png",
             "hotkey": None, "volume": 1.4},
        ],
        "microphone_name": "Mikrofon (Endorfy Solum Voice S Mic)",
    }
    merged = config._with_defaults(old)
    sound_a, sound_b = merged["sounds"]
    assert sound_a["volume"] == 1.0, sound_a
    assert sound_b["volume"] == 1.0, sound_b
    assert sound_a["id"] == "a" and sound_a["name"] == "x", "other keys untouched"
    assert sound_a["file"] == "sounds/a.mp3" and sound_a["icon"] == "icons/a.png"
    assert sound_a["hotkey"] is None
    monitor = merged["outputs"][config.MONITOR_KEY]
    assert monitor["sounds_gain"] == 0.5, monitor
    assert monitor["mic_gain"] == 0.4568, "only sounds_gain is reset on the monitor"
    assert merged["microphone_name"] == "Mikrofon (Endorfy Solum Voice S Mic)"

    # v2 is never reset again: a hand-tuned volume set after migration must survive.
    merged["sounds"][0]["volume"] = 0.3
    again = config._with_defaults(merged)
    assert again["sounds"][0]["volume"] == 0.3, "version 2 is never reset again"
    print("version 2 resets per-sound volumes once: OK")


def test_old_config_without_plays_survives_load_and_save():
    """A config.json written before the plays counter existed must load fine (the
    missing key means 0 plays, never a crash) and must not gain a fabricated value
    that then gets written back."""
    old = {
        "version": config.CONFIG_VERSION,
        "sounds": [{"id": "a", "name": "x", "file": "sounds/a.mp3",
                   "icon": "icons/a.png", "hotkey": None, "volume": 1.0}],
    }
    path = config.config_path()
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = config.load_config()
    sound = loaded["sounds"][0]
    assert "plays" not in sound, "load must not invent a plays key"
    assert sound.get("plays", 0) == 0

    config.save_config(loaded)
    reloaded = config.load_config()
    assert "plays" not in reloaded["sounds"][0], "a round trip must not add it either"

    reloaded["sounds"][0]["plays"] = 3
    config.save_config(reloaded)
    again = config.load_config()
    assert again["sounds"][0]["plays"] == 3, "once set, it must survive save/load"
    print("a config without plays loads fine and the counter survives save/load: OK")


def test_stop_all_default_is_alt_delete_and_the_old_default_migrates():
    assert config.DEFAULT_CONFIG["stop_all_hotkey"] == "ctrl+ß"
    merged = config._with_defaults({"stop_all_hotkey": "ctrl+alt+backspace"})
    assert merged["stop_all_hotkey"] == "ctrl+ß"
    assert merged["stop_all_migrated"] is True
    kept = config._with_defaults({"stop_all_hotkey": "ctrl+shift+s"})
    assert kept["stop_all_hotkey"] == "ctrl+shift+s", "a user's own choice stays"
    print("stop-all defaults to Alt+Entf; the old AltGr default migrates: OK")


def test_stop_all_migration_is_one_time_and_skips_a_taken_alt_delete():
    # A user who deliberately picks the old combo again after migration must not be
    # reverted the next time their config.json is loaded.
    once = config._with_defaults({"stop_all_hotkey": "ctrl+alt+backspace"})
    assert once["stop_all_hotkey"] == "ctrl+ß"
    once["stop_all_hotkey"] = "ctrl+alt+backspace"  # deliberately picked again
    twice = config._with_defaults(once)
    assert twice["stop_all_hotkey"] == "ctrl+alt+backspace", \
        "a deliberate later choice of the old combo must survive"
    assert twice["stop_all_migrated"] is True

    # A fresh install's default config already carries the marker, so it never
    # migrates even though it doesn't use the old combo.
    assert config.DEFAULT_CONFIG["stop_all_migrated"] is True

    # If a sound already owns ctrl+ß, the migration must not steal it - the
    # user's old stop-all combo stays instead.
    conflicting = config._with_defaults({
        "stop_all_hotkey": "ctrl+alt+backspace",
        "sounds": [{"id": "a", "name": "Airhorn", "file": "sounds/a.mp3",
                   "icon": "icons/a.png", "hotkey": "ctrl+ß", "volume": 1.0}],
    })
    assert conflicting["stop_all_hotkey"] == "ctrl+alt+backspace", \
        "must not steal a sound's hotkey"
    assert conflicting["stop_all_migrated"] is True
    print("stop-all migration runs once and skips a taken ctrl+ß: OK")


def test_stop_all_migration_survives_non_string_hotkeys():
    merged = config._with_defaults({
        "stop_all_hotkey": "ctrl+alt+backspace",
        "sounds": [{"id": "x", "name": "X", "hotkey": 5},
                   {"id": "y", "name": "Y", "hotkey": ["ctrl+ß"]}],
    })
    assert merged["stop_all_hotkey"] == "ctrl+ß"
    odd = config._with_defaults({"stop_all_hotkey": 42})
    assert odd["stop_all_migrated"] is True
    print("stop-all migration treats non-string hotkeys as no hotkey: OK")


def test_music_bus_defaults_and_gain_clamping():
    """Musik-Bus (Spec \"musik-bus-kern\" §3): aus beim Start, Gain 1.0, 0.0–2.0."""
    fresh = config._default_config()
    assert fresh["musicbus_enabled"] is False, "der Bus startet aus"
    assert fresh["musicbus_gain"] == 1.0
    merged = config._with_defaults({"version": 2})  # eine Config ohne die neuen Schluessel
    assert merged["musicbus_enabled"] is False and merged["musicbus_gain"] == 1.0
    assert config.clamp_gain(-1.0) == 0.0
    assert config.clamp_gain(9.0) == 2.0
    assert config.clamp_gain(0.25) == 0.25
    print("music bus defaults and gain clamping: OK")


def test_klangbild_defaults_and_old_configs():
    """Klangbild K3/K4: neue Schluessel mit Standard; alte Configs bekommen sie, ein
    gespeicherter Teilwert bleibt wie gespeichert (levels.py ergaenzt den Rest)."""
    fresh = config._default_config()
    assert fresh["klangbild_targets"] == {"effect": -20.0, "music": -14.0}
    assert fresh["music_offset_db"] == -3.0
    merged = config._with_defaults({"version": 2, "sounds": []})
    assert merged["klangbild_targets"] == {"effect": -20.0, "music": -14.0}
    assert merged["music_offset_db"] == -3.0
    kept = config._with_defaults({"version": 2, "sounds": [],
                                  "klangbild_targets": {"music": -12.0}})
    assert kept["klangbild_targets"] == {"music": -12.0}
    fresh["klangbild_targets"]["music"] = -10.0
    assert config.DEFAULT_CONFIG["klangbild_targets"]["music"] == -14.0, "Standard unberuehrt"
    print("Klangbild-Schluessel: Standard, alte Config, Teilwerte: OK")


def test_library_keys_default_and_bad_entries_are_dropped():
    """Bibliothek 2.0 (A1): Ordner/Playlists starten leer; was eine von Hand
    bearbeitete Datei kaputt macht, wird beim Laden verworfen statt abzustuerzen."""
    fresh = config._default_config()
    assert fresh["folders"] == [] and fresh["playlists"] == []
    old = config._with_defaults({"version": 2, "sounds": []})
    assert old["folders"] == [] and old["playlists"] == []
    merged = config._with_defaults({
        "version": 2,
        "folders": [
            {"id": "f-1", "name": "Memes", "parent_id": None},
            {"id": "f-2", "name": "Unter", "parent_id": "f-1"},
            {"id": "f-3", "name": "Waise", "parent_id": "f-weg"},   # parent missing -> root
            {"id": "bad id!", "name": "x", "parent_id": None},      # invalid id
            {"id": "f-1", "name": "Doppelt", "parent_id": None},    # duplicate id
            {"id": "f-4", "name": "   ", "parent_id": None},        # empty name
            "kein dict",
        ],
        "playlists": [
            {"id": "p-1", "name": "Lieblinge", "item_ids": ["s1", "s1", "weg", 5]},
            {"id": "p-2", "name": 7, "item_ids": []},
            {"id": "p-3", "name": "Ohne Liste"},
        ],
        "sounds": [
            {"id": "s1", "name": "A", "folder_id": "f-2", "tags": [" Lustig ", "lustig", 3, ""], "favorite": True},
            {"id": "s2", "name": "B", "folder_id": "f-weg", "tags": "kein", "favorite": "ja"},
        ],
    })
    assert merged["folders"] == [
        {"id": "f-1", "name": "Memes", "parent_id": None, "cover": None},
        {"id": "f-2", "name": "Unter", "parent_id": "f-1", "cover": None},
        {"id": "f-3", "name": "Waise", "parent_id": None, "cover": None},
    ], merged["folders"]
    assert merged["playlists"] == [
        {"id": "p-1", "name": "Lieblinge", "item_ids": ["s1"], "cover": None},
        {"id": "p-3", "name": "Ohne Liste", "item_ids": [], "cover": None},
    ], merged["playlists"]
    s1, s2 = merged["sounds"]
    assert (s1["folder_id"], s1["tags"], s1["favorite"]) == ("f-2", ["Lustig"], True), s1
    assert (s2["folder_id"], s2["tags"], s2["favorite"]) == (None, [], False), s2
    print("Bibliothek: Standard leer, kaputte Eintraege verworfen: OK")


def main():
    test_fresh_config_has_outputs()
    test_old_config_keeps_working()
    test_migration_is_idempotent()
    test_load_config_oserror_keeps_app_startable()
    test_output_settings_defaults()
    test_music_is_per_cable_and_never_on_the_headphones()
    test_music_follows_sounds_when_only_an_old_entry_without_music_is_stored()
    test_voicemeeter_starts_without_the_mic()
    test_set_output_settings_clamps()
    test_version_2_resets_the_hand_tuned_cable_gains()
    test_version_2_resets_per_sound_volumes()
    test_old_config_without_plays_survives_load_and_save()
    test_stop_all_default_is_alt_delete_and_the_old_default_migrates()
    test_stop_all_migration_is_one_time_and_skips_a_taken_alt_delete()
    test_stop_all_migration_survives_non_string_hotkeys()
    test_music_bus_defaults_and_gain_clamping()
    test_klangbild_defaults_and_old_configs()
    test_library_keys_default_and_bad_entries_are_dropped()
    print("\nALL CONFIG LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
