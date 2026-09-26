"""miccheck's PortAudio-error handling, pure: no real device is ever opened.

Hardware finding: a Hi-Fi Cable whose Output side sits at 44100 Hz in Windows while
its Input side is 48000 Hz makes sounddevice raise PortAudioError with PaErrorCode
-9997 (paInvalidSampleRate) the instant the mixer - fixed at miccheck.SAMPLERATE -
tries to open it. That used to reach the user as a bare "PaErrorCode -9997" via the
generic `f"Prüfung fehlgeschlagen: {exc}"` wrapper. These tests fake sounddevice's
InputStream/OutputStream/query_devices so the exact failure is reproduced without any
real hardware, and check the message names the misconfigured device and says what to
change in Windows instead of leaking the raw code."""

import logging
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-test-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sounddevice as sd  # noqa: E402

from soundboard import miccheck  # noqa: E402

# Every case here deliberately makes a stream fail to open - keep the expected
# log.warning(..., exc_info=True) calls out of the test output.
logging.getLogger("soundboard").propagate = False


class _FakeStream:
    """Stands in for sd.InputStream/OutputStream: a context manager that either opens
    cleanly or raises on __enter__, exactly where PortAudio itself would fail."""

    def __init__(self, device, raise_on_enter=None):
        self.device = device
        self._raise = raise_on_enter

    def __enter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def read_available(self):
        return 0

    def read(self, n):
        import numpy as np
        return np.zeros((0, 1), dtype=np.float32), False

    def write(self, data):
        pass


def _pa_error(code):
    return sd.PortAudioError(f"Error opening stream [PaErrorCode {code}]", code)


def test_is_invalid_sample_rate_keys_on_the_error_code():
    assert miccheck._is_invalid_sample_rate(_pa_error(-9997)) is True
    assert miccheck._is_invalid_sample_rate(_pa_error(-9998)) is False, \
        "a different PortAudio error code must not be treated as a sample-rate problem"
    assert miccheck._is_invalid_sample_rate(RuntimeError("device busy")) is False, \
        "a non-PortAudio exception must never be mistaken for this"
    assert miccheck._is_invalid_sample_rate(sd.PortAudioError("no code")) is False
    print("is_invalid_sample_rate keys on the error code: OK")


def test_sample_rate_mismatch_names_the_wrong_device_only():
    real_query = sd.query_devices
    catalogue = {
        28: {"name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)", "default_samplerate": 48000.0},
        38: {"name": "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)", "default_samplerate": 44100.0},
    }
    sd.query_devices = lambda index: catalogue[index]
    try:
        reason = miccheck._sample_rate_mismatch_reason(28, 38)
        assert "Hi-Fi Cable Output" in reason, reason
        assert "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)" not in reason, (
            f"the correctly-configured device must not be blamed: {reason}")
        assert "48000 Hz" in reason and "Standardformat" in reason, reason
        assert "PaErrorCode" not in reason, "the raw code must not leak through: " + reason
        print("sample rate mismatch names the wrong device only:", reason)
    finally:
        sd.query_devices = real_query


def test_sample_rate_mismatch_falls_back_when_nothing_can_be_named():
    real_query = sd.query_devices
    sd.query_devices = lambda index: (_ for _ in ()).throw(RuntimeError("gone"))
    try:
        reason = miccheck._sample_rate_mismatch_reason(28, 38)
        assert "Standardformat" in reason and "48000 Hz" in reason, reason
    finally:
        sd.query_devices = real_query
    print("sample rate mismatch falls back gracefully: OK")


def test_verify_path_explains_a_sample_rate_mismatch():
    real_input, real_output, real_query = sd.InputStream, sd.OutputStream, sd.query_devices
    catalogue = {
        29: {"name": "CABLE Input (VB-Audio Virtual Cable)", "default_samplerate": 48000.0},
        39: {"name": "CABLE Output (VB-Audio Virtual Cable)", "default_samplerate": 44100.0},
    }
    sd.query_devices = lambda index: catalogue[index]
    sd.InputStream = lambda **kw: _FakeStream(kw.get("device"), raise_on_enter=_pa_error(-9997))
    sd.OutputStream = lambda **kw: _FakeStream(kw.get("device"))
    try:
        result = miccheck.verify_path(29, 39, seconds=0.01)
        assert result["ok"] is False
        assert "CABLE Output (VB-Audio Virtual Cable)" in result["reason"], result
        assert "48000 Hz" in result["reason"] and "Standardformat" in result["reason"], result
        assert "PaErrorCode" not in result["reason"], result
        print("verify_path explains a sample rate mismatch:", result["reason"])
    finally:
        sd.InputStream, sd.OutputStream, sd.query_devices = real_input, real_output, real_query


def test_verify_path_keeps_the_plain_message_for_other_failures():
    """Regression guard: only the specific -9997 case gets the friendlier message -
    every other device failure must still show through unmodified, as it always did."""
    real_input, real_output = sd.InputStream, sd.OutputStream
    sd.InputStream = lambda **kw: _FakeStream(kw.get("device"), raise_on_enter=RuntimeError("device busy"))
    sd.OutputStream = lambda **kw: _FakeStream(kw.get("device"))
    try:
        result = miccheck.verify_path(29, 39, seconds=0.01)
        assert result["ok"] is False
        assert result["reason"] == "Prüfung fehlgeschlagen: device busy", result
    finally:
        sd.InputStream, sd.OutputStream = real_input, real_output
    print("verify_path keeps the plain message for other failures: OK")


def test_verify_mic_explains_a_sample_rate_mismatch():
    real_rec, real_wait, real_query = sd.rec, sd.wait, sd.query_devices
    sd.query_devices = lambda index: {"name": "Mikrofon (Realtek Audio)", "default_samplerate": 44100.0}

    def fake_rec(*a, **kw):
        raise _pa_error(-9997)

    sd.rec = fake_rec
    sd.wait = lambda: None
    try:
        result = miccheck.verify_mic(7, seconds=0.01)
        assert result["ok"] is False
        assert "Mikrofon (Realtek Audio)" in result["reason"], result
        assert "48000 Hz" in result["reason"], result
        print("verify_mic explains a sample rate mismatch:", result["reason"])
    finally:
        sd.rec, sd.wait, sd.query_devices = real_rec, real_wait, real_query


def main():
    test_is_invalid_sample_rate_keys_on_the_error_code()
    test_sample_rate_mismatch_names_the_wrong_device_only()
    test_sample_rate_mismatch_falls_back_when_nothing_can_be_named()
    test_verify_path_explains_a_sample_rate_mismatch()
    test_verify_path_keeps_the_plain_message_for_other_failures()
    test_verify_mic_explains_a_sample_rate_mismatch()
    print("\nALL MICCHECK LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
