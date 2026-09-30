"""Wiedergabe: Sounds abspielen, vorladen, "spielt gerade" verfolgen, stoppen.

Kern-Thread: Entscheidungen und Zustand (missing, pending, playing).
Geraete-Thread: alles, was die AudioEngine mit PortAudio tut - play, stop, stop_all
und playing_ids (raeumt fertige Wiedergaben ab und schliesst dabei Streams).
Worker: Dekodieren (engine.preload; der Cache der Engine ist per Lock geschuetzt).

Ein Play, das hinter einem langen Geraete-Umbau in der Warteschlange stand, wird nach
MAX_PLAY_WAIT_S verworfen statt verspaetet loszuplatzen.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import config, levels
from .layout import needle_for_index
from .protocol import Play, PlaybackEnded, PlaybackStarted, SoundMissing, Stop, StopAll

log = logging.getLogger(__name__)

POLL_S = 0.1
MAX_PLAY_WAIT_S = 2.0
# Spamming one sound is one use: another start of the same sound within this many
# seconds after the last counted start does not raise `plays` (spec A3).
USE_WINDOW_S = 2.0
# Bigger files (~10 min at 192 kbit/s) are not decoded at start: decoded audio costs
# ~23 MB per minute, a 2-hour mix alone would hold 3 GB. They decode on the first play.
LAZY_DECODE_BYTES = 15 * 1024 * 1024
PLAY_FAILED = "Wiedergabe fehlgeschlagen. Prüf das Ausgabegerät in den Windows-Soundeinstellungen."
PLAY_DROPPED = ("Sound verworfen – die Audiogeräte wurden gerade neu eingerichtet. "
                "Noch einmal drücken.")
MISSING = "Datei fehlt: „{name}“. Sound löschen und neu hinzufügen."


class PlaybackService:
    def __init__(self, core):
        self._core = core
        self.missing: set[str] = set()
        self.playing: set[str] = set()
        # waiting for an on-demand decode -> (volume, preview)
        self._pending: dict[str, tuple[float, bool]] = {}
        self._decoding: set[str] = set()
        self._poll_gen = 0  # stale device answers after stop_all carry an older number
        self._polling = False
        self._play_seq: dict[str, int] = {}  # per-sound stop counter, bumped by stop()
        self._clock = time.monotonic  # tests swap in a fake clock
        self._last_counted: dict[str, float] = {}  # sound id -> last start that counted
        core.handle(Play, lambda cmd: self.play(cmd.sound_id, cmd.volume, cmd.preview))
        core.handle(Stop, lambda cmd: self.stop(cmd.sound_id))
        core.handle(StopAll, lambda _cmd: self.stop_all())
        core.add_state("playback", self.snapshot)
        core.on_start(lambda: self.preload(self._sounds()))
        core.on_shutdown(lambda: self._core.devices.submit(self._core.engine.stop_all))

    def _sounds(self) -> list[dict]:
        return self._core.store.data["sounds"]

    def snapshot(self) -> dict:
        return {"playing": sorted(self.playing), "missing": sorted(self.missing)}

    # ---- decoding: core -> worker -> core ----

    def preload(self, sounds: list[dict]) -> None:
        """Decode the most useful sounds first: a sound with a hotkey can fire the
        instant the user presses it, so those go first; among the rest, the
        most-played sounds are the ones most likely to be pressed again right after
        start. Ties keep the original list order. The worker pool is FIFO
        (executors.WorkerPool.submit -> a plain queue.Queue, popped in submission
        order by whichever of its threads is free next), so submission order here
        is the decode priority."""
        ordered = sorted(
            enumerate(sounds),
            key=lambda pair: (0 if pair[1].get("hotkey") else 1, -pair[1].get("plays", 0), pair[0]),
        )
        jobs = [(s["id"], self._core.store.data_dir / s["file"], s.get("trim"))
                for _index, s in ordered if s["id"] not in self._decoding]
        for sound_id, _path, _trim in jobs:  # mark first: inline workers answer immediately
            self._decoding.add(sound_id)
        for sound_id, path, trim in jobs:
            self._core.workers.submit(self._decode, sound_id, path, True, trim)

    def _decode(self, sound_id: str, path: Path, up_front: bool = False,
               trim: dict | None = None) -> None:  # worker
        if up_front:
            try:
                too_long = path.stat().st_size > LAZY_DECODE_BYTES
            except OSError:
                too_long = False  # a missing file is reported by the decode below
            if too_long:
                self._core.executor.submit(self._skipped, sound_id)
                return
        try:
            self._core.engine.preload(sound_id, path, trim)
            ok = True
        except Exception:
            log.warning("decoding %s failed", path, exc_info=True)
            ok = False
        self._core.executor.submit(self._decoded, sound_id, ok, trim)

    def _skipped(self, sound_id: str) -> None:  # core
        """The up-front decode was skipped (file too big for LAZY_DECODE_BYTES): unlike
        _decoded, no decode ran and no answer is coming, so a Play that arrived while
        this sound sat in the worker queue and got marked pending would otherwise be
        lost forever. Replay it - it now takes the normal on-demand decode path."""
        self._decoding.discard(sound_id)
        pending = self._pending.pop(sound_id, None)
        if pending is not None:
            self.play(sound_id, *pending)

    def _decoded(self, sound_id: str, ok: bool, trim: dict | None = None) -> None:  # core
        self._decoding.discard(sound_id)
        sound = config.find_sound(self._core.store.data, sound_id)
        if sound is None:
            self._core.engine.forget(sound_id)  # deleted while decoding
            self._pending.pop(sound_id, None)
            return
        if ok and sound.get("trim") != trim:
            # The trim changed while this decode ran (C8 reload): its samples are stale.
            self._core.engine.forget(sound_id)
            self._decoding.add(sound_id)
            self._core.workers.submit(self._decode, sound_id,
                                      self._core.store.data_dir / sound["file"], False,
                                      sound.get("trim"))
            return
        changed = (sound_id in self.missing) == ok
        if ok:
            self.missing.discard(sound_id)
        else:
            self.missing.add(sound_id)
            self._core.emit(SoundMissing(sound_id))
        if changed:
            self._core.state_changed()
        pending = self._pending.pop(sound_id, None)
        if pending is None:
            return
        if ok:
            self.play(sound_id, *pending)
        else:
            self._hint_missing(sound_id)

    # ---- playing ----

    def play(self, sound_id: str, volume: float | None = None, preview: bool = False) -> None:
        sounds = self._sounds()
        index = next((i for i, s in enumerate(sounds) if s["id"] == sound_id), None)
        if index is None:
            return
        if sound_id in self.missing:
            self._hint_missing(sound_id)
            return
        sound = sounds[index]
        # second guard behind packs/VolumeDialog: config.json may be hand-edited
        volume = config.clamp_volume(sound.get("volume", 1.0) if volume is None else volume)
        # `sound_id in self._decoding` (not just `is_loaded`): a worker can write the
        # engine cache (making is_loaded True) before the core thread runs _decoded to
        # notice a trim change meanwhile and redo the decode - a real race between the
        # worker and core threads. While `_decoding` still holds the sound, a decode for
        # it has not resolved yet, so the cache cannot be trusted.
        if sound_id in self._decoding or not self._core.engine.is_loaded(sound_id):
            self._pending[sound_id] = (volume, preview)
            if sound_id not in self._decoding:
                self._decoding.add(sound_id)
                self._core.workers.submit(self._decode, sound_id,
                                          self._core.store.data_dir / sound["file"],
                                          False, sound.get("trim"))
            return
        gain = levels.play_gain(sound, volume, self._core.store.data)
        music = levels.sound_category(sound) == "music"
        needle = needle_for_index(index, len(sounds))
        # a play token: (stop-all generation, per-sound stop counter) as they stood at
        # submit time - a later StopAll or Stop(sound_id) bumps one of them, so a stale
        # answer from the device thread is recognized and dropped in _started.
        token = (self._poll_gen, self._play_seq.get(sound_id, 0))
        self._core.devices.submit(self._play_on_device, sound_id, gain, needle,
                                  time.monotonic(), token, preview, music)

    def _play_on_device(self, sound_id: str, gain: float, needle: float,
                        queued_at: float, token: tuple[int, int],
                        preview: bool = False, music: bool = False) -> None:  # device thread
        if time.monotonic() - queued_at > MAX_PLAY_WAIT_S:
            self._core.executor.submit(self._core.notice, PLAY_DROPPED, "hint")
            return
        try:
            if preview:
                started = self._core.engine.play(sound_id, volume=gain, monitor_only=True, music=music)
            else:
                started = self._core.engine.play(sound_id, volume=gain, music=music)
        except KeyError:
            return  # forgotten between is_loaded and play (deleted)
        except Exception:
            log.exception("playback of %s failed", sound_id)
            self._core.executor.submit(self._core.notice, PLAY_FAILED, "hint")
            return
        if started is None:
            return  # K6: no headphone, no mixer/VoiceMeeter - nothing started, stay silent
        self._core.executor.submit(self._started, sound_id, needle, token, preview)

    def _started(self, sound_id: str, needle: float, token: tuple[int, int],
                preview: bool = False) -> None:  # core
        sound = config.find_sound(self._core.store.data, sound_id)
        if sound is None:
            return  # the sound was deleted while the play was in flight
        if token != (self._poll_gen, self._play_seq.get(sound_id, 0)):
            return  # a Stop(sound_id) or StopAll landed after this play was submitted
        if not preview:
            # A preview (VolumeDialog) is not a "play" for preload priority purposes,
            # and spamming within USE_WINDOW_S is one use, not many.
            now = self._clock()
            last = self._last_counted.get(sound_id)
            if last is None or now - last >= USE_WINDOW_S:
                self._last_counted[sound_id] = now
                sound["plays"] = sound.get("plays", 0) + 1
                self._core.store.save_soon()  # debounced: clicks can come in fast
        self.playing.add(sound_id)
        self._core.emit(PlaybackStarted(sound_id, needle))
        self._core.state_changed()
        if not self._polling:
            self._polling = True
            self._core.executor.call_later(POLL_S, self._request_poll, self._poll_gen)

    def _request_poll(self, gen: int) -> None:  # core timer
        if gen != self._poll_gen:
            return
        self._core.devices.submit(self._poll_on_device, gen)

    def _poll_on_device(self, gen: int) -> None:  # device thread
        try:
            still = self._core.engine.playing_ids()
        except Exception:
            log.exception("polling playing_ids failed")
            still = set()
        self._core.executor.submit(self._polled, gen, still)

    def _polled(self, gen: int, still: set[str]) -> None:  # core
        if gen != self._poll_gen:
            return
        ended = self.playing - still
        self.playing &= still
        for sound_id in sorted(ended):
            self._core.emit(PlaybackEnded(sound_id))
        if ended:
            self._core.state_changed()
        if self.playing:
            self._core.executor.call_later(POLL_S, self._request_poll, gen)
        else:
            self._polling = False

    # ---- trim (C8) ----

    def reload(self, sound_id: str) -> None:
        """The sound's trim changed: drop the decoded audio and decode it again with the
        new cut. A decode already running notices the change itself in _decoded."""
        sound = config.find_sound(self._core.store.data, sound_id)
        if sound is None:
            return
        self._core.engine.forget(sound_id)
        if sound_id in self._decoding:
            return
        self.preload([sound])

    def play_clip(self, sound_id: str, samples) -> None:
        """Trim preview (Z3): exactly `samples`, only on the headphones, at the sound's
        own volume and normalization. A preview: no `plays`, no use window."""
        sounds = self._sounds()
        index = next((i for i, s in enumerate(sounds) if s["id"] == sound_id), None)
        if index is None:
            return
        sound = sounds[index]
        gain = levels.play_gain(sound, config.clamp_volume(sound.get("volume", 1.0)),
                                self._core.store.data)
        needle = needle_for_index(index, len(sounds))
        token = (self._poll_gen, self._play_seq.get(sound_id, 0))
        self._core.devices.submit(self._clip_on_device, sound_id, samples, gain, needle, token)

    def _clip_on_device(self, sound_id: str, samples, gain: float, needle: float,
                        token: tuple[int, int]) -> None:  # device thread
        try:
            started = self._core.engine.play_clip(sound_id, samples, gain)
        except Exception:
            log.exception("trim preview of %s failed", sound_id)
            self._core.executor.submit(self._core.notice, PLAY_FAILED, "hint")
            return
        if started is None:
            return  # K6: no headphone, no mixer - nothing started, no PlaybackStarted
        self._core.executor.submit(self._started, sound_id, needle, token, True)

    # ---- stopping ----

    def stop(self, sound_id: str) -> None:
        self._pending.pop(sound_id, None)
        self._play_seq[sound_id] = self._play_seq.get(sound_id, 0) + 1
        self._core.devices.submit(self._core.engine.stop, sound_id)
        if sound_id in self.playing:
            self.playing.discard(sound_id)
            self._core.emit(PlaybackEnded(sound_id))
            self._core.state_changed()

    def stop_all(self) -> None:
        self._pending.clear()
        self._poll_gen += 1
        self._polling = False
        ended = sorted(self.playing)
        self.playing.clear()
        self._core.devices.submit(self._core.engine.stop_all)
        for sound_id in ended:
            self._core.emit(PlaybackEnded(sound_id))
        if ended:
            self._core.state_changed()

    def forget(self, sound_id: str) -> None:
        """A sound was deleted: drop everything playback knows about it."""
        self._pending.pop(sound_id, None)
        self.missing.discard(sound_id)
        self._last_counted.pop(sound_id, None)
        self._core.engine.forget(sound_id)

    def _hint_missing(self, sound_id: str) -> None:
        sound = config.find_sound(self._core.store.data, sound_id)
        self._core.notice(MISSING.format(name=sound["name"] if sound else "Sound"))
