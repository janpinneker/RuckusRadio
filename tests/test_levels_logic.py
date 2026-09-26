"""Pegel-Logik ohne Geraet: Normalisierung, Config-Leser, dB-Texte fuer die UI."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-levels-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import dynamics, levels  # noqa: E402


def test_loud_songs_come_down_to_the_voice():
    db = levels.normalization_db({"integrated": -10.8, "max_short": -9.0, "peak": 0.0})
    assert abs(db - (-9.2)) < 1e-9, db
    print("loud song is brought down to voice loudness: OK")


def test_a_loud_start_wins_over_the_average():
    db = levels.normalization_db({"integrated": -16.0, "max_short": -6.0, "peak": 0.0})
    assert abs(db - (-11.0)) < 1e-9, "max short-term may sit at most 3 LU above the voice"
    print("a loud start wins over the average: OK")


def test_quiet_sounds_are_lifted_but_capped():
    db = levels.normalization_db({"integrated": -40.0, "max_short": -38.0, "peak": -20.0})
    assert db == 12.0, db
    print("quiet sounds are lifted at most +12 dB: OK")


def test_unmeasured_sounds_are_cautious():
    assert levels.normalization_db(None) == levels.UNMEASURED_GAIN_DB == -18.0
    assert levels.normalization_db({"failed": True}) == -18.0
    print("unmeasured sounds play at -18 dB: OK")


def test_play_gain_multiplies_the_user_volume():
    sound = {"loudness": {"integrated": -20.0, "max_short": -20.0, "peak": -3.0}}
    assert abs(levels.play_gain(sound, 0.5) - 0.5) < 1e-12
    quiet = {"loudness": {"integrated": -26.0, "max_short": -26.0, "peak": -9.0}}
    assert abs(levels.play_gain(quiet, 1.0) - dynamics.db_to_gain(6.0)) < 1e-12
    print("play_gain = volume x normalization: OK")


def test_config_readers_clamp_and_default():
    assert levels.sounds_offset_db({}) == -6.0
    assert levels.sounds_offset_db({"sounds_offset_db": -99}) == -20.0
    assert levels.sounds_offset_db({"sounds_offset_db": "x"}) == -6.0
    assert levels.ducking({}) == (True, -6.0)
    assert levels.ducking({"ducking_enabled": False, "ducking_db": 5}) == (False, 0.0)
    print("config readers clamp and default: OK")


def test_texts_give_everyday_examples():
    assert levels.format_db(0.2) == "0 dB"
    assert levels.format_db(3.52) == "+3,5 dB"
    assert levels.describe_db(0.0) == "0 dB · unverändert"
    assert levels.describe_db(-6.0) == "-6 dB · deutlich leiser"
    assert levels.describe_db(-10.0) == "-10 dB · etwa halb so laut"
    assert levels.describe_db(-20.0) == "-20 dB · leise im Hintergrund"
    assert levels.describe_db(-35.0) == "-35 dB · kaum hörbar"
    assert levels.describe_db(3.5) == "+3,5 dB · etwas lauter"
    assert levels.describe_db(6.0) == "+6 dB · deutlich lauter"
    assert levels.describe_offset(0.0) == "0 dB · gleich laut wie deine Stimme"
    assert levels.describe_offset(-6.0) == "-6 dB · deutlich leiser als deine Stimme"
    assert levels.describe_offset(-10.0) == "-10 dB · etwa halb so laut wie deine Stimme"
    print("dB texts give everyday examples: OK")


def test_badges_and_summary():
    assert levels.loudness_badge({}) == "misst …"
    assert levels.loudness_badge({"loudness": {"failed": True}}) == "nicht messbar"
    assert levels.loudness_badge({"loudness": {"integrated": -20.0, "max_short": -20.0}}) == ""
    assert levels.loudness_badge({"loudness": {"integrated": -10.8, "max_short": -10.0}}) == "auto -9 dB"
    assert "-10,8 LUFS" in levels.loudness_summary(
        {"loudness": {"integrated": -10.8, "max_short": -10.0, "peak": 0.0}})
    assert "-18 dB" in levels.loudness_summary({})
    print("tile badge and dialog summary: OK")


def main():
    test_loud_songs_come_down_to_the_voice()
    test_a_loud_start_wins_over_the_average()
    test_quiet_sounds_are_lifted_but_capped()
    test_unmeasured_sounds_are_cautious()
    test_play_gain_multiplies_the_user_volume()
    test_config_readers_clamp_and_default()
    test_texts_give_everyday_examples()
    test_badges_and_summary()
    print("\nALL LEVELS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
