"""Mehrere Ziele, ein Mikrofon: wer hoert was.

Mit einem einzigen virtuellen Kabel hoert jedes Programm dieselbe Mischung. Getrennte
Ziele brauchen getrennte Kabel, und dieses Modul verteilt darauf: `SinkGroup` besitzt
den EINEN Mikrofon-`InputStream` und legt jeden Block in die Queue JEDES Ziels. Jedes
`Target` haelt seinen eigenen `OutputStream` und mischt daraus seine eigene Mischung -
CS bekommt Stimme und Sounds, Discord nur die Stimme, die Kopfhoerer nur die Sounds.

Nach aussen behaelt `SinkGroup` die Schnittstelle des frueheren Einzel-Ziel-Mixers:
`add_source`, `remove`, `stop_all_sources`, `running`, `stop`. `AudioEngine` merkt den
Unterschied nicht.

Eigene Queue je Ziel ist der Punkt: ein langsames Ziel darf keinem anderen Bloecke
wegnehmen. `MixSource` und der Misch-Kern bleiben unveraendert in `virtualmic.py`,
jedes Ziel bekommt nur seine eigene `MixSource` ueber demselben Sample-Array.

Threading: beide Callbacks laufen auf PortAudio-Threads. Sie fassen die Queues, die
Quellenliste (kurzer Lock), einfache Floats sowie je Ziel dessen eigenen Limiter- und
Ducker-Zustand an, dazu den gemeinsamen `VoiceState` - von jedem Ausgabe-Callback
gelesen, geschrieben nur vom Mikrofon-Callback. Nie Tk, nie die Live-Config.
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

from . import dynamics
from .config import MONITOR_KEY
from .layout import MONITOR_LABEL
from .micfilter import MicChain
from .virtualmic import (BLOCKSIZE, MIC_QUEUE_BLOCKS, TARGET_CHANNELS, TARGET_SAMPLERATE,
                         MixSource, mix_blocks, to_stereo)

log = logging.getLogger(__name__)

SOUND_CEILING_DB = -1.0


class VoiceState:
    """Ob gerade gesprochen wird: vom Mikrofon-Callback geschrieben, von jedem Ziel
    gelesen. Ein einzelnes bool, in CPython atomar."""

    __slots__ = ("speaking",)

    def __init__(self):
        self.speaking = False


def _sum_blocks(blocks: list[np.ndarray], frames: int) -> np.ndarray:
    """Wie mix_blocks, aber ohne Clipping: der Limiter soll die echte Summe sehen,
    sonst waere eine Ueberlagerung schon hart abgeschnitten, bevor er greift."""
    out = np.zeros((frames, TARGET_CHANNELS), dtype=np.float32)
    for block in blocks:
        n = min(frames, len(block))
        if n:
            out[:n] += block[:n]
    return out


class _AllDone:
    """Sieht aus wie threading.Event, fragt aber die Quellen.

    Ein eigenes Event muesste jemand setzen, sobald die letzte Quelle fertig ist - und
    das wuesste nur `MixSource`, die unveraendert bleiben soll. Also wird nicht gesetzt,
    sondern gefragt. `AudioEngine._reap` ruft `is_set()` ohnehin regelmaessig."""

    __slots__ = ("_sources",)

    def __init__(self, sources: list[MixSource]):
        self._sources = sources

    def is_set(self) -> bool:
        return all(s.finished.is_set() for s in self._sources)

    def set(self) -> None:
        for source in self._sources:
            source.finished.set()

    def wait(self, timeout=None) -> bool:
        for source in self._sources:
            source.finished.wait(timeout)
        return self.is_set()


class GroupSource:
    """Eine Wiedergabe ueber alle Ziele. `finished` ist gesetzt, wenn jedes Ziel fertig ist."""

    __slots__ = ("_sources", "finished")

    def __init__(self, sources: list[MixSource]):
        self._sources = sources
        self.finished = _AllDone(sources)

    def per_target(self) -> list[MixSource]:
        return list(self._sources)


class Target:
    """Ein Ziel: ein Ausgabegeraet plus die vier Einstellungen dafuer."""

    def __init__(self, key: str, label: str, device: int, settings: dict,
                 blocksize: int = BLOCKSIZE):
        self.key = key
        self.label = label
        self.device = device
        self.blocksize = blocksize
        self.mic = bool(settings["mic"])
        self.mic_gain = float(settings["mic_gain"])
        self.sounds = bool(settings["sounds"])
        self.sounds_gain = float(settings["sounds_gain"])
        self.dropped_blocks = 0  # drift: mic ran ahead of this target's clock
        self.starved_blocks = 0  # drift: mic ran behind, silence inserted
        self.open_failed = False
        # Set by the group: False while no microphone stream is running, so a target
        # does not count starved blocks for a microphone that was never opened.
        self._mic_wanted = True
        # Set by the group from SinkGroup.mic_muted: while True, the output callback
        # neither reads the mic queue nor counts starved blocks - the same "mic active
        # and not muted" guard the single-target mixer needed before this rework.
        # Without this, muting only stops the INPUT side (_distribute_mic), and every
        # mic-enabled target's output callback keeps calling get_nowait() on a queue
        # that nothing refills, inflating starved_blocks for as long as the mute lasts.
        self._mic_muted = False
        self._mic_queue: queue.Queue = queue.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self._sources: list[MixSource] = []
        self._lock = threading.Lock()
        self._stream = None
        # Pegel der Sound-Summe, gesetzt von SinkGroup.apply_levels. Der Kopfhoerer
        # behaelt 1.0 und keinen Ducker: "Sounds unter Stimme" und Ducking gelten fuer
        # das, was andere hoeren, nicht fuer das eigene Mithoeren.
        self.sounds_offset = 1.0
        self.ducker: dynamics.Ducker | None = None
        self.limiter = dynamics.Limiter(ceiling_db=SOUND_CEILING_DB,
                                        samplerate=TARGET_SAMPLERATE)
        self.voice = VoiceState()

    # ---- lifecycle ----

    @property
    def wants_stream(self) -> bool:
        """Nothing switched on means nothing to send: keep the device free."""
        return self.mic or self.sounds

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _open_stream(self):
        return sd.OutputStream(
            samplerate=TARGET_SAMPLERATE, device=self.device, channels=TARGET_CHANNELS,
            dtype="float32", blocksize=self.blocksize, callback=self._output_callback,
        )

    def open(self) -> bool:
        """True when the stream is running. A failure only disables THIS target."""
        if self._stream is not None:
            return True
        if not self.wants_stream:
            return False
        try:
            self._stream = self._open_stream()
            self._stream.start()
            self.open_failed = False
        except Exception:
            log.warning("output for %s could not be opened", self.key, exc_info=True)
            self._stream = None
            self.open_failed = True
            return False
        return True

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort(ignore_errors=True)
            stream.close(ignore_errors=True)
        except Exception:
            log.debug("closing the stream for %s failed", self.key, exc_info=True)

    def apply(self, settings: dict) -> None:
        """Take new settings and open or close the stream to match."""
        self.mic = bool(settings["mic"])
        self.mic_gain = float(settings["mic_gain"])
        self.sounds = bool(settings["sounds"])
        self.sounds_gain = float(settings["sounds_gain"])
        if self.wants_stream:
            self.open()
        else:
            self.close()

    # ---- sources ----

    def add_source(self, samples: np.ndarray, gain: float) -> MixSource | None:
        """This target's own MixSource over the SHARED sample array. None when the
        target does not take sounds, so nothing is decoded or mixed for nothing."""
        if not self.sounds:
            return None
        source = MixSource(samples, gain * self.sounds_gain)
        with self._lock:
            self._sources.append(source)
        return source

    def remove(self, source: MixSource | None) -> None:
        if source is None:
            return
        source.stop()
        with self._lock:
            if source in self._sources:
                self._sources.remove(source)
        source.finished.set()

    def clear_sources(self) -> None:
        with self._lock:
            sources, self._sources = self._sources, []
        for source in sources:
            source.stop()
            source.finished.set()

    # ---- mic ----

    def push_mic(self, block: np.ndarray) -> None:
        """Called from the group's mic callback, once per target."""
        if not self.mic:
            return
        try:
            self._mic_queue.put_nowait(block)
        except queue.Full:
            # This target's clock runs slower than the mic: drop the oldest block
            # instead of letting latency grow without bound.
            try:
                self._mic_queue.get_nowait()
                self._mic_queue.put_nowait(block)
            except (queue.Empty, queue.Full):
                pass
            self.dropped_blocks += 1

    # ---- callback (PortAudio thread) ----

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        blocks: list[np.ndarray] = []
        if self.mic and self._mic_wanted and not self._mic_muted:
            try:
                blocks.append(self._mic_queue.get_nowait() * self.mic_gain)
            except queue.Empty:
                self.starved_blocks += 1

        with self._lock:
            sources = list(self._sources)
        done: list[MixSource] = []
        chunks: list[np.ndarray] = []
        for source in sources:
            chunk = source.pull(frames)
            if chunk is None:
                done.append(source)
            else:
                chunks.append(chunk)
        if done:
            with self._lock:
                self._sources = [s for s in self._sources if s not in done]

        # The ducker runs every block, sound or not, so a sound that starts mid-sentence
        # is already ducked in its first block.
        ducker = self.ducker
        duck = ducker.next_ramp(self.voice.speaking, frames) if ducker is not None else None
        if chunks:
            bus = _sum_blocks(chunks, frames) * np.float32(self.sounds_offset)
            if duck is not None:
                bus = bus * duck
            blocks.append(self.limiter.process(bus))
        else:
            self.limiter.reset()

        outdata[:] = mix_blocks(blocks, frames)


class SinkGroup:
    """Owns the one microphone stream and fans every block out to the targets."""

    def __init__(self, targets: list[Target], mic_device: int | None,
                 blocksize: int = BLOCKSIZE):
        self.targets = list(targets)
        self.mic_device = mic_device
        self.blocksize = blocksize
        self.mic_active = False
        # Stimme einmal aufbereiten, bevor sie verteilt wird. Sounds laufen nie durch
        # diese Kette, sie werden je Ziel getrennt summiert.
        self.mic_chain = MicChain(TARGET_SAMPLERATE)
        self.voice = VoiceState()
        for target in self.targets:
            target.voice = self.voice
        # Global mute, independent of any per-target "mic" switch: compat with the
        # dock's "Mikro an/aus" button (gui.toggle_mic), which predates the matrix.
        self.mic_muted = False
        self._in_stream = None
        self._alive = False
        self._handles: list[GroupSource] = []
        self._handle_lock = threading.Lock()

    # ---- lifecycle ----

    def start(self) -> None:
        """Opens every target that wants a stream, then the microphone. Never raises:
        a target that fails is marked and skipped, a missing mic only disables voice.

        _mic_wanted/_mic_muted are pushed to every target BEFORE any stream opens, not
        after: a target's OutputStream can start invoking its callback the instant
        open() returns, and a callback firing in the gap between open() and the state
        update would either count a never-started mic as starved or, while muted, keep
        pulling from a queue nothing refills - both look exactly like drift instead of
        the deliberate state they are."""
        self._alive = True
        for target in self.targets:
            target._mic_wanted = False
            target._mic_muted = self.mic_muted
        for target in self.targets:
            target.open()
        if self.mic_device is not None:
            self._start_mic()

    def _start_mic(self) -> None:
        try:
            self._in_stream = sd.InputStream(
                samplerate=TARGET_SAMPLERATE, device=self.mic_device, channels=1,
                dtype="float32", blocksize=self.blocksize, callback=self._input_callback,
            )
            self._in_stream.start()
            self.mic_active = True
        except Exception:
            log.warning("microphone %s could not be opened, running without voice",
                        self.mic_device, exc_info=True)
            self._in_stream = None
            self.mic_active = False
        for target in self.targets:
            target._mic_wanted = self.mic_active
            target._mic_muted = self.mic_muted

    def stop(self) -> None:
        stream, self._in_stream = self._in_stream, None
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
                stream.close(ignore_errors=True)
            except Exception:
                log.debug("closing the mic stream failed", exc_info=True)
        self.mic_active = False
        for target in self.targets:
            target.clear_sources()
            target.close()
        with self._handle_lock:
            handles, self._handles = self._handles, []
        for handle in handles:
            handle.finished.set()
        self._alive = False

    @property
    def running(self) -> bool:
        """Describes the GROUP, not the sum of its open streams. AudioEngine.play falls
        back to its own direct output when this is False, which would write into the
        cable behind a user who switched every target off."""
        return self._alive

    def target(self, key: str) -> Target | None:
        return next((t for t in self.targets if t.key == key), None)

    def apply(self, key: str, settings: dict) -> None:
        target = self.target(key)
        if target is not None:
            target.apply(settings)
            target._mic_wanted = self.mic_active
            target._mic_muted = self.mic_muted

    def apply_levels(self, offset_db: float, ducking_enabled: bool,
                     ducking_db: float) -> None:
        """"Sounds unter Stimme" und Ducking fuer alle Kabel; der Kopfhoerer bleibt neutral."""
        for target in self.targets:
            if target.key == MONITOR_KEY:
                target.sounds_offset = 1.0
                target.ducker = None
                continue
            target.sounds_offset = dynamics.db_to_gain(offset_db)
            if target.ducker is None:
                # Built and fully configured locally, published to target.ducker only
                # once complete: dynamics.Ducker's constructor always starts `enabled =
                # True`, and it has no `enabled` constructor parameter, so assigning
                # the half-built object straight to target.ducker would let the output
                # callback (another thread) see it briefly enabled even when ducking
                # is meant to be off.
                ducker = dynamics.Ducker(depth_db=ducking_db, samplerate=TARGET_SAMPLERATE)
                ducker.enabled = bool(ducking_enabled)
                target.ducker = ducker
            else:
                target.ducker.depth_db = ducking_db
                target.ducker.enabled = bool(ducking_enabled)

    # ---- sources ----

    def add_source(self, samples: np.ndarray, gain: float, only: str | None = None) -> GroupSource:
        """`only`: the key of the one target that gets this sound (a preview goes to
        the headphones alone); None = every target that takes sounds."""
        targets = [t for t in self.targets if only is None or t.key == only]
        sources = [s for s in (t.add_source(samples, gain) for t in targets)
                   if s is not None]
        handle = GroupSource(sources)
        with self._handle_lock:
            self._handles.append(handle)
        return handle

    def remove(self, handle: GroupSource) -> None:
        """Take this playback out of every target.

        Not zip(self.targets, handle.per_target()): a target with sounds switched off
        never got a source, so the lists differ in length and zip would silently pair
        a source with the WRONG target. Match by identity instead."""
        wanted = {id(s) for s in handle.per_target()}
        for target in self.targets:
            with target._lock:
                mine = [s for s in target._sources if id(s) in wanted]
            for source in mine:
                target.remove(source)
        for source in handle.per_target():
            source.finished.set()
        with self._handle_lock:
            if handle in self._handles:
                self._handles.remove(handle)

    def stop_all_sources(self) -> None:
        for target in self.targets:
            target.clear_sources()
        with self._handle_lock:
            handles, self._handles = self._handles, []
        for handle in handles:
            handle.finished.set()

    # ---- mic callback (PortAudio thread) ----

    def _input_callback(self, indata, frames, time_info, status) -> None:
        # Mono before the chain: half the work of filtering the duplicated stereo block.
        mono = np.asarray(indata, dtype=np.float32)[:, :1]
        processed = self.mic_chain.process(mono)
        self.voice.speaking = bool(self.mic_chain.speaking) and not self.mic_muted
        self._distribute_mic(to_stereo(processed))

    def _distribute_mic(self, block: np.ndarray) -> None:
        """Every target gets its OWN reference to the same read-only block."""
        if self.mic_muted:
            return
        for target in self.targets:
            target.push_mic(block)

    # ---- mic controls (compat with the dock's mic-mute button, predating the matrix) ----

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = bool(muted)
        for target in self.targets:
            target._mic_muted = self.mic_muted


def build(cfg: dict, resolved: dict, open_streams: bool = True) -> SinkGroup | None:
    """A started SinkGroup with one target per virtual cable plus the monitor.

    Never raises: a target that cannot open is marked and skipped. Returns None when
    not a single target exists, and the caller falls back to the plain engine.
    `open_streams=False` builds without touching any device (tests)."""
    from . import config as config_module

    targets: list[Target] = []
    for pair in resolved.get("virtual_mics") or []:
        settings = config_module.output_settings(
            cfg, pair["key"],
            is_voicemeeter=config_module.is_voicemeeter_key(pair["key"]))
        targets.append(Target(pair["key"], pair["label"], pair["out_index"], settings))

    monitor_index = resolved.get("monitor")
    if monitor_index is not None:
        settings = config_module.output_settings(cfg, config_module.MONITOR_KEY,
                                                 is_monitor=True)
        targets.append(Target(config_module.MONITOR_KEY, MONITOR_LABEL,
                              monitor_index, settings))

    if not targets:
        return None

    group = SinkGroup(targets, resolved.get("mic"))
    from . import levels
    group.apply_levels(levels.sounds_offset_db(cfg), *levels.ducking(cfg))
    if not open_streams:
        group._alive = True
        return group
    try:
        group.start()
    except Exception:
        log.exception("the sink group failed to start")
        group.stop()
        return None
    log.info("sink group on %s, mic=%s active=%s",
             [(t.key, t.mic, t.sounds) for t in group.targets],
             group.mic_device, group.mic_active)
    return group
