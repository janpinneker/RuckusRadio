"""Zuschnitt eines Sounds (Spec C8): Start und Ende in Sekunden, nicht-destruktiv.

Die Datei in sounds/ bleibt unberuehrt; `trim` steht am Sound in der Config und wird
beim Dekodieren (audio.decode_audio), beim Messen der Lautheit (loudness) und beim
Export einer neuen Datei angewandt. Reine Funktionen: kein Kern, kein ffmpeg.
"""

from __future__ import annotations

import math

import numpy as np

MIN_LENGTH_S = 0.1
PEAK_BUCKETS = 1000
DURATION_SLACK_S = 0.001  # the page rounds the length to 3 decimals
TRIM_INVALID = ("Ungültiger Zuschnitt: Der Start muss vor dem Ende liegen, "
                "beide innerhalb des Sounds.")
TRIM_TOO_SHORT = "Zuschnitt zu kurz: mindestens 0,1 Sekunden."


def _seconds(value) -> float | None:
    """A finite JSON number rounded to milliseconds, else None (bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return round(value, 3) if math.isfinite(value) else None


def check_trim(start, end, duration: float) -> str | None:
    """None when start/end are a valid cut of a `duration`-second sound, else the German
    hint. Rule (spec §3): 0 <= start < end <= duration, end - start >= 0.1 s."""
    s, e = _seconds(start), _seconds(end)
    if s is None or e is None or s < 0 or s >= e or e > duration + DURATION_SLACK_S:
        return TRIM_INVALID
    if e - s < MIN_LENGTH_S - 1e-9:
        return TRIM_TOO_SHORT
    return None


def clean_trim(raw) -> dict | None:
    """A stored or imported trim in canonical form, or None when absent or malformed
    (hand-edited config, foreign pack). Does not know the length: apply_trim clamps."""
    if not isinstance(raw, dict):
        return None
    s, e = _seconds(raw.get("start")), _seconds(raw.get("end"))
    if s is None or e is None or s < 0 or e - s < MIN_LENGTH_S - 1e-9:
        return None
    return {"start": s, "end": e}


def is_full_length(start: float, end: float, duration: float) -> bool:
    """A "cut" that keeps the whole sound is no cut: it is stored as no trim at all."""
    return start <= DURATION_SLACK_S and end >= duration - DURATION_SLACK_S


def clamp_trim_seconds(trim, duration_s: float) -> tuple[float, float] | None:
    """The clamped (start, end) in seconds for a real cut, or None when there is no cut
    to apply: absent/malformed trim, or a start at or beyond the (clamped) end - "better
    the whole sound than silence" (an end beyond the file is clamped; a start beyond it
    means no cut at all). Shared by `apply_trim` (playback) and `audio.export_trimmed`
    (K5: the export must not disagree with what actually plays)."""
    cut = clean_trim(trim)
    if cut is None:
        return None
    start = min(cut["start"], duration_s)
    end = min(cut["end"], duration_s)
    if end <= start:
        return None
    return start, end


def apply_trim(samples: np.ndarray, samplerate: int, trim) -> np.ndarray:
    """The frames between trim.start and trim.end as a copy (so the uncut frames can be
    freed). No or a malformed trim: `samples` unchanged. An end beyond the file is
    clamped; a start beyond it keeps everything (better the whole sound than silence)."""
    cut = clamp_trim_seconds(trim, len(samples) / samplerate if samplerate else 0.0)
    if cut is None:
        return samples
    start_s, end_s = cut
    first = min(len(samples), int(round(start_s * samplerate)))
    last = min(len(samples), int(round(end_s * samplerate)))
    if last <= first:
        return samples
    return samples[first:last].copy()


def peaks(samples: np.ndarray, buckets: int = PEAK_BUCKETS) -> tuple[float, ...]:
    """Min/max pairs of the mono mix, flattened (min0, max0, min1, max1, ...), at most
    `buckets` pairs, rounded to 3 decimals (~12 KB JSON for 1000 pairs)."""
    if samples.size == 0:
        return ()
    if samples.ndim == 2:
        mono = samples.mean(axis=1, dtype=np.float32)  # no float64 copy of the whole buffer
    else:
        mono = np.asarray(samples, dtype=np.float32)
    n = min(buckets, len(mono))
    starts = (np.arange(n) * len(mono)) // n
    out = np.empty(2 * n, dtype=np.float64)
    # only the reduced (<= buckets-sized) result is widened to float64, not the buffer
    out[0::2] = np.round(np.minimum.reduceat(mono, starts).astype(np.float64), 3)
    out[1::2] = np.round(np.maximum.reduceat(mono, starts).astype(np.float64), 3)
    return tuple(float(v) for v in out)
