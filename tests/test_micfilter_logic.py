"""Mikrofon-Kette ohne Geraet: Hochpass, Leveler, Limiter, Sprech-Erkennung."""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

_TMP = tempfile.mkdtemp(prefix="ruckus-micfilter-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import dynamics  # noqa: E402
from soundboard.micfilter import VOICE_TARGET_DB, MicChain  # noqa: E402

RATE = 48_000
FRAMES = 480


def sine(level_db, start=0, freq=200.0):
    t = (np.arange(FRAMES) + start) / RATE
    amplitude = dynamics.db_to_gain(level_db) * np.sqrt(2.0)
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32).reshape(-1, 1)


def test_output_is_mono_column():
    chain = MicChain(RATE)
    out = chain.process(np.zeros(FRAMES, np.float32))
    assert out.shape == (FRAMES, 1) and out.dtype == np.float32, (out.shape, out.dtype)
    assert chain.process(np.zeros((0, 1), np.float32)).shape == (0, 1)
    print("mic chain returns a mono column: OK")


def test_highpass_removes_rumble():
    chain = MicChain(RATE, highpass_hz=80.0)
    out = None
    for _ in range(20):
        out = chain.process(np.full((FRAMES, 1), 0.2, np.float32))
    assert float(np.abs(out).mean()) < 0.01, float(np.abs(out).mean())
    print("high-pass removes steady rumble: OK")


def test_quiet_speech_reaches_the_voice_target():
    chain = MicChain(RATE)
    out = None
    for i in range(400):
        out = chain.process(sine(-36.0, start=i * FRAMES))
    assert abs(dynamics.block_rms_db(out) - VOICE_TARGET_DB) < 1.5, dynamics.block_rms_db(out)
    assert chain.speaking
    print("quiet speech reaches the voice target: OK")


def test_speaking_ends_after_silence():
    chain = MicChain(RATE)
    for i in range(50):
        chain.process(sine(-30.0, start=i * FRAMES))
    assert chain.speaking
    for _ in range(60):
        chain.process(np.zeros((FRAMES, 1), np.float32))
    assert not chain.speaking
    print("speaking ends after 0.6 s of silence: OK")


def test_peaks_never_pass_the_ceiling():
    chain = MicChain(RATE)
    for i in range(30):
        out = chain.process(sine(-2.0, start=i * FRAMES))
        assert float(np.max(np.abs(out))) <= dynamics.db_to_gain(-1.0) + 1e-6
    print("voice peaks never pass -1 dBFS: OK")


def main():
    test_output_is_mono_column()
    test_highpass_removes_rumble()
    test_quiet_speech_reaches_the_voice_target()
    test_speaking_ends_after_silence()
    test_peaks_never_pass_the_ceiling()
    print("\nALL MIC FILTER LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
