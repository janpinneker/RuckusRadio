"""Pegel-Werkzeuge ohne Geraet: dB-Umrechnung, Limiter, Ducker, Leveler."""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

_TMP = tempfile.mkdtemp(prefix="ruckus-dynamics-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import dynamics  # noqa: E402

RATE = 48_000
FRAMES = 480


def sine(level_db, frames=FRAMES, freq=200.0, start=0):
    """Mono sine whose RMS is exactly level_db dBFS. 200 Hz = exactly two periods per
    10-ms block, so a single block's RMS is exact too."""
    t = (np.arange(frames) + start) / RATE
    amplitude = dynamics.db_to_gain(level_db) * np.sqrt(2.0)
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32).reshape(-1, 1)


def test_db_conversion():
    assert abs(dynamics.db_to_gain(-6.0) - 0.501187) < 1e-5
    assert abs(dynamics.gain_to_db(0.5) - (-6.0206)) < 1e-3
    assert dynamics.gain_to_db(0.0) == dynamics.SILENCE_DB
    assert dynamics.block_rms_db(np.zeros((FRAMES, 2), np.float32)) == dynamics.SILENCE_DB
    assert abs(dynamics.block_rms_db(sine(-30.0)) - (-30.0)) < 0.1
    print("dB conversion: OK")


def test_limiter_caps_peaks_immediately():
    limiter = dynamics.Limiter(ceiling_db=-1.0, release_ms=200.0, samplerate=RATE)
    out = limiter.process(np.full((FRAMES, 2), 1.6, np.float32))
    assert float(np.max(np.abs(out))) <= dynamics.db_to_gain(-1.0) + 1e-6, float(np.max(np.abs(out)))
    print("limiter caps peaks in the same block: OK")


def test_limiter_leaves_quiet_audio_untouched():
    limiter = dynamics.Limiter(samplerate=RATE)
    quiet = np.full((FRAMES, 2), 0.3, np.float32)
    original = quiet.copy()
    assert np.array_equal(limiter.process(quiet), original)
    assert np.array_equal(quiet, original), "the input must never be modified"
    print("limiter leaves quiet audio untouched: OK")


def test_limiter_releases_gradually():
    limiter = dynamics.Limiter(ceiling_db=-1.0, release_ms=200.0, samplerate=RATE)
    limiter.process(np.full((FRAMES, 2), 1.6, np.float32))
    after_attack = limiter.gain
    limiter.process(np.full((FRAMES, 2), 0.1, np.float32))
    assert after_attack < limiter.gain < 1.0, "release must be gradual, not a jump"
    for _ in range(300):
        limiter.process(np.full((FRAMES, 2), 0.1, np.float32))
    assert limiter.gain > 0.999, limiter.gain
    limiter.process(np.full((FRAMES, 2), 1.6, np.float32))
    limiter.reset()
    assert limiter.gain == 1.0
    print("limiter releases gradually and resets: OK")


def test_ducker_follows_the_voice():
    ducker = dynamics.Ducker(depth_db=-6.0, attack_ms=50.0, release_ms=400.0, samplerate=RATE)
    for _ in range(50):  # 0.5 s speaking
        ducker.next_ramp(True, FRAMES)
    assert abs(dynamics.gain_to_db(ducker.gain) + 6.0) < 0.1, ducker.gain
    ducker.next_ramp(False, FRAMES)
    assert ducker.gain > dynamics.db_to_gain(-6.0), "release starts right away"
    for _ in range(500):  # 5 s silence
        ducker.next_ramp(False, FRAMES)
    assert ducker.gain == 1.0, ducker.gain
    print("ducker follows the voice: OK")


def test_disabled_ducker_never_ducks():
    ducker = dynamics.Ducker(depth_db=-6.0, samplerate=RATE)
    ducker.enabled = False
    for _ in range(50):
        ramp = ducker.next_ramp(True, FRAMES)
    assert ducker.gain == 1.0 and float(np.min(ramp)) == 1.0
    print("disabled ducker never ducks: OK")


def test_leveler_lifts_quiet_speech_to_target():
    leveler = dynamics.Leveler(target_db=-20.0, samplerate=RATE)
    out = None
    for i in range(400):  # 4 s at -36 dBFS
        out = leveler.process(sine(-36.0, start=i * FRAMES))
    assert abs(dynamics.block_rms_db(out) - (-20.0)) < 1.0, dynamics.block_rms_db(out)
    assert leveler.speaking
    print("leveler lifts quiet speech to the target: OK")


def test_leveler_tames_loud_speech():
    leveler = dynamics.Leveler(target_db=-20.0, max_cut_db=-12.0, samplerate=RATE)
    out = None
    for i in range(100):  # 1 s at -6 dBFS
        out = leveler.process(sine(-6.0, start=i * FRAMES))
    assert abs(dynamics.block_rms_db(out) - (-18.0)) < 1.0, dynamics.block_rms_db(out)
    print("leveler tames loud speech down to max_cut: OK")


def test_leveler_attenuates_pauses():
    leveler = dynamics.Leveler(target_db=-20.0, expander_db=-10.0, samplerate=RATE)
    for i in range(100):
        leveler.process(sine(-30.0, start=i * FRAMES))
    out = None
    for i in range(60):  # 0.6 s pause, longer than the 300 ms hangover
        out = leveler.process(sine(-65.0, start=i * FRAMES))
    assert not leveler.speaking
    assert dynamics.block_rms_db(out) < -65.0 + leveler.gain_db - 8.0, dynamics.block_rms_db(out)
    print("leveler attenuates pauses: OK")


def test_leveler_does_not_modify_its_input():
    leveler = dynamics.Leveler(samplerate=RATE)
    block = sine(-36.0)
    original = block.copy()
    leveler.process(block)
    assert np.array_equal(block, original)
    print("leveler does not modify its input: OK")


def feed_music(leveler, level_db, seconds, start=0):
    """Stereo sine at level_db dBFS RMS through the music leveler for `seconds`;
    returns the last output block."""
    out = None
    for i in range(int(seconds * RATE / FRAMES)):
        mono = sine(level_db, start=start + i * FRAMES)
        out = leveler.process(np.hstack([mono, mono]))
    return out


def test_music_leveler_lifts_a_quiet_spotify_to_the_reference():
    # Jan 2026-09-30: Spotify turned down for his ears must still reach the cables
    # at the "Spotify 100 %" level - the bus levels itself (Auto-Pegel).
    lev = dynamics.MusicLeveler()
    out = feed_music(lev, -34.0, 20.0)
    assert abs(dynamics.block_rms_db(out) - dynamics.MUSIC_REFERENCE_DB) < 1.0, dynamics.block_rms_db(out)
    assert abs(lev.gain_db - 20.0) < 1.0, lev.gain_db
    print("Musik-Leveler hebt leises Spotify auf den Bezug: OK")


def test_music_leveler_boost_and_cut_are_capped():
    lev = dynamics.MusicLeveler()
    feed_music(lev, -55.0, 30.0)
    assert abs(lev.gain_db - dynamics.MUSIC_MAX_BOOST_DB) < 1e-6, lev.gain_db
    lev = dynamics.MusicLeveler()
    feed_music(lev, 0.0, 10.0)
    assert abs(lev.gain_db - dynamics.MUSIC_MAX_CUT_DB) < 1e-6, lev.gain_db
    print("Musik-Leveler: Boost und Absenkung begrenzt: OK")


def test_music_leveler_rises_slowly_and_holds_through_silence():
    lev = dynamics.MusicLeveler()
    feed_music(lev, -34.0, 1.0)
    assert 0.0 < lev.gain_db <= dynamics.MusicLeveler().rise_db_per_s + 1e-6, lev.gain_db
    feed_music(lev, -34.0, 20.0)
    settled = lev.gain_db
    feed_music(lev, -120.0, 5.0)  # a pause in Spotify: no boost creep, no drop
    assert abs(lev.gain_db - settled) < 1e-6, (lev.gain_db, settled)
    print("Musik-Leveler steigt langsam und haelt in Pausen: OK")


def test_music_leveler_follows_a_louder_spotify_down():
    lev = dynamics.MusicLeveler()
    feed_music(lev, -34.0, 20.0)
    feed_music(lev, -14.0, 10.0)
    assert abs(lev.gain_db) < 1.0, lev.gain_db
    print("Musik-Leveler folgt lauterem Spotify: OK")


def main():
    test_db_conversion()
    test_limiter_caps_peaks_immediately()
    test_limiter_leaves_quiet_audio_untouched()
    test_limiter_releases_gradually()
    test_ducker_follows_the_voice()
    test_disabled_ducker_never_ducks()
    test_leveler_lifts_quiet_speech_to_target()
    test_leveler_tames_loud_speech()
    test_leveler_attenuates_pauses()
    test_leveler_does_not_modify_its_input()
    test_music_leveler_lifts_a_quiet_spotify_to_the_reference()
    test_music_leveler_boost_and_cut_are_capped()
    test_music_leveler_rises_slowly_and_holds_through_silence()
    test_music_leveler_follows_a_louder_spotify_down()
    print("\nALL DYNAMICS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
