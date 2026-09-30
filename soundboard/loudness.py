"""Lautheit eines Sounds messen (EBU R128) - einmal beim Import, nie beim Abspielen.

ffmpeg bringt den Messfilter `ebur128` mit und liegt ohnehin im Build. Gemessen werden
die integrierte Lautheit (Durchschnitt), die lauteste Kurzzeit-Lautheit (3-s-Fenster:
faengt Songs, die sofort voll einsetzen) und der True Peak. Laeuft auf Worker-Threads;
fasst weder Tk noch die Config an.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, trimming

log = logging.getLogger(__name__)

SILENT_LUFS = -70.0  # ebur128 reports this for silence and the warm-up frames
NO_PEAK_DB = -120.0

_INTEGRATED = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS", re.MULTILINE)
_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", re.MULTILINE)
_SHORT = re.compile(r"\sS:\s*(-?\d+(?:\.\d+)?)")
_TIME = re.compile(r"\st:\s*(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Loudness:
    integrated: float
    max_short: float
    peak: float
    # Klangbild K2: the length, from the same run. Not part of equality: two
    # measurements of the same loudness stay equal whatever the frame count says.
    duration: float | None = field(default=None, compare=False)


def parse_duration(text: str) -> float | None:
    """Length in seconds: the last frame timestamp `t:` of the framelog (100-ms frames,
    exact to about 0.1 s). None without frame lines."""
    times = _TIME.findall(text)
    return float(times[-1]) if times else None


def parse_ebur128(text: str) -> Loudness | None:
    """Summary lines start the line ("    I: ..."); frame lines start with "[Parsed_",
    so the anchored patterns only ever see the summary."""
    integrated = _INTEGRATED.findall(text)
    if not integrated:
        return None
    value = SILENT_LUFS if integrated[-1] == "-inf" else float(integrated[-1])
    peaks = _PEAK.findall(text)
    peak = float(peaks[-1]) if peaks and peaks[-1] != "-inf" else NO_PEAK_DB
    shorts = [float(v) for v in _SHORT.findall(text) if float(v) > SILENT_LUFS]
    return Loudness(integrated=value, max_short=max(shorts) if shorts else value, peak=peak,
                    duration=parse_duration(text))


def _measure(path, ffmpeg: str | None = None, timeout: float = 120.0,
             trim: dict | None = None) -> tuple[Loudness | None, bool]:
    """Runs ffmpeg and reports both the result and whether a bad-file failure was
    actually established. `failed` is True ONLY when ffmpeg ran to completion on an
    existing file and either exited nonzero or its output could not be parsed - that
    is the file's own fault. No ffmpeg, a missing file, a timeout, or an OSError/
    SubprocessError starting the process is transient or environmental: `failed` stays
    False so the caller can retry later instead of pinning a permanent failure.
    `trim` (C8) measures only the cut: input seeking with -ss/-t, so normalization
    fits the part that actually plays."""
    exe = ffmpeg or paths.ffmpeg_path()
    if exe is None or not Path(path).exists():
        return None, False
    cut = trimming.clean_trim(trim)
    window = ([] if cut is None else
              ["-ss", f"{cut['start']:.3f}", "-t", f"{cut['end'] - cut['start']:.3f}"])
    command = [exe, "-hide_banner", "-nostats", *window, "-i", str(path),
               "-af", "ebur128=peak=true:framelog=info", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            # The app is windowed: without this every measurement flashes a console.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # subprocess.TimeoutExpired is a SubprocessError: a timeout is transient too.
        log.warning("loudness measurement failed for %s", path, exc_info=True)
        return None, False
    if proc.returncode != 0:
        log.warning("ffmpeg ebur128 exited with %s for %s", proc.returncode, path)
        return None, True
    result = parse_ebur128(proc.stderr)
    return result, result is None


def analyze(path, ffmpeg: str | None = None, timeout: float = 120.0,
            trim: dict | None = None) -> Loudness | None:
    result, _failed = _measure(path, ffmpeg, timeout, trim)
    return result


def measure_dict(path, trim: dict | None = None) -> dict | None:
    """What config stores per sound: the measurement, {"failed": True} when ffmpeg
    actually rejected the file (a real, permanent problem), or None when the file
    could not be measured right now (no ffmpeg, missing file, timeout, OSError/
    SubprocessError) - callers must not store None, so the sound is measured again on
    the next backfill instead of being pinned at a fallback loudness forever. Also
    carries "duration" in seconds when the framelog had frames (Klangbild K2) - the
    library moves it onto the sound. With a `trim` (C8) "duration" is left out: that
    call only re-measures loudness for the cut that now plays, and the cut's own
    length (end - start) is already known from the trim itself - it must not
    overwrite the sound's stored (full-file) duration."""
    result, failed = _measure(path, trim=trim)
    if result is not None:
        out = {"integrated": round(result.integrated, 2),
               "max_short": round(result.max_short, 2),
               "peak": round(result.peak, 2)}
        # F2: whether "duration" belongs in the result depends on whether the trim was
        # actually usable (trimming.clean_trim(trim) is None), not on `trim is None` -
        # a broken stored trim (hand-edited config) makes clean_trim() fall back to no
        # window at all, so the whole file gets measured and its length belongs here
        # too, exactly as if no trim had been passed.
        if result.duration is not None and trimming.clean_trim(trim) is None:
            out["duration"] = round(result.duration, 2)
        return out
    return {"failed": True} if failed else None
