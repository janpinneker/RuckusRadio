"""Pegel-Logik ohne Geraet: Normalisierung, Config-Leser, dB-Texte fuer die UI."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-levels-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import dynamics, levels, loudness  # noqa: E402


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


def test_each_category_has_its_own_target():
    """Klangbild K1/K3: Effekte -20 (Stimme), Musik -14 (Spotify), geklemmt -24 ... -10
    in ganzen dB; Unsinn in der Config faellt auf den Standard zurueck."""
    assert levels.category_target({}, "effect") == -20.0
    assert levels.category_target({}, "music") == -14.0
    assert levels.category_target(None, "music") == -14.0
    assert levels.category_target({}, "voice") == -20.0, "unbekannt = Effekt"
    cfg = {"klangbild_targets": {"music": -12.4, "effect": "x"}}
    assert levels.category_target(cfg, "music") == -12.0, "ganze dB"
    assert levels.category_target(cfg, "effect") == -20.0, "Unsinn = Standard"
    assert levels.category_target({"klangbild_targets": {"music": -99}}, "music") == -24.0
    assert levels.category_target({"klangbild_targets": {"music": 0}}, "music") == -10.0
    assert levels.category_target({"klangbild_targets": "kaputt"}, "music") == -14.0
    assert levels.category_targets({}) == {"effect": -20.0, "music": -14.0}
    assert levels.clamp_target("music", -30) == -24.0
    assert levels.clamp_target("music", None) == -14.0
    print("each category has its own clamped target: OK")


def test_the_short_term_guard_is_relative_to_the_target():
    loud_start = {"integrated": -16.0, "max_short": -6.0, "peak": 0.0}
    assert abs(levels.normalization_db(loud_start, -14.0) - (-5.0)) < 1e-9
    assert abs(levels.normalization_db(loud_start) - (-11.0)) < 1e-9, "Standard bleibt die Stimme"
    even = {"integrated": -20.0, "max_short": -18.0, "peak": -3.0}
    assert abs(levels.normalization_db(even, -14.0) - 6.0) < 1e-9
    print("the 3-LU short-term guard follows the target: OK")


def test_play_gain_picks_the_target_by_category():
    measured = {"integrated": -20.0, "max_short": -20.0, "peak": -3.0}
    effect = {"loudness": measured}
    music = {"loudness": measured, "category": "music"}
    assert abs(levels.play_gain(effect, 1.0, {}) - 1.0) < 1e-12
    assert abs(levels.play_gain(music, 1.0, {}) - dynamics.db_to_gain(6.0)) < 1e-12
    cfg = {"klangbild_targets": {"music": -16.0}}
    assert abs(levels.play_gain(music, 0.5, cfg) - 0.5 * dynamics.db_to_gain(4.0)) < 1e-12
    assert abs(levels.play_gain(music, 1.0) - dynamics.db_to_gain(6.0)) < 1e-12, "ohne cfg: Standard"
    assert levels.sound_category({"category": "music"}) == "music"
    assert levels.sound_category({"category": "quatsch"}) == "effect"
    assert levels.sound_category({}) == "effect"
    print("play_gain picks the target by category: OK")


def test_music_has_its_own_offset_and_the_bus_a_fixed_compensation():
    assert levels.music_offset_db({}) == -3.0
    assert levels.music_offset_db({"music_offset_db": -99}) == -20.0
    assert levels.music_offset_db({"music_offset_db": 4}) == 0.0
    assert levels.music_offset_db({"music_offset_db": "x"}) == -3.0
    assert levels.musicbus_compensation_db({}) == 0.0, "Standard: Musik-Ziel = Spotify"
    assert levels.musicbus_compensation_db({"klangbild_targets": {"music": -18.0}}) == -4.0
    assert levels.musicbus_compensation_db({"klangbild_targets": {"music": -10.0}}) == 4.0
    print("music offset and the fixed music-bus compensation: OK")


def test_long_sounds_count_as_music():
    assert levels.category_for_duration(29.9) == "effect"
    assert levels.category_for_duration(30.0) == "music"
    assert levels.category_for_duration(184) == "music"
    assert levels.category_for_duration(None) is None
    assert levels.category_for_duration(float("nan")) is None
    assert levels.category_for_duration("x") is None
    print("sounds of 30 s and more count as music: OK")


def test_short_cuts_get_no_boost_but_real_silence_is_unchanged():
    """F1: ebur128 braucht einen 400-ms-Block; ein kuerzerer Schnitt misst -70 LUFS
    (SILENT_LUFS) obwohl er echten Pegel hat (peak > NO_PEAK_DB) - das ist "zu kurz zum
    Messen", nicht "wirklich still", und darf nicht angehoben werden (Absenken bleibt
    erlaubt). Eine wirklich stille Datei (kein echter Peak) bleibt wie bisher."""
    too_short = {"integrated": -70.0, "max_short": -70.0, "peak": -16.5}
    assert levels.normalization_db(too_short) <= 0.0, levels.normalization_db(too_short)
    whole_file = {"integrated": -40.0, "max_short": -38.0, "peak": -20.0}
    assert levels.normalization_db(whole_file) == 12.0, "unveraendert ausserhalb des Kurz-Falls"
    past_end_with_peak = {"integrated": -70.0, "max_short": -70.0, "peak": -30.0}
    assert levels.normalization_db(past_end_with_peak) <= 0.0
    really_silent = {"integrated": -70.0, "max_short": -70.0, "peak": loudness.NO_PEAK_DB}
    assert levels.normalization_db(really_silent) == 12.0, "wirklich still: wie bisher behandelt"
    print("kurze Schnitte werden nicht angehoben, wirklich stille Dateien bleiben unveraendert: OK")


def test_badge_and_summary_follow_the_category():
    measured = {"integrated": -10.8, "max_short": -10.0, "peak": 0.0}
    assert levels.loudness_badge({"loudness": measured}) == "auto -9 dB"
    assert levels.loudness_badge({"loudness": measured, "category": "music"}) == "auto -3 dB"
    assert "-3 dB" in levels.loudness_summary({"loudness": measured, "category": "music"})
    print("badge and summary follow the category: OK")


def main():
    test_loud_songs_come_down_to_the_voice()
    test_a_loud_start_wins_over_the_average()
    test_quiet_sounds_are_lifted_but_capped()
    test_unmeasured_sounds_are_cautious()
    test_play_gain_multiplies_the_user_volume()
    test_config_readers_clamp_and_default()
    test_texts_give_everyday_examples()
    test_badges_and_summary()
    test_each_category_has_its_own_target()
    test_the_short_term_guard_is_relative_to_the_target()
    test_play_gain_picks_the_target_by_category()
    test_music_has_its_own_offset_and_the_bus_a_fixed_compensation()
    test_long_sounds_count_as_music()
    test_short_cuts_get_no_boost_but_real_silence_is_unchanged()
    test_badge_and_summary_follow_the_category()
    print("\nALL LEVELS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
