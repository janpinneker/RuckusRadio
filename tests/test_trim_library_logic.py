"""C8 Kuerzen im Kern: Wellenform (Ereignis, nie Zustand), Vorhoeren, Zuschnitt setzen
und entfernen, Nachmessen, Export ueber den Kern-Dialog, Einordnung LIBRARY."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-trimlib-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_fakes  # noqa: E402
from soundboard import access, audio, config, library, trimming  # noqa: E402
from soundboard import protocol as p  # noqa: E402
from soundboard.filedialogs import FileDialogs  # noqa: E402

SOUND = Path(__file__).parent / "fixtures" / "test_tone.mp3"  # 2 s Ton


class FakeDialogs:
    def __init__(self, target=None):
        self.target = target
        self.asked: list[str] = []

    def ask_audio_target(self, default_name):
        self.asked.append(default_name)
        return self.target


def added(dialogs=None):
    c, events = core_fakes.make_core()
    if dialogs is not None:
        c.library._dialogs = dialogs
    c.send(p.AddSound(str(SOUND), "Horn"))
    return c, events, c.library.sounds[-1]


def notices(events):
    return [(n.text, n.level) for n in core_fakes.of_type(events, p.Notice)]


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def fake_measure(calls):
    def measure(path, trim=None):
        calls.append(trim)
        return {"integrated": -30.0, "max_short": -28.0, "peak": -6.0}
    return measure


def test_the_waveform_is_an_event_not_state():
    c, events, sound = added()
    c.send(p.LoadWaveform(sound["id"]))
    [wave] = core_fakes.of_type(events, p.WaveformLoaded)
    assert wave.sound_id == sound["id"]
    assert abs(wave.duration - 2.0) < 0.05, wave.duration
    assert len(wave.peaks) == 2 * trimming.PEAK_BUCKETS
    assert all(-1.0 <= v <= 1.0 for v in wave.peaks) and max(wave.peaks) > 0.05
    assert "peaks" not in json.dumps(c.state()), "nie im Zustand (Z2)"
    c.send(p.LoadWaveform("gone"))
    assert len(core_fakes.of_type(events, p.WaveformLoaded)) == 1
    print("Wellenform kommt als Ereignis, nicht im Zustand: OK")


def test_set_trim_stores_remeasures_and_redecodes():
    c, events, sound = added()
    calls = []
    original = library.loudness.measure_dict
    library.loudness.measure_dict = fake_measure(calls)
    try:
        c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    finally:
        library.loudness.measure_dict = original
    assert sound["trim"] == {"start": 0.5, "end": 1.5}
    assert calls == [{"start": 0.5, "end": 1.5}], "nur der Schnitt wird gemessen"
    assert sound["loudness"]["integrated"] == -30.0
    assert c.engine.trims[sound["id"]] == {"start": 0.5, "end": 1.5}, "neu dekodiert"
    assert c.state("library")["sounds"][-1]["trim"] == {"start": 0.5, "end": 1.5}
    assert config.find_sound(on_disk(), sound["id"])["trim"] == {"start": 0.5, "end": 1.5}
    assert (Path(_TMP) / sound["file"]).read_bytes() == SOUND.read_bytes(), "Datei unberuehrt"
    assert notices(events)[-1] == (library.TRIMMED.format(name="Horn"), "hint")
    print("SetSoundTrim speichert, misst nach und dekodiert neu: OK")


def test_invalid_or_too_short_cuts_are_refused_with_a_hint():
    c, events, sound = added()
    c.send(p.SetSoundTrim(sound["id"], 1.5, 0.5))
    c.send(p.SetSoundTrim(sound["id"], 0.0, 9.0))
    c.send(p.SetSoundTrim(sound["id"], 1.0, 1.05))
    assert "trim" not in sound
    assert notices(events)[-3:] == [(trimming.TRIM_INVALID, "hint"),
                                    (trimming.TRIM_INVALID, "hint"),
                                    (trimming.TRIM_TOO_SHORT, "hint")]
    print("ungueltige Schnitte verwirft der Kern mit Hinweis: OK")


def test_a_full_length_cut_and_clear_remove_the_trim():
    c, events, sound = added()
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    c.send(p.SetSoundTrim(sound["id"], 0.0, 2.0))
    assert "trim" not in sound, "ganze Laenge = kein Zuschnitt"
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    c.send(p.ClearSoundTrim(sound["id"]))
    assert "trim" not in sound and c.engine.trims[sound["id"]] is None
    assert notices(events)[-1] == (library.TRIM_CLEARED.format(name="Horn"), "hint")
    count = len(notices(events))
    c.send(p.ClearSoundTrim(sound["id"]))
    assert len(notices(events)) == count, "nichts zu entfernen: still"
    print("ganze Laenge und ClearSoundTrim entfernen den Zuschnitt: OK")


def test_an_older_trim_answer_never_overwrites_a_newer_one():
    c, events, sound = added()
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.send(p.SetSoundTrim(sound["id"], 0.2, 1.0))
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    workers.run_one(1)  # der neuere landet zuerst
    workers.run_one(0)  # der aeltere landet spaet
    workers.run_all()   # Neu-Dekodieren aus reload
    assert sound["trim"] == {"start": 0.5, "end": 1.5}
    print("eine aeltere Antwort ueberschreibt keinen neueren Zuschnitt: OK")


def test_preview_plays_only_the_cut_and_saves_nothing():
    c, events, sound = added()
    c.send(p.PreviewTrim(sound["id"], 0.5, 1.0))
    [(sound_id, frames, _gain)] = c.engine.clips
    assert sound_id == sound["id"] and abs(frames - 24000) <= 1, frames
    assert c.engine.plays == [] and "trim" not in sound
    c.send(p.PreviewTrim(sound["id"], 1.0, 0.5))
    assert len(c.engine.clips) == 1 and notices(events)[-1] == (trimming.TRIM_INVALID, "hint")
    print("Vorhoeren spielt nur den Schnitt und speichert nichts: OK")


def test_preview_without_a_headphone_or_a_mixer_starts_nothing():
    """K6: no target (no headphone, no sink/mixer) - play_clip must not report a
    PlaybackStarted for something that never actually played anywhere."""
    c, events, sound = added()
    c.engine.no_target = True
    c.send(p.PreviewTrim(sound["id"], 0.5, 1.0))
    assert c.engine.clips == [], "ohne Ziel darf nichts anlaufen"
    assert sound["id"] not in c.engine.playing
    assert core_fakes.of_type(events, p.PlaybackStarted) == [], \
        "ohne Ziel darf kein PlaybackStarted gemeldet werden"
    print("Vorhoeren ohne Kopfhoerer und ohne Mischer startet nichts: OK")


def test_export_asks_python_for_the_target_and_writes_the_cut():
    target = Path(_TMP) / "out" / "Horn kurz.mp3"
    dialogs = FakeDialogs(target=str(target))
    c, events, sound = added(dialogs)
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert dialogs.asked == ["Horn.mp3"]
    seconds = len(audio.decode_audio(target).samples) / 48000
    assert 0.95 < seconds < 1.1, seconds
    assert notices(events)[-1] == ("Exportiert: Horn kurz.mp3", "info")
    assert (Path(_TMP) / sound["file"]).exists(), "das Original bleibt"
    dialogs.target = None
    before = len(notices(events))
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert len(notices(events)) == before, "abgebrochen: nichts"
    print("Export: Ziel aus dem Kern-Dialog, neue Datei mit Schnitt: OK")


def test_the_native_dialog_asks_for_an_mp3_target():
    seen = {}

    class FakeFiledialog:
        @staticmethod
        def asksaveasfilename(**kwargs):
            seen.update(kwargs)
            return "C:/x/Horn.mp3"

    got = FileDialogs._open(FakeFiledialog, {"default_name": "Horn.mp3"}, "audio_target")
    assert got == "C:/x/Horn.mp3"
    assert seen["title"] == "Gekürzten Sound speichern" and seen["initialfile"] == "Horn.mp3"
    assert seen["defaultextension"] == ".mp3"
    print("Windows-Dialog fragt nach einem MP3-Ziel: OK")


def test_the_backfill_measures_the_cut():
    c, events, sound = added()
    sound["trim"] = {"start": 0.5, "end": 1.5}
    sound.pop("loudness")
    calls = []
    original = library.loudness.measure_dict
    library.loudness.measure_dict = fake_measure(calls)
    try:
        c.library.start_loudness_backfill()
    finally:
        library.loudness.measure_dict = original
    assert calls == [{"start": 0.5, "end": 1.5}]
    print("der Nachmess-Lauf misst mit Zuschnitt: OK")


def test_the_trim_commands_are_window_only():
    for cls in (p.LoadWaveform, p.PreviewTrim, p.SetSoundTrim, p.ClearSoundTrim,
                p.RequestExportTrimmed):
        assert access.COMMAND_CAPABILITY[cls] == access.LIBRARY, cls
    assert access.LIBRARY not in access.ROLE_CAPABILITIES[access.ROLE_VIEW]
    print("alle Zuschnitt-Befehle sind LIBRARY, die Ansicht hat sie nicht (Z7): OK")


# ---- Review-Fixes ----

def test_a_trimmed_sound_without_duration_gets_measured_once():
    """Fix 1(a)/(b): ein alter Sound von vor Klangbild hat schon einen Zuschnitt, aber
    noch keine "duration" - der Nachmess-Lauf misst einmal zusaetzlich ohne Zuschnitt,
    um die volle Laenge zu bekommen, und leitet daraus die Kategorie ab. Danach nie
    wieder (needs_measuring greift, sobald "duration" da ist)."""
    c, events, sound = added()
    sound["trim"] = {"start": 0.5, "end": 1.5}
    sound.pop("duration", None)
    sound.pop("category", None)
    sound.pop("loudness", None)
    calls = []

    def fake(path, trim=None):
        calls.append(trim)
        if trim is None:
            return {"integrated": -20.0, "max_short": -18.0, "peak": -3.0, "duration": 40.0}
        return {"integrated": -12.0, "max_short": -10.0, "peak": -1.0}

    original = library.loudness.measure_dict
    library.loudness.measure_dict = fake
    try:
        c.library.start_loudness_backfill()
        assert len(calls) == 2 and None in calls and {"start": 0.5, "end": 1.5} in calls
        c.library.start_loudness_backfill()
        assert len(calls) == 2, "danach nicht mehr gemessen"
    finally:
        library.loudness.measure_dict = original
    assert sound["duration"] == 40.0, sound
    assert sound["category"] == "music", "40 s zaehlt als Musik"
    assert sound["loudness"] == {"integrated": -12.0, "max_short": -10.0, "peak": -1.0}
    print("ein alter, getrimmter Sound ohne Laenge wird einmal zusaetzlich gemessen: OK")


def test_a_failed_trim_remeasure_falls_back_to_the_safe_default():
    """F3 (final review): eine fehlgeschlagene Neumessung NACH EINER TRIM-AENDERUNG darf
    nicht die alte Lautheit behalten - die gehoert zu einer anderen Spanne (dem vorigen
    Zuschnitt) und waere fuer den neuen Schnitt falsch. Stattdessen wird {"failed": True}
    gespeichert -> Rueckfall auf den sicheren Standardpegel (UNMEASURED_GAIN_DB). Das
    Backfill-Verhalten (gute Lautheit behalten) bleibt fuer den Nachmess-Lauf selbst
    unveraendert (siehe test_a_trim_remeasure_keeps_the_stored_duration_and_category
    und die Fix-1(c)-Backfill-Tests)."""
    c, events, sound = added()
    good = dict(sound["loudness"])

    def fail(path, trim=None):
        return {"failed": True}

    original = library.loudness.measure_dict
    library.loudness.measure_dict = fail
    try:
        c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    finally:
        library.loudness.measure_dict = original
    assert sound["trim"] == {"start": 0.5, "end": 1.5}
    assert sound["loudness"] == {"failed": True}, \
        "eine fehlgeschlagene Neumessung nach einer Trim-Aenderung faellt auf den sicheren Standard zurueck"
    assert sound["loudness"] != good
    print("eine fehlgeschlagene Zuschnitt-Neumessung faellt auf den sicheren Standard zurueck: OK")


def test_a_failed_remeasure_after_clearing_a_trim_falls_back_to_the_safe_default():
    """K2: wie beim Setzen (test_a_failed_trim_remeasure_falls_back_to_the_safe_default)
    darf auch der Clear-Weg bei einer fehlgeschlagenen Neumessung nicht die alte Lautheit
    behalten - die gehoert zum entfernten Zuschnitt und waere fuer die volle Datei falsch.
    {"failed": True} -> sicherer Standardpegel, statt der alten guten Lautheit."""
    c, events, sound = added()
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))
    good = dict(sound["loudness"])

    def fail(path, trim=None):
        return {"failed": True}

    original = library.loudness.measure_dict
    library.loudness.measure_dict = fail
    try:
        c.send(p.ClearSoundTrim(sound["id"]))
    finally:
        library.loudness.measure_dict = original
    assert "trim" not in sound
    assert sound["loudness"] == {"failed": True}, \
        "eine fehlgeschlagene Neumessung nach ClearSoundTrim faellt auf den sicheren Standard zurueck"
    assert sound["loudness"] != good
    print("eine fehlgeschlagene Neumessung nach ClearSoundTrim faellt auf den sicheren Standard zurueck: OK")


def test_clearing_a_trim_remeasures_the_full_file_properly():
    """Fix 2: ohne Zuschnitt (ClearSoundTrim oder ein Schnitt ueber die ganze Laenge)
    traegt die Messung "duration" - die muss ueber apply_measurement richtig einsortiert
    werden, nicht als verschachtelter Wert in "loudness" landen."""
    c, events, sound = added()
    c.send(p.SetSoundTrim(sound["id"], 0.5, 1.5))

    def full(path, trim=None):
        assert trim is None
        return {"integrated": -16.0, "max_short": -14.0, "peak": -2.0, "duration": 2.0}

    original = library.loudness.measure_dict
    library.loudness.measure_dict = full
    try:
        c.send(p.ClearSoundTrim(sound["id"]))
    finally:
        library.loudness.measure_dict = original
    assert "trim" not in sound
    assert sound["loudness"] == {"integrated": -16.0, "max_short": -14.0, "peak": -2.0}, sound["loudness"]
    assert sound["duration"] == 2.0
    print("ClearSoundTrim setzt die volle Lautheit ohne verschachtelte Laenge: OK")


def test_export_forces_the_mp3_suffix():
    """Fix 3: ein Ziel ohne (oder mit falscher) Endung wird trotzdem als .mp3 geschrieben."""
    target = Path(_TMP) / "out" / "Horn kurz"
    dialogs = FakeDialogs(target=str(target))
    c, events, sound = added(dialogs)
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert (Path(_TMP) / "out" / "Horn kurz.mp3").exists(), "die .mp3-Endung wird erzwungen"
    print("Export erzwingt die .mp3-Endung: OK")


def test_export_refuses_a_target_inside_the_sounds_folder():
    """Fix 3: ein Ziel im Sound-Ordner der Bibliothek wird abgelehnt, nichts wird
    geschrieben, mit einem klaren Hinweis."""
    inside = Path(_TMP) / "sounds" / "sneaky.mp3"
    dialogs = FakeDialogs(target=str(inside))
    c, events, sound = added(dialogs)
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert not inside.exists()
    assert notices(events)[-1] == (library.EXPORT_TARGET_UNSAFE, "error")
    print("Export ins Sound-Verzeichnis wird abgelehnt: OK")


def test_export_worker_maps_a_same_path_valueerror_to_the_safe_target_notice():
    """Fix 3: export_trimmed's eigene dest==src-Sperre (ValueError) wird als derselbe
    klare Hinweis gemeldet, nicht als generischer Schreibfehler."""
    c, events, sound = added()
    src = Path(_TMP) / sound["file"]
    c.library._export_trimmed(src, src, None)
    assert notices(events)[-1] == (library.EXPORT_TARGET_UNSAFE, "error")
    print("dest==src wird als sicherer Ziel-Hinweis gemeldet, nicht als Schreibfehler: OK")


def test_a_removed_sound_drops_its_late_waveform_and_preview():
    """Fix 4: eine Wellenform oder Vorschau, deren Dekodieren erst nach dem Entfernen
    des Sounds zurueckkommt, wird auf dem Kern-Thread verworfen."""
    c, events, sound = added()
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.send(p.LoadWaveform(sound["id"]))
    c.send(p.PreviewTrim(sound["id"], 0.2, 1.0))
    c.library.sounds.remove(sound)  # aus der Bibliothek entfernt, Datei bleibt auf der Platte
    workers.run_all()
    assert core_fakes.of_type(events, p.WaveformLoaded) == []
    assert c.engine.clips == []
    print("ein zwischenzeitlich entfernter Sound verwirft seine spaete Wellenform/Vorschau: OK")


def test_clear_while_a_set_is_still_pending_wins_over_it():
    """Fix 4: ClearSoundTrim gewinnt auch dann, wenn der Sound noch nie sichtbar
    getrimmt war und ein aelteres SetSoundTrim noch auf dem Worker rechnet."""
    c, events, sound = added()
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.send(p.SetSoundTrim(sound["id"], 0.2, 1.0))  # noch nicht gelaufen
    c.send(p.ClearSoundTrim(sound["id"]))  # "trim" noch nicht sichtbar, muss trotzdem gewinnen
    workers.run_all()
    assert "trim" not in sound, "Clear gewinnt gegen ein noch laufendes, aelteres Set"
    print("Clear waehrend eines noch laufenden Set gewinnt: OK")


# ---- Review-Fixes Runde 2 ----

def test_an_old_full_measure_without_duration_is_stored_once():
    """Fix A: das zusaetzliche, ungeschnittene Mass fuer die volle Laenge kommt zurueck,
    aber ohne "duration" (ffmpeg lehnt die volle Datei ab, oder der Framelog hat keine
    Frames) - dann wird explizit "duration": None gespeichert, wie apply_measurement es
    tut, statt beide Messungen bei jedem Start zu wiederholen. Nur eine echte Fehlanzeige
    (None) wird erneut versucht."""
    c, events, sound = added()
    sound["trim"] = {"start": 0.5, "end": 1.5}
    sound.pop("duration", None)
    sound.pop("category", None)
    sound.pop("loudness", None)
    calls = []

    def fake(path, trim=None):
        calls.append(trim)
        if trim is None:
            return {"integrated": -20.0, "max_short": -18.0, "peak": -3.0}  # kein "duration"
        return {"integrated": -12.0, "max_short": -10.0, "peak": -1.0}

    original = library.loudness.measure_dict
    library.loudness.measure_dict = fake
    try:
        c.library.start_loudness_backfill()
        assert len(calls) == 2
        c.library.start_loudness_backfill()
        assert len(calls) == 2, "eine Antwort ohne Laenge wird nicht wiederholt"
    finally:
        library.loudness.measure_dict = original
    assert "duration" in sound and sound["duration"] is None, sound
    assert "category" not in sound
    assert sound["loudness"] == {"integrated": -12.0, "max_short": -10.0, "peak": -1.0}
    print("eine Vollmessung ohne Laenge wird einmal gespeichert, nicht wiederholt: OK")


def test_a_trim_change_landing_before_the_backfill_answer_is_not_overwritten():
    """Fix B: der Nachmess-Lauf hat den Zuschnitt beim Start als Momentaufnahme
    mitgenommen; landet ein ClearSoundTrim zuerst (eigener Worker-Auftrag), muss die
    spaete, jetzt veraltete Backfill-Antwort verworfen werden - sie darf weder die neue
    Voll-Lautheit noch die neue Laenge ueberschreiben."""
    c, events, sound = added()
    sound["trim"] = {"start": 0.5, "end": 1.5}
    sound.pop("loudness")
    workers = core_fakes.QueuedWorkers()
    c.workers = workers

    def fake(path, trim=None):
        if trim is None:
            return {"integrated": -5.0, "max_short": -4.0, "peak": -1.0, "duration": 2.0}
        return {"integrated": -30.0, "max_short": -28.0, "peak": -6.0}  # der (bald veraltete) Schnitt

    original = library.loudness.measure_dict
    library.loudness.measure_dict = fake
    try:
        c.library.start_loudness_backfill()  # queued: misst noch mit dem alten Zuschnitt
        c.send(p.ClearSoundTrim(sound["id"]))  # landet zuerst, entfernt den Zuschnitt
        workers.run_one(1)  # ClearSoundTrim: _prepare_trim + _trim_ready (Executor ist inline)
        workers.run_one(0)  # die jetzt veraltete Backfill-Antwort
    finally:
        library.loudness.measure_dict = original
    assert "trim" not in sound
    assert sound["duration"] == 2.0, "die neue volle Laenge bleibt (Clear kam zuerst an)"
    assert sound["loudness"] == {"integrated": -5.0, "max_short": -4.0, "peak": -1.0}, sound["loudness"]
    print("eine Zuschnitt-Aenderung vor der Backfill-Antwort gewinnt: OK")


def test_export_appends_mp3_without_eating_a_dotted_name():
    """Fix C: with_suffix() wuerde "Take 1.5" zu "Take 1.mp3" verstuemmeln - die Endung
    wird angehaengt, nicht ersetzt."""
    target = Path(_TMP) / "out2" / "Take 1.5"
    dialogs = FakeDialogs(target=str(target))
    c, events, sound = added(dialogs)
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert (Path(_TMP) / "out2" / "Take 1.5.mp3").exists()
    assert not (Path(_TMP) / "out2" / "Take 1.mp3").exists()
    print("Export haengt .mp3 an, statt einen gepunkteten Namen zu verstuemmeln: OK")


def test_export_refuses_when_the_forced_name_already_exists():
    """Fix C: der Speichern-Dialog hat nur "Clash" gesehen, nie "Clash.mp3" - existiert
    diese erzwungene Datei schon, wird abgelehnt statt sie stillschweigend zu ersetzen."""
    out_dir = Path(_TMP) / "out3"
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = out_dir / "Clash.mp3"
    existing.write_bytes(b"schon da")
    dialogs = FakeDialogs(target=str(out_dir / "Clash"))
    c, events, sound = added(dialogs)
    c.send(p.RequestExportTrimmed(sound["id"]))
    assert existing.read_bytes() == b"schon da", "die vorhandene Datei bleibt unangetastet"
    assert notices(events)[-1] == (library.EXPORT_TARGET_EXISTS.format(name="Clash.mp3"), "error")
    print("ein erzwungener Name, den es schon gibt, wird abgelehnt: OK")


def main():
    test_the_waveform_is_an_event_not_state()
    test_set_trim_stores_remeasures_and_redecodes()
    test_invalid_or_too_short_cuts_are_refused_with_a_hint()
    test_a_full_length_cut_and_clear_remove_the_trim()
    test_an_older_trim_answer_never_overwrites_a_newer_one()
    test_preview_plays_only_the_cut_and_saves_nothing()
    test_preview_without_a_headphone_or_a_mixer_starts_nothing()
    test_export_asks_python_for_the_target_and_writes_the_cut()
    test_the_native_dialog_asks_for_an_mp3_target()
    test_the_backfill_measures_the_cut()
    test_the_trim_commands_are_window_only()
    test_a_trimmed_sound_without_duration_gets_measured_once()
    test_a_failed_trim_remeasure_falls_back_to_the_safe_default()
    test_a_failed_remeasure_after_clearing_a_trim_falls_back_to_the_safe_default()
    test_clearing_a_trim_remeasures_the_full_file_properly()
    test_export_forces_the_mp3_suffix()
    test_export_refuses_a_target_inside_the_sounds_folder()
    test_export_worker_maps_a_same_path_valueerror_to_the_safe_target_notice()
    test_a_removed_sound_drops_its_late_waveform_and_preview()
    test_clear_while_a_set_is_still_pending_wins_over_it()
    test_an_old_full_measure_without_duration_is_stored_once()
    test_a_trim_change_landing_before_the_backfill_answer_is_not_overwritten()
    test_export_appends_mp3_without_eating_a_dotted_name()
    test_export_refuses_when_the_forced_name_already_exists()
    print("\nALL TRIM LIBRARY CHECKS PASSED")


if __name__ == "__main__":
    main()
