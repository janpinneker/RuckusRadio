"""Bausteine des Mischers: eine Quelle, Kanalumwandlung, das Summieren.

Wer die Bloecke wohin schreibt, steht in `sinkgroup.py`. Hier liegt nur, was davon
geraeteunabhaengig ist - und genau deshalb hier: dieser Kern laeuft nachweislich sauber
und wird beim Umbau auf mehrere Ziele nicht angefasst.
"""

from __future__ import annotations

import threading

import numpy as np

TARGET_SAMPLERATE = 48000
TARGET_CHANNELS = 2
BLOCKSIZE = 480  # 10 ms at 48 kHz
MIC_QUEUE_BLOCKS = 8  # ~80 ms of slack before drift forces a drop


class MixSource:
    """One playing sound inside the mix. `pull` runs on the PortAudio thread and only
    touches its own position, so the engine may read `finished` from any thread."""

    __slots__ = ("_samples", "_pos", "gain", "finished", "_stopped")

    def __init__(self, samples: np.ndarray, gain: float):
        self._samples = samples
        self._pos = 0
        self.gain = gain
        self.finished = threading.Event()
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def pull(self, frames: int) -> np.ndarray | None:
        """Next `frames` frames scaled by gain, zero-padded at the end. None once done."""
        if self._stopped:
            self.finished.set()
            return None
        remaining = len(self._samples) - self._pos
        if remaining <= 0:
            self.finished.set()
            return None
        n = min(frames, remaining)
        chunk = self._samples[self._pos:self._pos + n] * self.gain
        self._pos += n
        if n < frames:
            chunk = np.vstack([chunk, np.zeros((frames - n, chunk.shape[1]), dtype=np.float32)])
            self.finished.set()
        return chunk


def to_stereo(block: np.ndarray) -> np.ndarray:
    """Any channel count -> TARGET_CHANNELS. Mono is duplicated, extra channels dropped."""
    if block.ndim == 1:
        block = block.reshape(-1, 1)
    if block.shape[1] == TARGET_CHANNELS:
        return block
    if block.shape[1] == 1:
        return np.repeat(block, TARGET_CHANNELS, axis=1)
    return block[:, :TARGET_CHANNELS]


def mix_blocks(blocks: list[np.ndarray], frames: int) -> np.ndarray:
    """Sum blocks and clip to -1..1 so 150 % volume saturates instead of wrapping
    (same rule as audio._StreamPlayback)."""
    out = np.zeros((frames, TARGET_CHANNELS), dtype=np.float32)
    for block in blocks:
        n = min(frames, len(block))
        if n:
            out[:n] += block[:n]
    np.clip(out, -1.0, 1.0, out=out)
    return out

