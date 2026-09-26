"""Sound-Bibliothek im Testmodus mit echten Dateien (Fixtures) und echtem ffmpeg."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-library-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, hotkeyservice, library, playback, protocol as p  # noqa: E402
import core_fakes  # noqa: E402

FIX = core_fakes.FIXTURES


def setup():
    c, events = core_fakes.bare_core()
    c.playback = playback.PlaybackService(c)
    c.hotkeys = hotkeyservice.HotkeyService(c, core_fakes.FakeHotkeys())
    c.library = library.LibraryService(c)
    return c, events


def notices(events):
    return [(n.text, n.level) for n in core_fakes.of_type(events, p.Notice)]


def files():
    root = Path(_TMP)
    return {str(f.relative_to(root)) for d in ("sounds", "icons") for f in (root / d).iterdir()}


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_add_copies_measures_and_announces():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Airhorn"))
    sound = c.library.sounds[-1]
    assert sound["name"] == "Airhorn" and sound["volume"] == 1.0 and sound["hotkey"] is None
    assert (Path(_TMP) / sound["file"]).exists() and (Path(_TMP) / sound["icon"]).exists()
    assert "integrated" in sound["loudness"], sound
    assert core_fakes.of_type(events, p.SoundAdded) == [p.SoundAdded(sound["id"])]
    assert notices(events) == [("„Airhorn“ wird hinzugefügt …", "hint"),
                               ("„Airhorn“ hinzugefügt", "hint")]
    assert c.engine.preloads == [sound["id"]]
    assert config.find_sound(on_disk(), sound["id"]) is not None
    assert c.state()["sounds"][-1]["id"] == sound["id"]
    print("add copies, measures, preloads, saves and announces: OK")


def test_add_mp4_with_icon_dedupes_the_name():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Airhorn"))
    c.send(p.AddSound(str(FIX / "test_tone.mp4"), "Airhorn", str(FIX / "test_image.png")))
    second = c.library.sounds[-1]
    assert second["name"] == "Airhorn (2)"
    assert (Path(_TMP) / second["file"]).suffix == ".mp3"
    assert (Path(_TMP) / second["file"]).stat().st_size > 0
    print("mp4 is extracted to mp3, a duplicate name gets (2): OK")


def test_failed_adds_leave_nothing_behind():
    c, events = setup()
    before = files()
    bad_icon = Path(_TMP) / "notimage.png"
    bad_icon.write_text("not an image", encoding="utf-8")
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "X", str(bad_icon)))
    bad_audio = Path(_TMP) / "broken.mp4"
    bad_audio.write_text("not a video", encoding="utf-8")
    c.send(p.AddSound(str(bad_audio), "Y"))
    assert c.library.sounds == []
    assert files() == before, "no copied audio or icon is left behind"
    errors = [text for text, level in notices(events) if level == "error"]
    assert errors == [library.ICON_UNREADABLE.format(name="notimage.png"),
                      library.AUDIO_UNREADABLE.format(name="broken.mp4")], errors
    print("an unreadable icon or audio file adds nothing and explains why: OK")


def test_rename_volume_and_icon():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Eins"))
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Zwei"))
    first, second = c.library.sounds
    c.send(p.RenameSound(second["id"], "  Eins  "))
    assert second["name"] == "Eins (2)"
    c.send(p.RenameSound(second["id"], "   "))
    assert second["name"] == "Eins (2)", "an empty name is ignored"
    c.send(p.SetSoundVolume(first["id"], 9))
    assert first["volume"] == config.MAX_SOUND_VOLUME
    c.send(p.SetSoundVolume(first["id"], 0.333))
    assert first["volume"] == 0.33 and config.find_sound(on_disk(), first["id"])["volume"] == 0.33
    states_before = len(core_fakes.of_type(events, p.StateChanged))
    c.send(p.SetSoundIcon(first["id"], str(FIX / "test_image.png")))
    assert len(core_fakes.of_type(events, p.StateChanged)) == states_before + 1
    bad = Path(_TMP) / "bad.png"
    bad.write_text("nope", encoding="utf-8")
    c.send(p.SetSoundIcon(first["id"], str(bad)))
    assert notices(events)[-1] == (library.ICON_CHANGE_UNREADABLE, "error")
    print("rename dedupes, volume is clamped and rounded, icon change is reported: OK")


def test_delete_removes_everything():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Weg"))
    sound = c.library.sounds[0]
    c.send(p.SetHotkey(sound["id"], "f9"))
    manager = c.hotkeys._manager
    assert "f9" in manager.registered
    c.send(p.DeleteSound(sound["id"]))
    assert c.library.sounds == []
    assert not (Path(_TMP) / sound["file"]).exists() and not (Path(_TMP) / sound["icon"]).exists()
    assert "f9" not in manager.registered
    assert sound["id"] in c.engine.forgotten
    assert on_disk()["sounds"] == []
    c.send(p.DeleteSound("unknown"))  # nothing to do, no error
    print("delete removes files, hotkey, decoded audio and the entry: OK")


def test_export_and_import_roundtrip():
    c, events = setup()
    c.send(p.ExportSounds(str(Path(_TMP) / "empty.ruckuspack")))
    assert notices(events)[-1] == (library.NOTHING_TO_EXPORT, "info")
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Horn"))
    target = Path(_TMP) / "board.ruckuspack"
    c.send(p.ExportSounds(str(target)))
    assert target.exists()
    assert notices(events)[-1] == ("Exportiert: board.ruckuspack", "info")
    c.send(p.ImportPack(str(target)))
    names = [s["name"] for s in c.library.sounds]
    assert names == ["Horn", "Horn (2)"], names
    assert notices(events)[-1] == (
        "1 Sounds importiert. Hotkeys vergibst du per Rechtsklick.", "info")
    imported = c.library.sounds[-1]
    assert "loudness" in imported, "imports are measured by the backfill"
    assert imported["id"] in c.engine.preloads
    broken = Path(_TMP) / "broken.ruckuspack"
    broken.write_text("no zip", encoding="utf-8")
    c.send(p.ImportPack(str(broken)))
    assert notices(events)[-1][1] == "error"
    print("export and import round-trip, a broken pack is an error notice: OK")


def test_icon_change_unreadable_message_matches_todays_gui_text():
    # today's gui.py/widgets.py text: f"{exc} {BAD_IMAGE_HINT}" with exc = IconError's
    # default message ("Die Datei ist kein lesbares Bild.") and BAD_IMAGE_HINT =
    # "Wähl ein PNG-, JPG-, BMP- oder WEBP-Bild."
    assert library.ICON_CHANGE_UNREADABLE == (
        "Die Datei ist kein lesbares Bild. Wähl ein PNG-, JPG-, BMP- oder WEBP-Bild.")
    print("ICON_CHANGE_UNREADABLE reproduces today's user-visible text: OK")


def test_deleted_sound_icon_is_cleaned_up_when_the_change_lands_late():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Geist"))
    sound = c.library.sounds[0]
    dest = Path(_TMP) / sound["icon"]
    c.send(p.DeleteSound(sound["id"]))
    # simulate the icon worker writing the file just as the sound got deleted
    dest.write_bytes(b"leftover-icon-bytes")
    c.library._icon_saved(sound["id"], dest, None)
    assert not dest.exists(), "an icon written for a deleted sound must be cleaned up"
    print("an icon change that lands after the sound was deleted is cleaned up: OK")


def test_icon_rev_increments_on_a_successful_change():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Runde"))
    sound = c.library.sounds[0]

    def rev():
        return next(s["icon_rev"] for s in c.state()["sounds"] if s["id"] == sound["id"])

    assert rev() == 0
    c.send(p.SetSoundIcon(sound["id"], str(FIX / "test_image.png")))
    assert rev() == 1
    c.send(p.SetSoundIcon(sound["id"], str(FIX / "test_image.png")))
    assert rev() == 2
    # a failed change must not bump the revision
    bad = Path(_TMP) / "bad2.png"
    bad.write_text("nope", encoding="utf-8")
    c.send(p.SetSoundIcon(sound["id"], str(bad)))
    assert rev() == 2
    print("icon_rev increments only on a successful icon change: OK")


def test_loudness_backfill_retries_only_transient_failures():
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Messen"))
    sound = c.library.sounds[0]
    sound.pop("loudness")
    original = library.loudness.measure_dict
    library.loudness.measure_dict = lambda _path: None
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert "loudness" not in sound, "a transient failure stores nothing"
    assert c.library._loudness_running is False
    c.library.start_loudness_backfill()
    assert "integrated" in sound["loudness"]

    def explode(_path):
        raise RuntimeError("ffmpeg crashed")

    sound.pop("loudness")
    library.loudness.measure_dict = explode
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert sound["loudness"] == {"failed": True}
    print("backfill: transient failures retry later, crashes mark the sound: OK")


def main():
    test_add_copies_measures_and_announces()
    test_add_mp4_with_icon_dedupes_the_name()
    test_failed_adds_leave_nothing_behind()
    test_rename_volume_and_icon()
    test_icon_change_unreadable_message_matches_todays_gui_text()
    test_deleted_sound_icon_is_cleaned_up_when_the_change_lands_late()
    test_icon_rev_increments_on_a_successful_change()
    test_delete_removes_everything()
    test_export_and_import_roundtrip()
    test_loudness_backfill_retries_only_transient_failures()
    print("\nALL LIBRARY LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
