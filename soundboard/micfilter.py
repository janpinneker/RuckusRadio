"""Aufbereitung NUR fuer den Mikrofonzweig: Hochpass, Leveler, Limiter.

Soundboard-Samples laufen nie hier durch - sie werden in sinkgroup.Target getrennt
summiert. Rauschen und Tastatur entfernt diese Kette nicht; dafuer wird NVIDIA
Broadcast als Mikrofon-Eingang empfohlen (siehe README). Die Kette sorgt dafuer, dass
die Stimme gleichmaessig auf VOICE_TARGET_DB liegt, damit "Sounds unter Stimme" einen
festen Bezugspunkt hat.
"""

from __future__ import annotations

import math

import numpy as np

from .dynamics import Leveler, Limiter

VOICE_TARGET_DB = -20.0  # speech RMS in dBFS; levels.VOICE_TARGET_LUFS matches it
VOICE_CEILING_DB = -1.0


class MicChain:
    """Mono in, mono out. Stateful: one instance per microphone stream."""

    def __init__(self, samplerate: int, highpass_hz: float = 80.0,
                 voice_target_db: float = VOICE_TARGET_DB):
        self._alpha = math.exp(-2.0 * math.pi * highpass_hz / samplerate)
        self._prev_x = 0.0
        self._prev_y = 0.0
        self.leveler = Leveler(target_db=voice_target_db, samplerate=samplerate)
        self.limiter = Limiter(ceiling_db=VOICE_CEILING_DB, release_ms=100.0,
                               samplerate=samplerate)

    @property
    def speaking(self) -> bool:
        return self.leveler.speaking

    def _highpass(self, x: np.ndarray) -> np.ndarray:
        """First-order high-pass y[n] = a*y[n-1] + x[n] - x[n-1], vectorised.

        Closed form: y[n] = a^n * (a*y[-1] + sum_k<=n (x[k]-x[k-1]) * a^-k). For a
        480-frame block a^-n stays below ~200, well inside float64 precision."""
        powers = self._alpha ** np.arange(len(x), dtype=np.float64)
        diffs = np.diff(x.astype(np.float64), prepend=self._prev_x)
        y = powers * (self._alpha * self._prev_y + np.cumsum(diffs / powers))
        self._prev_x = float(x[-1])
        self._prev_y = float(y[-1])
        return y.astype(np.float32)

    def process(self, mono: np.ndarray) -> np.ndarray:
        x = np.asarray(mono, dtype=np.float32).reshape(-1)
        if len(x) == 0:
            return np.zeros((0, 1), dtype=np.float32)
        filtered = self._highpass(x).reshape(-1, 1)
        return self.limiter.process(self.leveler.process(filtered))
