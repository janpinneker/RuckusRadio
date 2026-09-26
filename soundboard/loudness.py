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
from dataclasses import dataclass
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

SILENT_LUFS = -70.0  # ebur128 reports this for silence and the warm-up frames
NO_PEAK_DB = -120.0

_INTEGRATED = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS", re.MULTILINE)
_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", re.MULTILINE)
_SHORT = re.compile(r"\sS:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Loudness:
    integrated: float
    max_short: float
    peak: float


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
    return Loudness(integrated=value, max_short=max(shorts) if shorts else value, peak=peak)


def _measure(path, ffmpeg: str | None = None,
             timeout: float = 120.0) -> tuple[Loudness | None, bool]:
    """Runs ffmpeg and reports both the result and whether a bad-file failure was
    actually established. `failed` is True ONLY when ffmpeg ran to completion on an
    existing file and either exited nonzero or its output could not be parsed - that
    is the file's own fault. No ffmpeg, a missing file, a timeout, or an OSError/
    SubprocessError starting the process is transient or environmental: `failed` stays
    False so the caller can retry later instead of pinning a permanent failure."""
    exe = ffmpeg or paths.ffmpeg_path()
    if exe is None or not Path(path).exists():
        return None, False
    command = [exe, "-hide_banner", "-nostats", "-i", str(path),
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


def analyze(path, ffmpeg: str | None = None, timeout: float = 120.0) -> Loudness | None:
    result, _failed = _measure(path, ffmpeg, timeout)
    return result


def measure_dict(path) -> dict | None:
    """What config stores per sound: the measurement, {"failed": True} when ffmpeg
    actually rejected the file (a real, permanent problem), or None when the file
    could not be measured right now (no ffmpeg, missing file, timeout, OSError/
    SubprocessError) - callers must not store None, so the sound is measured again on
    the next backfill instead of being pinned at a fallback loudness forever."""
    result, failed = _measure(path)
    if result is not None:
        return {"integrated": round(result.integrated, 2),
                "max_short": round(result.max_short, 2),
                "peak": round(result.peak, 2)}
    return {"failed": True} if failed else None
