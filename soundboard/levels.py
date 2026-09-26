"""Pegel-Entscheidungen: wie laut ein Sound gegenueber der Stimme spielt, und wie das
fuer Menschen ohne Tontechnik-Erfahrung beschriftet wird. Rein, ohne Geraet und ohne Tk.

Bezugspunkt ist die Stimme: MicChain bringt sie auf VOICE_TARGET_LUFS. Jeder Sound wird
auf dieselbe Lautheit normalisiert, und die Kabel ziehen "Sounds unter Stimme" davon ab.
"""

from __future__ import annotations

import math
from typing import Any

from .dynamics import db_to_gain

VOICE_TARGET_LUFS = -20.0  # same value as micfilter.VOICE_TARGET_DB
SHORT_TERM_HEADROOM_LU = 3.0  # the loudest 3-s passage may sit this far above the voice
MAX_BOOST_DB = 12.0
MAX_CUT_DB = -30.0
UNMEASURED_GAIN_DB = -18.0  # typical pop song (-8 LUFS) down to -26: safe until measured

DEFAULT_OFFSET_DB = -6.0
OFFSET_RANGE_DB = (-20.0, 0.0)
DEFAULT_DUCKING_DB = -6.0
DUCKING_RANGE_DB = (-20.0, 0.0)
ROW_RANGE_DB = (-40.0, 6.0)  # per-target mic/sounds sliders on Einstellungen

_PHRASES = (
    (4.5, "deutlich lauter"),
    (1.5, "etwas lauter"),
    (-1.5, "unverändert"),
    (-4.5, "etwas leiser"),
    (-8.0, "deutlich leiser"),
    (-14.0, "etwa halb so laut"),
    (-24.0, "leise im Hintergrund"),
)
_OFFSET_SUFFIX = {
    "etwas leiser": "als deine Stimme",
    "deutlich leiser": "als deine Stimme",
    "etwa halb so laut": "wie deine Stimme",
}


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalization_db(loudness: dict | None) -> float:
    """dB that bring a measured sound to the voice's loudness. The average decides,
    unless the loudest passage would then stick out more than 3 LU - that catches
    songs that start at full level."""
    if not loudness or loudness.get("failed") or "integrated" not in loudness:
        return UNMEASURED_GAIN_DB
    integrated = float(loudness["integrated"])
    max_short = float(loudness.get("max_short", integrated))
    gain = min(VOICE_TARGET_LUFS - integrated,
               VOICE_TARGET_LUFS + SHORT_TERM_HEADROOM_LU - max_short)
    return _clamp(gain, MAX_CUT_DB, MAX_BOOST_DB)


def play_gain(sound: dict, volume: float) -> float:
    """Linear gain for AudioEngine.play: the user's per-sound volume on top of the
    automatic normalization."""
    return volume * db_to_gain(normalization_db(sound.get("loudness")))


def sounds_offset_db(cfg: dict) -> float:
    return _clamp(_float(cfg.get("sounds_offset_db"), DEFAULT_OFFSET_DB), *OFFSET_RANGE_DB)


def ducking(cfg: dict) -> tuple[bool, float]:
    enabled = bool(cfg.get("ducking_enabled", True))
    depth = _clamp(_float(cfg.get("ducking_db"), DEFAULT_DUCKING_DB), *DUCKING_RANGE_DB)
    return enabled, depth


def _step(db: float) -> float:
    return round(db * 2.0) / 2.0


def _phrase(step: float) -> str:
    for floor, text in _PHRASES:
        if step >= floor:
            return text
    return "kaum hörbar"


def format_db(db: float) -> str:
    """Half-dB steps, German decimal comma: "-6 dB", "+3,5 dB", "0 dB"."""
    step = _step(db)
    if step == 0:
        return "0 dB"
    text = f"{abs(step):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{'+' if step > 0 else '-'}{text} dB"


def describe_db(db: float) -> str:
    """Number plus an everyday comparison, e.g. "-10 dB · etwa halb so laut"."""
    step = _step(db)
    return f"{format_db(step)} · {_phrase(step)}"


def describe_offset(db: float) -> str:
    step = _step(db)
    if step > -1.5:
        return f"{format_db(step)} · gleich laut wie deine Stimme"
    phrase = _phrase(step)
    suffix = _OFFSET_SUFFIX.get(phrase, "unter deiner Stimme")
    return f"{format_db(step)} · {phrase} {suffix}"


def loudness_badge(sound: dict) -> str:
    """Short text under a tile: what the automatic correction did."""
    measured = sound.get("loudness")
    if measured is None:
        return "misst …"
    if measured.get("failed"):
        return "nicht messbar"
    db = normalization_db(measured)
    return "" if abs(db) < 1.0 else f"auto {format_db(db)}"


def loudness_summary(sound: dict) -> str:
    """One sentence for the volume dialog."""
    measured = sound.get("loudness")
    if not measured or measured.get("failed") or "integrated" not in measured:
        return (f"Noch nicht gemessen – spielt vorsichtshalber mit "
                f"{format_db(UNMEASURED_GAIN_DB)}.")
    original = f"{float(measured['integrated']):.1f}".replace(".", ",")
    return (f"Automatisch angeglichen: {format_db(normalization_db(measured))} "
            f"(gemessen {original} LUFS). Der Regler kommt obendrauf.")
