"""Pegel-Werkzeuge fuer den Mischer: dB-Umrechnung, Limiter, Ducking, Leveler.

Alles arbeitet blockweise (ein Aufruf pro 10-ms-Callback) und rein mit numpy, ohne
Python-Schleife ueber einzelne Samples. Aendert sich ein Gain, bekommt der Block eine
lineare Rampe vom alten zum neuen Wert - ein Sprung an der Blockgrenze wuerde knacken.

Threading: jede Instanz gehoert genau einem PortAudio-Callback. Andere Threads setzen
hoechstens einfache Attribute (enabled, depth_db); das ist in CPython atomar.
"""

from __future__ import annotations

import math

import numpy as np

SILENCE_DB = -120.0


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


def gain_to_db(gain: float) -> float:
    return 20.0 * math.log10(gain) if gain > 0.0 else SILENCE_DB


def block_rms_db(block: np.ndarray) -> float:
    if len(block) == 0:
        return SILENCE_DB
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    return max(gain_to_db(rms), SILENCE_DB)


def _ramp(start: float, end: float, frames: int):
    """Scalar when nothing changes (cheap broadcast), else a (frames, 1) ramp."""
    if start == end:
        return np.float32(end)
    return np.linspace(start, end, frames, dtype=np.float32).reshape(-1, 1)


def _smoothing(time_ms: float, frames: int, samplerate: int) -> float:
    """Share of the remaining distance one block of `frames` covers (one-pole)."""
    return 1.0 - math.exp(-frames / (samplerate * time_ms / 1000.0))


class Limiter:
    """Peak limiter without look-ahead latency: the gain is computed from the block
    itself before it is applied, so a block never leaves louder than the ceiling.
    Attack is immediate for the whole block, release is smooth."""

    def __init__(self, ceiling_db: float = -1.0, release_ms: float = 200.0,
                 samplerate: int = 48000):
        self.ceiling = db_to_gain(ceiling_db)
        self.release_ms = release_ms
        self.samplerate = samplerate
        self.gain = 1.0

    def reset(self) -> None:
        self.gain = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        frames = len(block)
        if frames == 0:
            return block.copy()
        peak = float(np.max(np.abs(block)))
        needed = min(1.0, self.ceiling / peak) if peak > 0.0 else 1.0
        if needed < self.gain:
            # No ramp from above: a ramp would let the start of this block overshoot.
            start = new = needed
        else:
            start = self.gain
            new = self.gain + (needed - self.gain) * _smoothing(
                self.release_ms, frames, self.samplerate)
        self.gain = new
        return block * _ramp(start, new, frames)


class Ducker:
    """Gain for the sounds while the user speaks: down to depth_db in attack_ms,
    back to unity in release_ms."""

    def __init__(self, depth_db: float = -6.0, attack_ms: float = 50.0,
                 release_ms: float = 400.0, samplerate: int = 48000):
        self.depth_db = depth_db
        self.attack_ms = attack_ms
        self.release_ms = release_ms
        self.samplerate = samplerate
        self.enabled = True
        self.gain = 1.0

    def next_ramp(self, speaking: bool, frames: int):
        target = db_to_gain(self.depth_db) if (self.enabled and speaking) else 1.0
        time_ms = self.attack_ms if target < self.gain else self.release_ms
        new = self.gain + (target - self.gain) * _smoothing(time_ms, frames, self.samplerate)
        if abs(new - target) < 1e-4:
            new = target
        ramp = _ramp(self.gain, new, frames)
        self.gain = new
        return ramp


# Auto-Pegel for the music bus (Jan 2026-09-30): the process loopback hears Spotify after
# its own volume slider, so turning Spotify down for his ears made it quiet on the cables
# too. The bus now rides its own level back to "Spotify at 100 %" (about -14 LUFS, the
# levels.SPOTIFY_REFERENCE_LUFS the Klangbild compensation assumes).
MUSIC_REFERENCE_DB = -14.0
MUSIC_MAX_BOOST_DB = 24.0
MUSIC_MAX_CUT_DB = -6.0


class MusicLeveler:
    """Slow gain rider for music: measures the loudness over seconds (an exponential
    mean of the power, so single drum hits or a quiet bridge do not pump), pulls it
    toward target_db and holds the gain through pauses. Peaks stay with the limiter."""

    def __init__(self, target_db: float = MUSIC_REFERENCE_DB, max_boost_db: float = MUSIC_MAX_BOOST_DB,
                 max_cut_db: float = MUSIC_MAX_CUT_DB, gate_db: float = -60.0,
                 window_s: float = 3.0, rise_db_per_s: float = 3.0, fall_db_per_s: float = 20.0,
                 samplerate: int = 48000):
        self.target_db = target_db
        self.max_boost_db = max_boost_db
        self.max_cut_db = max_cut_db
        self.gate_db = gate_db
        self.window_s = window_s
        self.rise_db_per_s = rise_db_per_s
        self.fall_db_per_s = fall_db_per_s
        self.samplerate = samplerate
        self.gain_db = 0.0
        self._power: float | None = None  # running mean of the input power
        self._applied = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        frames = len(block)
        if frames == 0:
            return block.copy()
        seconds = frames / self.samplerate
        level = block_rms_db(block)
        if level > self.gate_db:  # a pause (or Spotify stopped) holds everything
            power = db_to_gain(level) ** 2
            if self._power is None:
                self._power = power
            else:
                self._power += (power - self._power) * min(1.0, seconds / self.window_s)
            measured = gain_to_db(self._power ** 0.5)
            desired = min(max(self.target_db - measured, self.max_cut_db), self.max_boost_db)
            if desired > self.gain_db:
                self.gain_db = min(desired, self.gain_db + self.rise_db_per_s * seconds)
            else:
                self.gain_db = max(desired, self.gain_db - self.fall_db_per_s * seconds)
        new = db_to_gain(self.gain_db)
        out = block * _ramp(self._applied, new, frames)
        self._applied = new
        return out


class Leveler:
    """Slow gain rider for speech (a gentle compressor): pulls speech toward
    target_db RMS, holds its gain through pauses and lowers pauses by expander_db so
    the boost does not lift the room noise. `speaking` feeds the ducking."""

    def __init__(self, target_db: float = -20.0, max_boost_db: float = 18.0,
                 max_cut_db: float = -12.0, speech_threshold_db: float = -50.0,
                 rise_db_per_s: float = 6.0, fall_db_per_s: float = 30.0,
                 hangover_ms: float = 300.0, expander_db: float = -10.0,
                 samplerate: int = 48000):
        self.target_db = target_db
        self.max_boost_db = max_boost_db
        self.max_cut_db = max_cut_db
        self.speech_threshold_db = speech_threshold_db
        self.rise_db_per_s = rise_db_per_s
        self.fall_db_per_s = fall_db_per_s
        self.hangover_frames = int(samplerate * hangover_ms / 1000.0)
        self.expander_db = expander_db
        self.samplerate = samplerate
        self.gain_db = 0.0
        self.speaking = False
        self._hang = 0
        self._applied = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        frames = len(block)
        if frames == 0:
            return block.copy()
        level = block_rms_db(block)
        if level > self.speech_threshold_db:
            self.speaking = True
            self._hang = self.hangover_frames
            desired = min(max(self.target_db - level, self.max_cut_db), self.max_boost_db)
            seconds = frames / self.samplerate
            if desired > self.gain_db:
                self.gain_db = min(desired, self.gain_db + self.rise_db_per_s * seconds)
            else:
                self.gain_db = max(desired, self.gain_db - self.fall_db_per_s * seconds)
        else:
            self._hang -= frames
            if self._hang <= 0:
                self.speaking = False
        applied_db = self.gain_db + (0.0 if self.speaking else self.expander_db)
        new = db_to_gain(applied_db)
        out = block * _ramp(self._applied, new, frames)
        self._applied = new
        return out
