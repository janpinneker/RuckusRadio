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

from . import config, icons, loudness
from .audio import extract_audio
from .filedialogs import FileDialogs
from .packs import PackError, export_pack
from .packs import import_pack as read_pack
from .protocol import (AddSound, DeleteSound, ExportSounds, ImportPack, RenameSound,
                       RequestAddSound, RequestExportSounds, RequestImportPack,
                       RequestSetSoundIcon, SetSoundIcon, SetSoundVolume, SoundAdded)

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
        sound["loudness"] = measured
    return sound


class LibraryService:
    def __init__(self, core, dialogs=None):
        self._core = core
        # Tests setzen eine Attrappe ein; im Betrieb entstehen die nativen Dialoge erst
        # beim ersten Request (FileDialogs startet seinen Tk-Thread nur bei Bedarf).
        self._dialogs = dialogs if dialogs is not None else FileDialogs()
        self._loudness_running = False
        self._icon_rev: dict[str, int] = {}  # sound id -> revision, not persisted
        core.handle(AddSound, self.add)
        core.handle(DeleteSound, self.delete)
        core.handle(RenameSound, self.rename)
        core.handle(SetSoundIcon, self.set_icon)
        core.handle(SetSoundVolume, self.set_volume)
        core.handle(ExportSounds, self.export)
        core.handle(ImportPack, self.import_pack)
        core.handle(RequestAddSound, self._request_add_sound)
        core.handle(RequestImportPack, self._request_import_pack)
        core.handle(RequestExportSounds, self._request_export_sounds)
        core.handle(RequestSetSoundIcon, self._request_set_sound_icon)
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
        imports, transient failures of an earlier run)."""
        if self._loudness_running:
            return
        todo = [(s["id"], self._core.store.data_dir / s["file"])
                for s in self.sounds if "loudness" not in s]
        if not todo:
            return
        self._loudness_running = True
        self._core.workers.submit(self._measure_all, todo)

    def _measure_all(self, todo: list[tuple[str, Path]]) -> None:  # worker
        results = []
        for sound_id, path in todo:
            try:
                measured = loudness.measure_dict(path)
            except Exception:
                log.exception("loudness measurement failed for sound %s", sound_id)
                measured = {"failed": True}
            results.append((sound_id, measured))
        self._core.executor.submit(self._measured, results)

    def _measured(self, results: list[tuple[str, dict | None]]) -> None:
        self._loudness_running = False
        measured_ids = {sound_id for sound_id, _ in results}
        for sound_id, measured in results:
            if measured is None:
                continue  # transient: stays absent, retried at the next start or import
            sound = self._find(sound_id)
            if sound is not None:
                sound["loudness"] = measured
        self._save_and_publish()
        # Re-run only for sounds that arrived WHILE this worker ran; a sound that came
        # back None must not restart the backfill at once (endless retry loop).
        if any(s["id"] not in measured_ids for s in self.sounds if "loudness" not in s):
            self.start_loudness_backfill()
