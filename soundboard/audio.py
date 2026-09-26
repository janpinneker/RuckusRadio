"""Decode MP3/MP4 to PCM and play simultaneously to two output devices at
independent volumes (VoiceMeeter Input + local monitor)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd
from pydub import AudioSegment

from soundboard.config import MONITOR_KEY
from soundboard.paths import configure_ffmpeg

# Must run before any AudioSegment decode/export below: points pydub at the
# bundled ffmpeg/ffprobe when frozen, else leaves system PATH resolution alone.
FFMPEG_PATH = configure_ffmpeg()

TARGET_SAMPLERATE = 48000
TARGET_CHANNELS = 2


@dataclass
class DecodedSound:
    samples: np.ndarray  # float32, shape (n_frames, channels), range -1..1
    samplerate: int


def extract_audio(src_path: str | Path, dest_path: str | Path, fmt: str = "mp3") -> Path:
    """Extract/transcode the audio track of any file ffmpeg can read (incl.
    MP4) into a standalone audio file. Video streams are never decoded."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    segment = AudioSegment.from_file(str(src_path))
    segment.export(str(dest_path), format=fmt)
    return dest_path


def decode_audio(path: str | Path) -> DecodedSound:
    """Decode an MP3 or MP4 file to float32 PCM via ffmpeg/pydub.

    For MP4 input, pydub/ffmpeg extracts only the audio stream — no video
    is ever decoded or rendered.
    """
    segment = AudioSegment.from_file(str(path))
    segment = segment.set_frame_rate(TARGET_SAMPLERATE).set_channels(TARGET_CHANNELS)

    raw = np.array(segment.get_array_of_samples())
    max_value = float(1 << (8 * segment.sample_width - 1))
    samples = raw.astype(np.float32) / max_value
    samples = samples.reshape(-1, TARGET_CHANNELS)
    return DecodedSound(samples=samples, samplerate=TARGET_SAMPLERATE)


class _StreamPlayback:
    """One OutputStream reading from a shared, read-only buffer at a fixed gain.
    The callback runs on a PortAudio thread and only touches its own position."""

    def __init__(self, samples: np.ndarray, samplerate: int, device: int, gain: float):
        self._samples = samples
        self._pos = 0
        self._gain = gain
        self.finished = threading.Event()
        self.stream = sd.OutputStream(
            samplerate=samplerate,
            device=device,
            channels=samples.shape[1],
            dtype="float32",
            callback=self._callback,
            finished_callback=self.finished.set,
        )

    def _callback(self, outdata, frames, time_info, status):
        remaining = len(self._samples) - self._pos
        n = max(0, min(frames, remaining))
        if n > 0:
            chunk = self._samples[self._pos : self._pos + n] * self._gain
            np.clip(chunk, -1.0, 1.0, out=outdata[:n])  # volume >100% must not wrap/distort
            self._pos += n
        if n < frames:
            outdata[n:] = 0
        if remaining <= frames:
            raise sd.CallbackStop()

    def start(self):
        self.stream.start()

    def abort(self):
        try:
            self.stream.abort(ignore_errors=True)
        except Exception:
            pass

    def close(self):
        try:
            self.stream.close(ignore_errors=True)
        except Exception:
            pass


class _SinkPlayback:
    """Same start/abort/close/finished protocol as _StreamPlayback, but instead of an
    own OutputStream it feeds a sink (a `sinkgroup.SinkGroup`) that already owns one.
    Used whenever mic and sounds have to leave through a single mixed stream."""

    def __init__(self, sink, samples: np.ndarray, gain: float, only: str | None = None):
        self._sink = sink
        self._source = (sink.add_source(samples, gain, only=only) if only is not None
                        else sink.add_source(samples, gain))
        self.finished = self._source.finished

    def start(self):
        pass  # the sink's stream is already running

    def abort(self):
        self._sink.remove(self._source)

    def close(self):
        self._sink.remove(self._source)


class ActivePlayback:
    """One overlapping playback of `sound_id`; owns one stream per output device."""

    def __init__(self, sound_id: str, streams: list[_StreamPlayback]):
        self.sound_id = sound_id
        self._streams = streams

    def start(self):
        for s in self._streams:
            s.start()

    def stop(self):
        for s in self._streams:
            s.abort()

    def close(self):
        for s in self._streams:
            s.close()

    def is_finished(self) -> bool:
        return all(s.finished.is_set() for s in self._streams)


class AudioEngine:
    """play/stop_all/playing_ids are called from the Tk thread; preload may run
    on worker threads. `_lock` guards `_cache` and `_active`; stream callbacks
    never take it."""

    def __init__(
        self,
        voicemeeter_device: int | None,
        monitor_device: int | None,
        monitor_volume: float = 0.5,
        sink=None,
    ):
        self.voicemeeter_device = voicemeeter_device
        self.monitor_device = monitor_device
        self.monitor_volume = monitor_volume
        self.sink = sink  # a sinkgroup.SinkGroup when one is running, else None
        self._active: list[ActivePlayback] = []
        self._lock = threading.Lock()
        self._cache: dict[str, DecodedSound] = {}

    def preload(self, sound_id: str, path: str | Path) -> None:
        decoded = decode_audio(path)  # slow part, outside the lock
        with self._lock:
            self._cache[sound_id] = decoded

    def is_loaded(self, sound_id: str) -> bool:
        with self._lock:
            return sound_id in self._cache

    def forget(self, sound_id: str) -> None:
        with self._lock:
            self._cache.pop(sound_id, None)

    def _targets_for(self, volume: float, monitor_only: bool = False) -> list[tuple[int, float]]:
        """Devices that need their OWN stream. Empty while a sink is running: the sink
        owns every output then, headphones included, and a second stream would play
        each sound twice and ignore the user's per-target switches."""
        if self.sink is not None and self.sink.running:
            return []
        targets: list[tuple[int, float]] = []
        if self.voicemeeter_device is not None and not monitor_only:
            targets.append((self.voicemeeter_device, volume))
        if self.monitor_device is not None:
            targets.append((self.monitor_device, volume * self.monitor_volume))
        return targets

    def play(self, sound_id: str, volume: float = 1.0, monitor_only: bool = False) -> ActivePlayback:
        """Raises KeyError if not preloaded, sd.PortAudioError if a device fails
        (any stream already opened for this play is closed first). `monitor_only`:
        a preview - the headphones hear it, the voice chat does not."""
        with self._lock:
            decoded = self._cache[sound_id]

        sink = self.sink if self.sink is not None and self.sink.running else None
        targets = self._targets_for(volume, monitor_only)

        streams: list = []
        playback = ActivePlayback(sound_id, streams)
        try:
            if sink is not None:
                streams.append(_SinkPlayback(sink, decoded.samples, volume,
                                             MONITOR_KEY if monitor_only else None))
            for device, gain in targets:
                streams.append(_StreamPlayback(decoded.samples, decoded.samplerate, device, gain))
            playback.start()
        except Exception:
            playback.stop()
            playback.close()
            raise

        with self._lock:
            self._active.append(playback)
        self._reap()
        return playback

    def _reap(self) -> None:
        done: list[ActivePlayback] = []
        running: list[ActivePlayback] = []
        with self._lock:
            for p in self._active:
                (done if p.is_finished() else running).append(p)
            self._active = running
        for p in done:
            p.close()

    def playing_ids(self) -> set[str]:
        self._reap()
        with self._lock:
            return {p.sound_id for p in self._active}

    def is_playing(self, sound_id: str) -> bool:
        return sound_id in self.playing_ids()

    def stop_all(self) -> None:
        with self._lock:
            active, self._active = self._active, []
        for p in active:
            p.stop()
            p.close()

    def stop(self, sound_id: str) -> None:
        """Stop and close every active playback of `sound_id` (thread-safe, like stop_all)."""
        with self._lock:
            matching = [p for p in self._active if p.sound_id == sound_id]
            self._active = [p for p in self._active if p.sound_id != sound_id]
        for p in matching:
            p.stop()
            p.close()
