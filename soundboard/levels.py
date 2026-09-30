"""Pegel-Entscheidungen: wie laut ein Sound spielt, und wie das fuer Menschen ohne
Tontechnik-Erfahrung beschriftet wird. Rein, ohne Geraet und ohne Tk.

Klangbild (Spec 2026-09-28): jeder Sound ist "effect" oder "music" und wird auf das Ziel
seiner Kategorie normalisiert - Effekte auf die Stimme (-20 LUFS), Musik wie Spotify
(-14 LUFS). Die Kabel ziehen danach "Sounds unter Stimme" bzw. "Musik unter Stimme" ab.
"""

from __future__ import annotations

import math
from typing import Any

from .dynamics import db_to_gain
from .loudness import NO_PEAK_DB, SILENT_LUFS

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

# Klangbild (spec 2026-09-28-klangbild, K1-K5)
CATEGORIES = ("effect", "music")
DEFAULT_CATEGORY = "effect"
CATEGORY_TARGET_LUFS = {"effect": VOICE_TARGET_LUFS, "music": -14.0}
TARGET_RANGE_LUFS = (-24.0, -10.0)
# Spotify "Normal" normalizes to about -14 LUFS. Assumption K5: holds while Spotify's
# own volume slider is at 100 % - the process loopback follows that slider.
SPOTIFY_REFERENCE_LUFS = -14.0
MUSIC_MIN_DURATION_S = 30.0  # K2: this long or longer counts as music
DEFAULT_MUSIC_OFFSET_DB = -3.0  # K4: "Musik unter Stimme" (Jan's Sofort-Wert)

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


def sound_category(sound: dict) -> str:
    """"effect" or "music"; anything missing or unknown is an effect."""
    category = sound.get("category")
    return category if category in CATEGORIES else DEFAULT_CATEGORY


def category_for_duration(seconds: Any) -> str | None:
    """K2: 30 s and longer = music. None when the length is unknown."""
    value = _float(seconds, math.nan)
    if not math.isfinite(value):
        return None
    return "music" if value >= MUSIC_MIN_DURATION_S else "effect"


def clamp_target(category: str, value: Any) -> float:
    """A target in LUFS for `category`: whole dB inside TARGET_RANGE_LUFS, nonsense
    becomes the category's default."""
    default = CATEGORY_TARGET_LUFS.get(category, CATEGORY_TARGET_LUFS[DEFAULT_CATEGORY])
    return float(round(_clamp(_float(value, default), *TARGET_RANGE_LUFS)))


def category_target(cfg: dict | None, category: str) -> float:
    if category not in CATEGORIES:
        category = DEFAULT_CATEGORY
    stored = (cfg or {}).get("klangbild_targets")
    return clamp_target(category, stored.get(category) if isinstance(stored, dict) else None)


def category_targets(cfg: dict | None) -> dict[str, float]:
    return {category: category_target(cfg, category) for category in CATEGORIES}


def normalization_db(loudness: dict | None, target: float = VOICE_TARGET_LUFS) -> float:
    """dB that bring a measured sound to `target`. The average decides, unless the
    loudest passage would then stick out more than 3 LU above the target - that
    catches songs that start at full level.

    F1: ebur128 needs a 400-ms block; a shorter cut measures SILENT_LUFS even though it
    has a real level (a real peak, above NO_PEAK_DB) - that is "too short to measure",
    not "actually silent", and must never be boosted (a cut is still allowed). A truly
    silent file (no real peak) keeps its old behaviour."""
    if not loudness or loudness.get("failed") or "integrated" not in loudness:
        return UNMEASURED_GAIN_DB
    integrated = float(loudness["integrated"])
    max_short = float(loudness.get("max_short", integrated))
    gain = min(target - integrated, target + SHORT_TERM_HEADROOM_LU - max_short)
    gain = _clamp(gain, MAX_CUT_DB, MAX_BOOST_DB)
    peak = _float(loudness.get("peak"), NO_PEAK_DB)
    too_short_to_measure = integrated <= SILENT_LUFS and peak > NO_PEAK_DB
    if too_short_to_measure:
        gain = min(gain, 0.0)
    return gain


def play_gain(sound: dict, volume: float, cfg: dict | None = None) -> float:
    """Linear gain for AudioEngine.play: the user's per-sound volume on top of the
    normalization to the target of the sound's category."""
    target = category_target(cfg, sound_category(sound))
    return volume * db_to_gain(normalization_db(sound.get("loudness"), target))


def sounds_offset_db(cfg: dict) -> float:
    return _clamp(_float(cfg.get("sounds_offset_db"), DEFAULT_OFFSET_DB), *OFFSET_RANGE_DB)


def music_offset_db(cfg: dict) -> float:
    """K4: how far music (tiles and the music bus) sits under the voice on the cables."""
    return _clamp(_float(cfg.get("music_offset_db"), DEFAULT_MUSIC_OFFSET_DB),
                  *OFFSET_RANGE_DB)


def musicbus_compensation_db(cfg: dict | None) -> float:
    """K5: the music bus cannot be measured ahead; Spotify delivers about -14 LUFS, so
    the bus is moved by the distance between the music target and that."""
    return category_target(cfg, "music") - SPOTIFY_REFERENCE_LUFS


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


def loudness_badge(sound: dict, cfg: dict | None = None) -> str:
    """Short text under a tile: what the automatic correction did."""
    measured = sound.get("loudness")
    if measured is None:
        return "misst …"
    if measured.get("failed"):
        return "nicht messbar"
    db = normalization_db(measured, category_target(cfg, sound_category(sound)))
    return "" if abs(db) < 1.0 else f"auto {format_db(db)}"


def loudness_summary(sound: dict, cfg: dict | None = None) -> str:
    """One sentence for the volume dialog."""
    measured = sound.get("loudness")
    if not measured or measured.get("failed") or "integrated" not in measured:
        return (f"Noch nicht gemessen – spielt vorsichtshalber mit "
                f"{format_db(UNMEASURED_GAIN_DB)}.")
    original = f"{float(measured['integrated']):.1f}".replace(".", ",")
    db = normalization_db(measured, category_target(cfg, sound_category(sound)))
    return (f"Automatisch angeglichen: {format_db(db)} "
            f"(gemessen {original} LUFS). Der Regler kommt obendrauf.")
