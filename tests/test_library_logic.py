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
    library.loudness.measure_dict = lambda _path, _trim=None: None
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert "loudness" not in sound, "a transient failure stores nothing"
    assert c.library._loudness_running is False
    c.library.start_loudness_backfill()
    assert "integrated" in sound["loudness"]

    def explode(_path, _trim=None):
        raise RuntimeError("ffmpeg crashed")

    sound.pop("loudness")
    library.loudness.measure_dict = explode
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert sound["loudness"] == {"failed": True}
    print("backfill: transient failures retry later, crashes mark the sound: OK")


def test_set_sound_category_switches_by_hand():
    """Klangbild K2: umstellen im Kontextmenue, sofort gespeichert; nur zwei Kategorien."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Kategorie"))
    sound = c.library.sounds[-1]
    c.send(p.SetSoundCategory(sound["id"], "music"))
    assert sound["category"] == "music"
    assert config.find_sound(on_disk(), sound["id"])["category"] == "music", "sofort gespeichert"
    c.send(p.SetSoundCategory(sound["id"], "voice"))
    assert sound["category"] == "music", "unbekannte Kategorie wird ignoriert"
    c.send(p.SetSoundCategory("gibt-es-nicht", "effect"))  # darf nicht werfen
    print("SetSoundCategory stellt von Hand um: OK")


def test_add_measures_the_length_and_sorts_long_sounds_into_music():
    """Klangbild K2: Laenge beim Hinzufuegen, >= 30 s = Musik; kurz = Effekt."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Kurz"))
    short = c.library.sounds[-1]
    assert short["category"] == "effect" and abs(short["duration"] - 2.0) < 0.15, short
    original = library.loudness.measure_dict
    library.loudness.measure_dict = lambda _p: {"integrated": -9.0, "max_short": -7.0,
                                                "peak": -0.5, "duration": 184.2}
    try:
        c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Lang"))
    finally:
        library.loudness.measure_dict = original
    song = c.library.sounds[-1]
    assert song["category"] == "music" and song["duration"] == 184.2, song
    assert song["loudness"] == {"integrated": -9.0, "max_short": -7.0, "peak": -0.5}, \
        "die Laenge steht am Sound, nicht in der Messung"
    assert c.state()["sounds"][-1]["category"] == "music", "die Seite sieht die Kategorie"
    assert not any("Musik" in text for text, _ in notices(events)), "kein K7-Hinweis beim Hinzufuegen"
    print("add measures the length and sorts long sounds into music: OK")


def test_old_sounds_are_sorted_by_length_once_with_a_hint():
    """Klangbild K7: alte Sounds (ohne Laenge) werden einmal nachgemessen und nach Laenge
    eingeordnet; eine Hand-Wahl bleibt; der Hinweis nennt die Zahl."""
    c, events = setup()
    for name in ("Lied", "Hand"):
        c.send(p.AddSound(str(FIX / "test_tone.mp3"), name))
    song, manual = c.library.sounds[-2], c.library.sounds[-1]
    for sound in (song, manual):  # so sah ein Sound vor Klangbild aus
        sound.pop("duration", None)
        sound.pop("category", None)
    manual["category"] = "effect"  # von Hand gesetzt
    calls = []
    original = library.loudness.measure_dict

    def long_song(path, trim=None):
        calls.append(path)
        return {"integrated": -9.0, "max_short": -7.0, "peak": -0.5, "duration": 200.0}

    library.loudness.measure_dict = long_song
    try:
        c.library.start_loudness_backfill()
        assert len(calls) == 2, "beide ohne Laenge werden nachgemessen"
        c.library.start_loudness_backfill()
        assert len(calls) == 2, "danach nie wieder"
    finally:
        library.loudness.measure_dict = original
    assert song["category"] == "music" and song["duration"] == 200.0, song
    assert manual["category"] == "effect", "die Hand-Wahl bleibt"
    assert manual["duration"] == 200.0
    assert ("1 langer Sound zählt jetzt als Musik.", "info") in notices(events), notices(events)
    print("old sounds are sorted by length once, with a hint: OK")


def test_failed_remeasurement_keeps_good_loudness():
    """Review-Fix 1: eine fehlgeschlagene Nachmessung (K7-Backfill trifft einen Sound mit
    schon gueltiger Lautheit) darf die vorhandenen guten Daten und die Kategorie nicht
    zerstoeren."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Gut"))
    sound = c.library.sounds[-1]
    good_loudness = dict(sound["loudness"])
    sound["category"] = "music"  # z.B. von Hand gewaehlt oder schon eingeordnet
    assert library.apply_measurement(sound, {"failed": True}) is False
    assert sound["loudness"] == good_loudness, sound["loudness"]
    assert sound["category"] == "music", sound["category"]
    print("a failed re-measurement keeps the good loudness and category: OK")


def test_measurement_without_duration_is_measured_once():
    """Review-Fix 2 (minor): ein Clip ohne Framezeilen (keine Laenge) bekommt trotzdem
    den Schluessel "duration" (Wert None) gesetzt, damit needs_measuring nicht bei
    jedem Start wieder zuschlaegt."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Kurzclip"))
    sound = c.library.sounds[-1]
    sound.pop("duration", None)
    sound.pop("category", None)
    made_music = library.apply_measurement(
        sound, {"integrated": -9.0, "max_short": -7.0, "peak": -0.5})
    assert made_music is False
    assert "duration" in sound and sound["duration"] is None, sound
    assert "category" not in sound, "unbekannte Laenge setzt keine Kategorie"
    assert library.needs_measuring(sound) is False, "einmal ohne Laenge gemessen reicht"
    print("a measurement without a length is stored once, not remeasured forever: OK")


def test_apply_trim_measurement_never_nests_duration_in_loudness():
    """F2: measure_dict(path, trim) can now carry "duration" when the stored trim was
    broken (falls back to the whole file - see loudness.measure_dict). That "duration"
    must never land nested inside sound["loudness"] - apply_trim_measurement pops it,
    exactly like apply_measurement does for its own "duration" key."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Kaputt"))
    sound = c.library.sounds[-1]
    measured = {"integrated": -20.0, "max_short": -18.0, "peak": -3.0, "duration": 99.0}
    library.apply_trim_measurement(sound, measured)
    assert sound["loudness"] == {"integrated": -20.0, "max_short": -18.0, "peak": -3.0}, sound["loudness"]
    print("apply_trim_measurement speichert keine verschachtelte Laenge in loudness: OK")


def test_a_trim_remeasure_keeps_the_stored_duration_and_category():
    """C8/Klangbild-Schnittstelle: loudness.measure_dict(path, trim) liefert nie
    "duration" (der Schnitt ist nicht die volle Laenge des Sounds). Der Nachmess-Lauf
    darf die schon gespeicherte volle Laenge und die davon abgeleitete Kategorie
    deshalb nicht mit None ueberschreiben, wenn er einen getrimmten Sound trifft."""
    c, events = setup()
    c.send(p.AddSound(str(FIX / "test_tone.mp3"), "Geschnitten"))
    sound = c.library.sounds[-1]
    sound["duration"] = 42.0
    sound["category"] = "music"
    sound["trim"] = {"start": 0.5, "end": 1.5}
    sound.pop("loudness")
    original = library.loudness.measure_dict
    library.loudness.measure_dict = lambda _path, _trim=None: {
        "integrated": -18.0, "max_short": -16.0, "peak": -3.0}
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert sound["loudness"]["integrated"] == -18.0, sound["loudness"]
    assert "duration" not in sound["loudness"]
    assert sound["duration"] == 42.0, "die volle Laenge bleibt unberuehrt"
    assert sound["category"] == "music", "die Kategorie bleibt unberuehrt"
    print("ein Nachmessen mit Zuschnitt laesst Laenge und Kategorie unberuehrt: OK")


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
    test_set_sound_category_switches_by_hand()
    test_add_measures_the_length_and_sorts_long_sounds_into_music()
    test_old_sounds_are_sorted_by_length_once_with_a_hint()
    test_failed_remeasurement_keeps_good_loudness()
    test_measurement_without_duration_is_measured_once()
    test_apply_trim_measurement_never_nests_duration_in_loudness()
    test_a_trim_remeasure_keeps_the_stored_duration_and_category()
    print("\nALL LIBRARY LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
