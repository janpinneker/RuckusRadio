"""Does sound actually reach the voice chat? Answer it without opening Discord.

The old check only asked "does a device called 'VoiceMeeter Input' exist" and then
reported "connected" - which stays green even when the virtual cable is dead (wrong
bus selected, mixer not routing, nothing assigned to the hardware input). These two
checks measure real audio instead:

* `verify_path`  - play a tone into the virtual mic's input side while recording from
  its output side. Silence means nothing would reach the voice chat either.
* `verify_mic`   - record from the real microphone and look at the level.

Both block for about a second, so callers run them on a worker thread and hand the
result back through `call_in_ui`.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import sounddevice as sd

TONE_HZ = 440.0
TONE_AMPLITUDE = 0.3
SIGNAL_RMS_MIN = 0.01  # below this the captured signal is indistinguishable from silence
MIC_RMS_MIN = 0.002  # mic noise floor is usually above this; a muted device is not
SAMPLERATE = 48000

# PortAudio's own paInvalidSampleRate. Measured for real: a virtual cable's two sides
# are two separate Windows devices with their own "Standardformat" setting, and WASAPI
# shared mode demands the stream open at EXACTLY that rate - a cable whose Output side
# was left at 44100 Hz while its Input side is 48000 Hz raises this the instant the
# mixer (fixed at SAMPLERATE) tries to open it, surfacing to the user as a bare
# "PaErrorCode -9997" with no indication of which device or what to do about it.
PA_INVALID_SAMPLE_RATE = -9997

log = logging.getLogger(__name__)


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def tone(seconds: float, samplerate: int = SAMPLERATE, channels: int = 2) -> np.ndarray:
    t = np.arange(int(seconds * samplerate), dtype=np.float32) / samplerate
    wave = (TONE_AMPLITUDE * np.sin(2 * np.pi * TONE_HZ * t)).astype(np.float32)
    return np.repeat(wave.reshape(-1, 1), channels, axis=1)


def _is_invalid_sample_rate(exc: Exception) -> bool:
    """True for the PortAudio error WASAPI raises when a device's Windows
    "Standardformat" is not exactly SAMPLERATE. Keyed on sounddevice's own error code
    (`PortAudioError.args[1]`, see its docstring), not on matching PortAudio's English
    error text - that text is not guaranteed stable and is not localized anyway."""
    return (isinstance(exc, sd.PortAudioError) and len(exc.args) > 1
            and exc.args[1] == PA_INVALID_SAMPLE_RATE)


def _sample_rate_mismatch_reason(*indices: int | None) -> str:
    """Name the device(s) among `indices` whose current Windows format is not
    SAMPLERATE, so the fix (device properties -> "Erweitert" -> "Standardformat") is
    obvious instead of a bare PaErrorCode. Falls back to a generic sentence when the
    device list cannot be queried either (e.g. PortAudio itself is in a bad state)."""
    culprits = []
    for index in indices:
        if index is None:
            continue
        try:
            rate = sd.query_devices(index)["default_samplerate"]
            name = sd.query_devices(index)["name"]
        except Exception:
            continue
        if rate and round(rate) != SAMPLERATE:
            culprits.append(f"„{name}“ ist nicht auf {SAMPLERATE} Hz eingestellt")
    detail = "; ".join(culprits) if culprits else "eines der beteiligten Geräte ist falsch eingestellt"
    return (f"Prüfung fehlgeschlagen: {detail}. In den Windows-Sound-Einstellungen bei "
            f"diesem Gerät unter „Erweitert“ das Standardformat auf {SAMPLERATE} Hz stellen.")


def verdict(level: float, threshold: float, ok_reason: str, bad_reason: str) -> dict:
    ok = level >= threshold
    return {"ok": ok, "rms": round(level, 5), "reason": ok_reason if ok else bad_reason}


def _play_and_record(signal: np.ndarray, out_index: int, in_index: int,
                     chunk: int = SAMPLERATE // 20) -> np.ndarray:
    """Write `signal` to `out_index` while draining `in_index`, in 50 ms slices.

    Deliberately two independent streams instead of sd.playrec: input and output of a
    virtual cable usually sit on different host APIs (DirectSound output, WASAPI input),
    and a duplex stream cannot span two of them."""
    captured: list[np.ndarray] = []
    with sd.InputStream(samplerate=SAMPLERATE, device=in_index, channels=1,
                        dtype="float32") as rec, \
            sd.OutputStream(samplerate=SAMPLERATE, device=out_index, channels=2,
                            dtype="float32") as play:
        for pos in range(0, len(signal), chunk):
            play.write(signal[pos:pos + chunk])
            available = rec.read_available
            if available:
                captured.append(rec.read(available)[0])
        time.sleep(0.2)  # let the tail travel through the cable
        available = rec.read_available
        if available:
            captured.append(rec.read(available)[0])
    return np.concatenate(captured) if captured else np.zeros((0, 1), dtype=np.float32)


def verify_path(out_index: int | None, in_index: int | None, seconds: float = 1.0) -> dict:
    """Play a tone into `out_index` while recording `in_index`. {"ok", "rms", "reason"}."""
    if out_index is None:
        return {"ok": False, "rms": 0.0, "reason": "Kein virtuelles Mikrofon gefunden."}
    if in_index is None:
        return {"ok": False, "rms": 0.0,
                "reason": "Das Aufnahme-Gegenstück des virtuellen Mikrofons fehlt."}
    try:
        recorded = _play_and_record(tone(seconds), out_index, in_index)
    except Exception as exc:  # noqa: BLE001 - every OTHER device failure looks the same
        log.warning("verify_path failed", exc_info=True)
        if _is_invalid_sample_rate(exc):
            return {"ok": False, "rms": 0.0,
                    "reason": _sample_rate_mismatch_reason(out_index, in_index)}
        return {"ok": False, "rms": 0.0, "reason": f"Prüfung fehlgeschlagen: {exc}"}
    return verdict(
        rms(recorded), SIGNAL_RMS_MIN,
        "Signal kommt an — die Gegenstelle hört deine Sounds.",
        "Kein Signal. Das virtuelle Kabel leitet nichts weiter.",
    )


def verify_mic(in_index: int | None, seconds: float = 1.5) -> dict:
    """Record from the real mic. {"ok", "rms", "reason"} - ok means: there is a level."""
    if in_index is None:
        return {"ok": False, "rms": 0.0, "reason": "Kein Mikrofon ausgewählt."}
    try:
        recorded = sd.rec(int(seconds * SAMPLERATE), samplerate=SAMPLERATE, device=in_index,
                          channels=1, dtype="float32")
        sd.wait()
    except Exception as exc:  # noqa: BLE001 - every OTHER device failure looks the same
        log.warning("verify_mic failed", exc_info=True)
        if _is_invalid_sample_rate(exc):
            return {"ok": False, "rms": 0.0, "reason": _sample_rate_mismatch_reason(in_index)}
        return {"ok": False, "rms": 0.0, "reason": f"Prüfung fehlgeschlagen: {exc}"}
    return verdict(
        rms(recorded), MIC_RMS_MIN,
        "Mikrofon liefert Signal.",
        "Mikrofon liefert nichts — stummgeschaltet oder falsches Gerät?",
    )
