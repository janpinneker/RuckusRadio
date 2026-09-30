"""Die Sound-Bibliothek: hinzufuegen, loeschen, umbenennen, Icon, Lautstaerke,
Import/Export als .ruckuspack, Lautheit nachmessen.

Kern-Thread: Liste und Config aendern, speichern, melden. Worker: Kopieren, ffmpeg,
Icons, Pakete. Worker-Ergebnisse kommen als Aufruf auf dem Kern-Thread zurueck und
werden dort gegen die aktuelle Liste geprueft (Sound inzwischen geloescht?).
"""

from __future__ import annotations

import copy
import logging
import shutil
from pathlib import Path

from . import audio, config, icons, levels, loudness, trimming
from .audio import extract_audio
from .filedialogs import FileDialogs
from .packs import PackError, export_pack, sanitize_filename
from .packs import import_pack as read_pack
from .protocol import (AddSound, ClearSoundTrim, DeleteSound, ExportSounds, ImportPack,
                       LoadWaveform, PreviewTrim, RenameSound, RequestAddSound,
                       RequestExportSounds, RequestExportTrimmed, RequestImportPack,
                       RequestSetSoundIcon, SetSoundCategory, SetSoundIcon, SetSoundTrim,
                       SetSoundVolume, SoundAdded, WaveformLoaded)

log = logging.getLogger(__name__)

ICON_UNREADABLE = ("Das Icon „{name}“ ist kein lesbares Bild. "
                   "Wähl ein PNG-, JPG-, BMP- oder WEBP-Bild oder lass das Icon leer.")
# today's SoundTile._change_icon (widgets.py, unchanged) shows f"{exc} {BAD_IMAGE_HINT}":
# exc is icons.IconError's default message, BAD_IMAGE_HINT its own hint text. Reproduced
# literally here so an icon-change failure reads exactly like it does in the Tk GUI.
ICON_CHANGE_UNREADABLE = (f"{icons.UNREADABLE_IMAGE_MESSAGE} "
                          "Wähl ein PNG-, JPG-, BMP- oder WEBP-Bild.")
AUDIO_UNREADABLE = ("„{name}“ konnte nicht gelesen werden. "
                    "Prüfe, ob es eine MP3 oder MP4 mit Tonspur ist, und wähle die Datei erneut.")
NOTHING_TO_EXPORT = "Noch keine Sounds zum Exportieren."
EXPORT_FAILED_WRITE = ("Export fehlgeschlagen: Die Datei konnte nicht geschrieben werden. "
                       "Prüf, ob der Zielordner existiert und beschreibbar ist.")
IMPORT_FAILED_READ = "Import fehlgeschlagen. Die Datei konnte nicht gelesen werden."
WAVEFORM_FAILED = ("Die Wellenform von „{name}“ konnte nicht geladen werden. "
                   "Prüf, ob die Datei noch da ist.")
TRIMMED = "„{name}“ gekürzt"
TRIM_CLEARED = "„{name}“: Zuschnitt entfernt"
EXPORT_TARGET_UNSAFE = ("Dieses Ziel liegt im Sound-Ordner der Bibliothek. "
                        "Wähl einen anderen Ordner, damit der Originalsound erhalten bleibt.")
EXPORT_TARGET_EXISTS = ("„{name}“ gibt es schon. Der Speichern-Dialog konnte das nicht "
                        "abfragen, weil die Endung „.mp3“ erst danach ergänzt wurde. "
                        "Wähl einen anderen Namen.")
MUSIC_SORTED_ONE = "1 langer Sound zählt jetzt als Musik."
MUSIC_SORTED = "{n} lange Sounds zählen jetzt als Musik."


def apply_measurement(sound: dict, measured: dict, keep_on_failure: bool = True) -> bool:
    """Stores a measurement on a sound: the loudness, the length (Klangbild K2) and -
    only while the sound has no category yet - the category by length. A choice made
    by hand is never overridden. True when this made the sound music (hint K7).

    Review fix: a re-measurement (K7 backfill) can fail on a sound that already has a
    good loudness from before - ffmpeg rejects the file, or the worker's exception path
    marks it {"failed": True}. That must never overwrite the good data (still stores a
    duration if one somehow came back with the failure) - this is the backfill's own
    default (`keep_on_failure=True`).

    K2: `_trim_ready`'s untrimmed re-measure (ClearSoundTrim, or a cut widened back to
    the full length) calls this with `keep_on_failure=False` instead - a failed
    remeasurement right after a TRIM CHANGE must not keep the old loudness, which
    belonged to the removed cut and would be wrong for the full file; it falls back to
    {"failed": True} (-> the safe UNMEASURED_GAIN_DB default) instead, same rule as
    apply_trim_measurement's own keep_on_failure."""
    measured = dict(measured)
    duration = measured.pop("duration", None)
    old = sound.get("loudness")
    failed = bool(measured.get("failed")) or "integrated" not in measured
    keep_old_loudness = keep_on_failure and failed and old is not None and "integrated" in old
    if not keep_old_loudness:
        sound["loudness"] = measured
    if duration is not None or not keep_old_loudness:
        # a clip without frame lines still gets an explicit "duration": None, so
        # needs_measuring() (key presence) does not retry it forever (review fix 2)
        sound["duration"] = duration
    if duration is None:
        return False
    if "category" in sound:
        return False
    category = levels.category_for_duration(duration)
    if category is None:
        return False
    sound["category"] = category
    return category == "music"


def apply_trim_measurement(sound: dict, measured: dict, full_duration: float | None = None,
                           full_measured: bool = False, keep_on_failure: bool = True) -> bool:
    """Stores the loudness of a sound's own cut (spec §3: normalization fits the trim,
    not the full file). `loudness.measure_dict(path, trim)` never returns "duration" -
    the cut's length is not the sound's own length - so on its own this never touches
    "duration"/"category". `full_duration`/`full_measured` come from a separate,
    untrimmed measurement that fills in a length the sound never had - an old sound
    from before Klangbild can still lack "duration" even with a trim already set - and
    derives the category from it exactly like apply_measurement, without overriding a
    category chosen by hand.

    `full_measured` distinguishes "no answer yet" from "an answer, but no length":
    False (the untrimmed measurement was never attempted, or came back None - no
    ffmpeg, a timeout, a missing file) leaves "duration" untouched, so the backfill
    tries again at the next start. True with `full_duration` still None (ffmpeg ran but
    rejected the untrimmed file, or its framelog had no frames) stores an explicit
    duration: None - like apply_measurement's own no-frames case - so needs_measuring()
    does not ask every single start for a length that will never come.

    A failed re-measurement (ffmpeg rejected the cut) keeps the good loudness from
    before, same rule as apply_measurement - for the backfill/re-verify path
    (`keep_on_failure` default True). F3 (final review): `_trim_ready` calls this with
    `keep_on_failure=False` instead - a failed measurement right after a TRIM CHANGE
    must not keep the old loudness, because that old measurement belongs to a
    different span (the previous cut) and would be wrong for the new one; it falls
    back to {"failed": True} (-> the safe UNMEASURED_GAIN_DB default) instead.

    Returns True when this made the sound music (K7 hint).

    F2: measure_dict(path, trim) can carry "duration" after all when a broken stored
    trim fell back to measuring the whole file - that duration is not the cut's own
    length and must never nest inside sound["loudness"], so it is popped here exactly
    like apply_measurement pops its own "duration" key."""
    measured = dict(measured)
    measured.pop("duration", None)
    old = sound.get("loudness")
    failed = bool(measured.get("failed")) or "integrated" not in measured
    keep_old_loudness = keep_on_failure and failed and old is not None and "integrated" in old
    if not keep_old_loudness:
        sound["loudness"] = measured
    if not full_measured or "duration" in sound:
        return False
    sound["duration"] = None if full_duration is None else round(full_duration, 2)
    if full_duration is None:
        return False
    if "category" in sound:
        return False
    category = levels.category_for_duration(full_duration)
    if category is None:
        return False
    sound["category"] = category
    return category == "music"


def needs_measuring(sound: dict) -> bool:
    """No loudness yet, or measured before Klangbild (no length) and not a file ffmpeg
    rejected - K7 measures those once more for their length."""
    measured = sound.get("loudness")
    if measured is None:
        return True
    return "duration" not in sound and not measured.get("failed")


def prepare_sound(data_dir: Path, sound_id: str, name: str, audio_path: Path,
                  icon_path: Path | None) -> dict:
    """File work for a new sound - worker thread, touches no config. Removes its own
    files again when anything fails, then re-raises."""
    dest_audio = data_dir / "sounds" / f"{sound_id}.mp3"
    dest_icon = data_dir / "icons" / f"{sound_id}.png"
    try:
        if audio_path.suffix.lower() == ".mp3":
            shutil.copy(audio_path, dest_audio)
        else:
            extract_audio(audio_path, dest_audio, fmt="mp3")
        if icon_path:
            icons.save_icon(icon_path, dest_icon)
        else:
            icons.placeholder_icon(name).save(dest_icon)
    except Exception:
        dest_audio.unlink(missing_ok=True)
        dest_icon.unlink(missing_ok=True)
        raise
    sound = {
        "id": sound_id,
        "name": name,
        "file": f"sounds/{sound_id}.mp3",
        "icon": f"icons/{sound_id}.png",
        "hotkey": None,
        "volume": 1.0,
    }
    # None = could not measure right now (no ffmpeg, timeout, ...): leave "loudness"
    # absent so the backfill measures it again at the next start or pack import.
    measured = loudness.measure_dict(dest_audio)
    if measured is not None:
        apply_measurement(sound, measured)
    return sound


class LibraryService:
    def __init__(self, core, dialogs=None):
        self._core = core
        # Tests setzen eine Attrappe ein; im Betrieb entstehen die nativen Dialoge erst
        # beim ersten Request (FileDialogs startet seinen Tk-Thread nur bei Bedarf).
        self._dialogs = dialogs if dialogs is not None else FileDialogs()
        self._loudness_running = False
        self._icon_rev: dict[str, int] = {}  # sound id -> revision, not persisted
        self._trim_rev: dict[str, int] = {}  # sound id -> newest trim change, not persisted
        core.handle(AddSound, self.add)
        core.handle(DeleteSound, self.delete)
        core.handle(RenameSound, self.rename)
        core.handle(SetSoundIcon, self.set_icon)
        core.handle(SetSoundVolume, self.set_volume)
        core.handle(SetSoundCategory, self.set_category)
        core.handle(ExportSounds, self.export)
        core.handle(ImportPack, self.import_pack)
        core.handle(RequestAddSound, self._request_add_sound)
        core.handle(RequestImportPack, self._request_import_pack)
        core.handle(RequestExportSounds, self._request_export_sounds)
        core.handle(RequestSetSoundIcon, self._request_set_sound_icon)
        core.handle(LoadWaveform, self.load_waveform)
        core.handle(PreviewTrim, self.preview_trim)
        core.handle(SetSoundTrim, self.set_trim)
        core.handle(ClearSoundTrim, self.clear_trim)
        core.handle(RequestExportTrimmed, self._request_export_trimmed)
        core.add_state("sounds", self._sounds_state, group="library")
        core.on_start(self.start_loudness_backfill)

    def _sounds_state(self) -> list[dict]:
        """Each sound plus its icon revision, so an interface knows to reload the
        image after a successful icon change - icon_rev is never persisted."""
        out = []
        for sound in self.sounds:
            snapshot = copy.deepcopy(sound)
            snapshot["icon_rev"] = self._icon_rev.get(sound["id"], 0)
            out.append(snapshot)
        return out

    @property
    def sounds(self) -> list[dict]:
        return self._core.store.data["sounds"]

    def _find(self, sound_id: str) -> dict | None:
        return config.find_sound(self._core.store.data, sound_id)

    def _save_and_publish(self) -> None:
        self._core.store.save_now()
        self._core.state_changed()
        self._core.changed("library")

    # ---- add ----

    def add(self, cmd: AddSound) -> None:
        sound_id = config.new_sound_id()
        self._core.notice(f"„{cmd.name}“ wird hinzugefügt …")
        icon_path = Path(cmd.icon_path) if cmd.icon_path else None
        self._core.workers.submit(self._prepare, sound_id, cmd.name, Path(cmd.path), icon_path)

    def _prepare(self, sound_id: str, name: str, audio_path: Path,
                 icon_path: Path | None) -> None:  # worker
        try:
            sound = prepare_sound(self._core.store.data_dir, sound_id, name, audio_path, icon_path)
        except Exception as exc:
            self._core.executor.submit(self._add_failed, audio_path, icon_path, exc)
            return
        self._core.executor.submit(self._commit, sound)

    def _commit(self, sound: dict) -> None:
        sound["name"] = config.unique_name(self._core.store.data, sound["name"])
        self.sounds.append(sound)
        self._save_and_publish()
        self._core.emit(SoundAdded(sound["id"]))
        self._core.notice(f"„{sound['name']}“ hinzugefügt")
        self._core.playback.preload([sound])

    def _add_failed(self, audio_path: Path, icon_path: Path | None, exc: Exception) -> None:
        if isinstance(exc, icons.IconError) and icon_path is not None:
            self._core.notice(ICON_UNREADABLE.format(name=icon_path.name), "error")
            return
        log.error("adding %s failed: %r", audio_path, exc)
        self._core.notice(AUDIO_UNREADABLE.format(name=audio_path.name), "error")

    # ---- requests: the page asks, Python opens the dialog ----

    def _request_add_sound(self, command) -> None:
        path = self._dialogs.pick_sound_file()
        if not path:
            return  # cancelled: nothing changes
        self.add(AddSound(path, Path(path).stem, None))

    def _request_import_pack(self, command) -> None:
        path = self._dialogs.pick_pack_file()
        if not path:
            return
        self.import_pack(ImportPack(path))

    def _request_export_sounds(self, command) -> None:
        if not self.sounds:
            self._core.notice(NOTHING_TO_EXPORT, "info")
            return
        if command.sound_id is not None:
            sound = self._find(command.sound_id)
            if sound is None:
                return
            chosen, default = (sound["id"],), f"{sound['name']}.ruckuspack"
        else:
            chosen = tuple(s["id"] for s in self.sounds)
            default = "sounds.ruckuspack"
        target = self._dialogs.ask_export_target(default)
        if not target:
            return
        self.export(ExportSounds(target, chosen))

    def _request_set_sound_icon(self, command) -> None:
        if self._find(command.sound_id) is None:
            return
        path = self._dialogs.pick_icon_file()
        if not path:
            return
        self.set_icon(SetSoundIcon(command.sound_id, path))

    # ---- change / delete ----

    def delete(self, cmd: DeleteSound) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        self._core.hotkeys.forget(sound)
        for rel in (sound["file"], sound["icon"]):
            try:
                (self._core.store.data_dir / rel).unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete %s", rel, exc_info=True)
        self._core.playback.forget(cmd.sound_id)
        self._core.store.data["sounds"] = [s for s in self.sounds if s["id"] != cmd.sound_id]
        self._icon_rev.pop(cmd.sound_id, None)
        self._save_and_publish()

    def rename(self, cmd: RenameSound) -> None:
        sound = self._find(cmd.sound_id)
        name = cmd.name.strip()
        if sound is None or not name:
            return
        sound["name"] = config.unique_name(self._core.store.data, name, exclude_id=sound["id"])
        self._save_and_publish()

    def set_volume(self, cmd: SetSoundVolume) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        sound["volume"] = round(config.clamp_volume(cmd.volume), 2)
        self._save_and_publish()

    def set_category(self, cmd: SetSoundCategory) -> None:
        """Klangbild K2: by hand; the backfill never overrides this again."""
        sound = self._find(cmd.sound_id)
        if sound is None or cmd.category not in levels.CATEGORIES:
            return
        sound["category"] = cmd.category
        self._save_and_publish()

    def set_icon(self, cmd: SetSoundIcon) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        dest = self._core.store.data_dir / sound["icon"]
        self._core.workers.submit(self._save_icon, cmd.sound_id, Path(cmd.icon_path), dest)

    def _save_icon(self, sound_id: str, source: Path, dest: Path) -> None:  # worker
        error = None
        try:
            icons.save_icon(source, dest)
        except icons.IconError:
            error = ICON_CHANGE_UNREADABLE
        except Exception:
            log.exception("saving icon %s failed", source)
            error = ICON_CHANGE_UNREADABLE
        self._core.executor.submit(self._icon_saved, sound_id, dest, error)

    def _icon_saved(self, sound_id: str, dest: Path, error: str | None) -> None:
        if error is not None:
            self._core.notice(error, "error")
            return
        if self._find(sound_id) is None:
            dest.unlink(missing_ok=True)  # the sound was deleted while the worker ran
            return
        self._icon_rev[sound_id] = self._icon_rev.get(sound_id, 0) + 1
        self._core.state_changed()
        self._core.changed("library")  # geht am Engpass _save_and_publish vorbei

    # ---- trim (C8): non-destructive, the file in sounds/ is never touched ----

    def _audio_path(self, sound: dict) -> Path:
        return self._core.store.data_dir / sound["file"]

    def _decode_uncut(self, sound_id: str, path: Path):  # worker
        """The whole file, ignoring any trim - the editor always shows all of it. Decoded
        per request instead of cached: a long file would hold hundreds of MB in the core.
        None (plus an error notice on the core thread) when it cannot be read."""
        try:
            return audio.decode_audio(path)
        except Exception:
            log.warning("decoding %s for the trim editor failed", path, exc_info=True)
            self._core.executor.submit(self._uncut_failed, sound_id)
            return None

    def _uncut_failed(self, sound_id: str) -> None:
        sound = self._find(sound_id)
        if sound is not None:
            self._core.notice(WAVEFORM_FAILED.format(name=sound["name"]), "error")

    def load_waveform(self, cmd: LoadWaveform) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        self._core.workers.submit(self._waveform, sound["id"], self._audio_path(sound))

    def _waveform(self, sound_id: str, path: Path) -> None:  # worker
        decoded = self._decode_uncut(sound_id, path)
        if decoded is None:
            return
        duration = round(len(decoded.samples) / decoded.samplerate, 3)
        event = WaveformLoaded(sound_id, duration, trimming.peaks(decoded.samples))
        self._core.executor.submit(self._emit_if_alive, event)

    def _emit_if_alive(self, event) -> None:
        """A decode that ran on a worker can outlive the sound (deleted meanwhile) -
        dropping its stale answer here mirrors playback._decoded's own guard."""
        if self._find(event.sound_id) is not None:
            self._core.emit(event)

    def preview_trim(self, cmd: PreviewTrim) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        self._core.workers.submit(self._preview_clip, sound["id"], self._audio_path(sound),
                                  cmd.start, cmd.end)

    def _preview_clip(self, sound_id: str, path: Path, start, end) -> None:  # worker
        decoded = self._decode_uncut(sound_id, path)
        if decoded is None:
            return
        error = trimming.check_trim(start, end, len(decoded.samples) / decoded.samplerate)
        if error is not None:
            self._core.executor.submit(self._core.notice, error, "hint")
            return
        clip = trimming.apply_trim(decoded.samples, decoded.samplerate,
                                   {"start": start, "end": end})
        self._core.executor.submit(self._play_clip_if_alive, sound_id, clip)

    def _play_clip_if_alive(self, sound_id: str, clip) -> None:
        """A deleted sound must not start a headphone-only preview once its decode
        finally comes back."""
        if self._find(sound_id) is not None:
            self._core.playback.play_clip(sound_id, clip)

    def set_trim(self, cmd: SetSoundTrim) -> None:
        self._change_trim(cmd.sound_id, (cmd.start, cmd.end))

    def clear_trim(self, cmd: ClearSoundTrim) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        if "trim" not in sound:
            # Nothing visible to remove - but this could be the sound's first-ever trim,
            # still computing on a worker from an earlier SetSoundTrim that has not
            # landed yet. Bump the revision anyway so that answer is dropped instead of
            # applying itself after this clear (Clear always wins).
            self._trim_rev[cmd.sound_id] = self._trim_rev.get(cmd.sound_id, 0) + 1
            return
        self._change_trim(cmd.sound_id, None)

    def _change_trim(self, sound_id: str, wanted: tuple | None) -> None:
        sound = self._find(sound_id)
        if sound is None:
            return
        rev = self._trim_rev.get(sound_id, 0) + 1
        self._trim_rev[sound_id] = rev
        self._core.workers.submit(self._prepare_trim, sound_id, self._audio_path(sound),
                                  wanted, rev)

    def _prepare_trim(self, sound_id: str, path: Path, wanted: tuple | None,
                      rev: int) -> None:  # worker
        """Check the cut against the real length, then measure the loudness of exactly
        the part that will play (spec §3: normalization must fit the cut). The full
        decoded length is kept alongside: an old sound from before Klangbild can still
        lack "duration", and this decode already knows it for free."""
        trim = None
        full_duration = None
        if wanted is not None:
            decoded = self._decode_uncut(sound_id, path)
            if decoded is None:
                return
            full_duration = len(decoded.samples) / decoded.samplerate
            start, end = wanted
            error = trimming.check_trim(start, end, full_duration)
            if error is not None:
                self._core.executor.submit(self._core.notice, error, "hint")
                return
            if not trimming.is_full_length(start, end, full_duration):
                trim = trimming.clean_trim({"start": start, "end": end})
        try:
            measured = loudness.measure_dict(path, trim)
        except Exception:
            log.exception("loudness measurement failed for sound %s", sound_id)
            measured = {"failed": True}
        self._core.executor.submit(self._trim_ready, sound_id, trim, measured, rev, full_duration)

    def _trim_ready(self, sound_id: str, trim: dict | None, measured: dict | None,
                    rev: int, full_duration: float | None = None) -> None:
        if rev != self._trim_rev.get(sound_id):
            return  # a newer SetSoundTrim/ClearSoundTrim is on its way
        sound = self._find(sound_id)
        if sound is None:
            return
        if trim is None:
            sound.pop("trim", None)
        else:
            sound["trim"] = trim
        if measured is None:
            sound.pop("loudness", None)  # transient: the next backfill measures it
        elif trim is None:
            # An untrimmed measurement DOES carry "duration" (it is the sound's own
            # full length again) - the normal path, same as any other re-measure, never
            # overriding a category already chosen by hand. K2: keep_on_failure=False -
            # a failed remeasure right after this very Clear must not keep loudness
            # measured for the previous (now removed) trim.
            apply_measurement(sound, measured, keep_on_failure=False)
        else:
            # `_prepare_trim` only reaches this branch (trim not None) after a
            # successful decode, so full_duration is always a real length here.
            # F3: keep_on_failure=False - a failed remeasure right after this very
            # trim change must not keep loudness measured for the *previous* span.
            apply_trim_measurement(sound, measured, full_duration, full_measured=True,
                                   keep_on_failure=False)
        self._save_and_publish()
        self._core.playback.reload(sound_id)
        text = TRIMMED if trim is not None else TRIM_CLEARED
        self._core.notice(text.format(name=sound["name"]))

    def _request_export_trimmed(self, cmd: RequestExportTrimmed) -> None:
        sound = self._find(cmd.sound_id)
        if sound is None:
            return
        target = self._dialogs.ask_audio_target(f"{sanitize_filename(sound['name'])}.mp3")
        if not target:
            return  # cancelled: nothing changes
        target_path = Path(target)
        if target_path.suffix.lower() != ".mp3":
            # a hand-typed path (or a dialog attrappe) can bypass the file picker's own
            # ".mp3" filter - append, never with_suffix() (that would eat a dotted name
            # like "Take 1.5" down to "Take 1.mp3"). Windows never asked about THIS
            # name (it only ever saw the one without ".mp3"), so an existing file here
            # must be refused instead of silently replaced.
            target_path = target_path.with_name(target_path.name + ".mp3")
            if target_path.exists():
                self._core.notice(EXPORT_TARGET_EXISTS.format(name=target_path.name), "error")
                return
        sounds_dir = self._core.store.data_dir / "sounds"
        resolved_target, resolved_sounds = target_path.resolve(), sounds_dir.resolve()
        if resolved_target == resolved_sounds or resolved_sounds in resolved_target.parents:
            self._core.notice(EXPORT_TARGET_UNSAFE, "error")
            return  # never overwrite anything the library itself manages
        trim = dict(sound["trim"]) if sound.get("trim") else None  # snapshot for the worker
        self._core.notice("Wird exportiert …")
        self._core.workers.submit(self._export_trimmed, self._audio_path(sound),
                                  target_path, trim)

    def _export_trimmed(self, src: Path, dest: Path, trim: dict | None) -> None:  # worker
        try:
            audio.export_trimmed(src, dest, trim)
        except ValueError:
            # export_trimmed's own dest == src guard - the safety net behind the
            # library-folder check above (e.g. a symlink neither of us resolved the same way).
            self._core.executor.submit(self._core.notice, EXPORT_TARGET_UNSAFE, "error")
            return
        except Exception:
            log.exception("exporting %s to %s failed", src, dest)
            self._core.executor.submit(self._core.notice, EXPORT_FAILED_WRITE, "error")
            return
        self._core.executor.submit(self._core.notice, f"Exportiert: {dest.name}", "info")

    # ---- packs ----

    def export(self, cmd: ExportSounds) -> None:
        ids = list(cmd.sound_ids) if cmd.sound_ids is not None else [s["id"] for s in self.sounds]
        if not ids:
            self._core.notice(NOTHING_TO_EXPORT, "info")
            return
        wanted = set(ids)
        # the worker gets a snapshot, never the live config
        snapshot = {"sounds": [dict(s) for s in self.sounds if s["id"] in wanted]}
        self._core.notice("Wird exportiert …")
        self._core.workers.submit(self._export, snapshot, ids, Path(cmd.target_path))

    def _export(self, snapshot: dict, ids: list[str], dest: Path) -> None:  # worker
        try:
            export_pack(snapshot, self._core.store.data_dir, ids, dest)
        except PackError as exc:
            self._core.executor.submit(self._core.notice, f"Export fehlgeschlagen: {exc}", "error")
            return
        except Exception:
            log.exception("export failed")
            self._core.executor.submit(self._core.notice, EXPORT_FAILED_WRITE, "error")
            return
        self._core.executor.submit(self._core.notice, f"Exportiert: {dest.name}", "info")

    def import_pack(self, cmd: ImportPack) -> None:
        existing = {s["name"] for s in self.sounds}
        self._core.notice("Paket wird importiert …")
        self._core.workers.submit(self._import, Path(cmd.path), existing)

    def _import(self, path: Path, existing: set[str]) -> None:  # worker
        try:
            new_sounds = read_pack(path, existing, self._core.store.data_dir)
        except PackError as exc:
            self._core.executor.submit(self._core.notice, str(exc), "error")
            return
        except Exception:
            log.exception("import failed")
            self._core.executor.submit(self._core.notice, IMPORT_FAILED_READ, "error")
            return
        self._core.executor.submit(self._imported, new_sounds)

    def _imported(self, new_sounds: list[dict]) -> None:
        # the list may have changed while the worker ran: dedupe against the live one
        for sound in new_sounds:
            sound["name"] = config.unique_name(self._core.store.data, sound["name"])
            self.sounds.append(sound)
        self._save_and_publish()
        self._core.playback.preload(new_sounds)
        self.start_loudness_backfill()
        self._core.notice(f"{len(new_sounds)} Sounds importiert. "
                          "Hotkeys vergibst du per Rechtsklick.", "info")

    # ---- loudness backfill ----

    def start_loudness_backfill(self) -> None:
        """Measure every sound without loudness on one worker (older configs, pack
        imports, transient failures of an earlier run), and sounds from before
        Klangbild without a length (K7)."""
        if self._loudness_running:
            return
        todo = [(s["id"], self._core.store.data_dir / s["file"],
                dict(s["trim"]) if s.get("trim") else None,
                "duration" in s)
                for s in self.sounds if needs_measuring(s)]
        if not todo:
            return
        self._loudness_running = True
        self._core.workers.submit(self._measure_all, todo)

    def _measure_all(self, todo: list[tuple[str, Path, dict | None, bool]]) -> None:  # worker
        results = []
        for sound_id, path, trim, has_duration in todo:
            try:
                measured = loudness.measure_dict(path, trim)
            except Exception:
                log.exception("loudness measurement failed for sound %s", sound_id)
                measured = {"failed": True}
            full_duration = None
            full_measured = False
            if trim is not None and not has_duration:
                # An old sound from before Klangbild can still lack "duration" even with
                # a trim already set - measure_dict(path, trim) never carries one (the
                # cut's length is not the sound's). One extra, untrimmed measurement
                # gets the real length. `full_measured` tells _measured() an answer
                # actually came back (even a rejection): only a transient None (no
                # ffmpeg, a timeout) leaves "duration" untouched for the next retry.
                try:
                    full = loudness.measure_dict(path)
                except Exception:
                    full = None
                if full is not None:
                    full_measured = True
                    full_duration = full.get("duration")
            # `trim` travels with its own result as a snapshot: by the time this lands,
            # a SetSoundTrim/ClearSoundTrim may already have changed and re-measured the
            # sound itself (review fix, race) - _measured() drops a result whose
            # snapshot no longer matches the live trim instead of overwriting that
            # newer answer with this now-stale one.
            results.append((sound_id, measured, full_duration, full_measured, trim))
        self._core.executor.submit(self._measured, results)

    def _measured(self, results: list[tuple[str, dict | None, float | None, bool, dict | None]]) -> None:
        self._loudness_running = False
        measured_ids = {sound_id for sound_id, *_rest in results}
        new_music = 0
        for sound_id, measured, full_duration, full_measured, snapshot_trim in results:
            if measured is None:
                continue  # transient: stays absent, retried at the next start or import
            sound = self._find(sound_id)
            if sound is None:
                continue
            if sound.get("trim") != snapshot_trim:
                continue  # a trim change landed first and already re-measured this sound
            if snapshot_trim:
                if apply_trim_measurement(sound, measured, full_duration, full_measured):
                    new_music += 1
                continue
            if apply_measurement(sound, measured):
                new_music += 1
        self._save_and_publish()
        if new_music:
            # K7: say what changed, once - the context menu switches back
            self._core.notice(MUSIC_SORTED_ONE if new_music == 1
                              else MUSIC_SORTED.format(n=new_music), "info")
        # Re-run only for sounds that arrived WHILE this worker ran; a sound that came
        # back None must not restart the backfill at once (endless retry loop).
        if any(s["id"] not in measured_ids for s in self.sounds if needs_measuring(s)):
            self.start_loudness_backfill()
