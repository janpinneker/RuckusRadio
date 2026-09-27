r"""Handpruefung: WASAPI-Loopback ohne neue Abhaengigkeit (nur ctypes/COM).

    venv\Scripts\python.exe tests\test_loopback_manual.py

Braucht echte Audio-Hardware, laeuft deshalb nicht in der gruenen Testschleife
(dasselbe Muster wie test_audio_manual.py und test_cable_manual.py).

Wozu: Der Musik-Bus soll den Ton der Spotify-Desktop-App abgreifen, damit Discord und
Steam ihn hoeren. PortAudio kann das nicht - gemessen gibt es 0 Loopback-Geraete und
`WasapiSettings` bietet keine Loopback-Option (offener Feature-Request upstream).
`defaultdevice.py` zeigt aber, dass die zwei COM-Aufrufe, die Ruckus braucht, schon per
ctypes gehen. Dieses Skript klaert, ob das fuer den Mitschitt genauso gilt - **ohne**
`soundcard` oder `PyAudioWPatch` und ohne ein zweites PortAudio im Prozess.

Was es prueft:

1. **Format** des Standard-Wiedergabegeraets (so muss der Mitschitt geoeffnet werden).
2. **Stille** - Mitschitt, waehrend nichts laeuft. Beweist, dass der Client ueberhaupt
   oeffnet und Pakete liefert.
3. **Echtes Signal** - ein 440-Hz-Ton laeuft, waehrend mitgeschnitten wird. **Das ist der
   eigentliche Beweis**: kommt ein Pegel an oder nur Nullen?
4. **Kein Stoeren** - laeuft `sounddevice` weiter, waehrend der Loopback-Client offen ist?
   Genau das ist die Sorge: zwei Audio-Stacks im selben Prozess.
5. **Zweiter Client** - kann ein zweiter Mitschitt parallel laufen (Mehrfach-Abgriff)?

Nur Messen: das Skript oeffnet den Standardausgang im **Shared Mode**. Es aendert keine
Systemeinstellung, schreibt nichts auf Platte und laesst das echte Datenverzeichnis in
Ruhe (es wird gar kein Kern gebaut).

**Ergebnis der Messung (2026-09-27, Jans Rechner):** alle fuenf Pruefungen bestanden.
Format 48000 Hz / 2 ch / 32-bit float; der eigene 440-Hz-Ton hob den Pegel um +16.9 dB
(rms 0.026 -> 0.181; fuer Amplitude 0.25 sind 0.177 zu erwarten). `sounddevice` lief mit
offenen 57 Geraeten und einem laufenden Ausgabestream parallel, zwei Loopback-Clients
gleichzeitig. **Damit braucht der Musik-Bus keine neue Abhaengigkeit** - `soundcard` und
`PyAudioWPatch` sind vom Tisch, es bleibt bei `ctypes` nach dem Muster von
`defaultdevice.py`. Zwei Fallen, die die echte Umsetzung kennen muss:

- **Auf das Format achten.** Die Erweiterung von `WAVEFORMATEX` muss ueber feste
  Offsets gelesen werden: `ctypes.Structure` richtet aus und schiebt zwei Byte Padding
  ein, dann liest `SubFormat.Data1` den Kanal-Mask (3 fuer Stereo) statt des Formats.
  Ein Float-Format wird so still als Integer gedeutet und liefert plausible Zahlen.
  Der Fehler ist in diesem Skript passiert und kostete einen ganzen Messlauf.
- **Exclusive Mode.** Der Abgriff ist Shared Mode. Nimmt eine andere App das Geraet
  exklusiv (Spiele, DAWs), liefert der Mitschitt nichts. Nicht gemessen, weil das den
  Ton auf Jans Rechner gestoert haette - offen fuer F3 mit echten Geraeten.
- **Geraetewechsel.** Der Mitschitt haengt an der Endpoint-ID vom Oeffnen. Wechselt der
  Windows-Standard, muss der Client neu gebaut werden - dieselbe Aufgabe, die
  `DefaultDeviceWatcher` fuer den Kopfhoerer schon loest.
"""

from __future__ import annotations

import ctypes
import math
import struct
import sys
import time
import uuid
from ctypes import POINTER, byref, c_void_p, wintypes

# ---------------------------------------------------------------------------
# COM-Grundlagen (dieselbe minimale Bauart wie soundboard/defaultdevice.py)
# ---------------------------------------------------------------------------

CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAUDIO_CLIENT = "1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"
IID_IAUDIO_CAPTURE_CLIENT = "C8ADBD64-E71E-48a0-A4DE-185C395CD317"

CLSCTX_ALL = 23
COINIT_MULTITHREADED = 0x0

E_RENDER = 0
E_CONSOLE = 0  # das Geraet, das Windows "Standardgeraet" nennt

VTBL_RELEASE = 2

# IMMDevice
VTBL_DEVICE_ACTIVATE = 3
VTBL_DEVICE_GET_ID = 5

# IAudioClient
VTBL_AC_INITIALIZE = 3
VTBL_AC_GET_BUFFER_SIZE = 4
VTBL_AC_GET_MIX_FORMAT = 8
VTBL_AC_START = 10
VTBL_AC_STOP = 11
VTBL_AC_GET_SERVICE = 14

# IAudioCaptureClient
VTBL_CC_GET_BUFFER = 3
VTBL_CC_RELEASE_BUFFER = 4
VTBL_CC_GET_NEXT_PACKET_SIZE = 5

# IMMDeviceEnumerator
VTBL_ENUM_GET_DEFAULT_AUDIO_ENDPOINT = 4

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# 100-ns-Einheiten (REFERENCE_TIME)
SECOND = 10_000_000
REFERENCE_TIME = ctypes.c_longlong


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        value = uuid.UUID(text)
        guid = cls()
        guid.Data1, guid.Data2, guid.Data3 = value.fields[0], value.fields[1], value.fields[2]
        guid.Data4[:] = list(value.bytes[8:])
        return guid


# WAVEFORMATEX/WAVEFORMATEXTENSIBLE werden mit `struct` gelesen, NICHT als
# ctypes.Structure. Grund: ctypes richtet Strukturen aus und schiebt hinter den
# 18 Byte von WAVEFORMATEX zwei Byte Padding ein - damit liegen wValidBitsPerSample,
# dwChannelMask und SubFormat um zwei Byte verschoben. Das ist eine stille Falle:
# `SubFormat.Data1` las dann den Kanal-Mask (3 fuer Stereo) statt des Formats, und
# ein Float-Format wurde als Integer gedeutet. Die Offsets auf dem Draht sind fest:
# 0 Tag / 2 Kanaele / 4 Rate / 8 AvgBytes / 12 BlockAlign / 14 Bits / 16 cbSize /
# 18 validBits / 20 Kanal-Mask / 24 SubFormat-GUID.
_WFX = "<HHIIHHH"
_OFF_SUBFORMAT = 24


def _method(obj: c_void_p, index: int, *argtypes):
    """One vtable call. `ctypes.HRESULT` as the return type raises on failure."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])


def _release(obj: c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[VTBL_RELEASE])(obj)


def com_init() -> None:
    """MTA wie in devices.init_thread_com - PortAudio laeuft ebenfalls damit."""
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)


def default_render_device() -> c_void_p:
    """IMMDevice of the default playback device (the one Spotify would use)."""
    ole32 = ctypes.windll.ole32
    enumerator = c_void_p()
    device = c_void_p()
    hr = ole32.CoCreateInstance(byref(_GUID.parse(CLSID_MM_DEVICE_ENUMERATOR)), None,
                               CLSCTX_ALL, byref(_GUID.parse(IID_IMM_DEVICE_ENUMERATOR)),
                               byref(enumerator))
    if hr != 0 or not enumerator:
        raise OSError(f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08x}")
    try:
        _method(enumerator, VTBL_ENUM_GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int,
                ctypes.c_int, POINTER(c_void_p))(enumerator, E_RENDER, E_CONSOLE,
                                                 byref(device))
        if not device:
            raise OSError("no default render endpoint")
        # GetDefaultAudioEndpoint hands out its own reference, so the enumerator can go.
        return device
    finally:
        _release(enumerator)


def device_name(device: c_void_p) -> str:
    text = ctypes.c_wchar_p()
    _method(device, VTBL_DEVICE_GET_ID, POINTER(ctypes.c_wchar_p))(device, byref(text))
    value = text.value or "?"
    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(text, c_void_p))
    return value.split("{")[-1].rstrip("}") if value.startswith("{") else value


class AudioFormat:
    """The mix format as far as the PoC needs it: rate, channels and sample width."""

    def __init__(self, rate: int, channels: int, bits: int, is_float: bool, block_align: int):
        self.rate, self.channels, self.bits = rate, channels, bits
        self.is_float, self.block_align = is_float, block_align

    def __str__(self) -> str:
        kind = "float" if self.is_float else "int"
        return f"{self.rate} Hz, {self.channels} ch, {self.bits}-bit {kind}, {self.block_align} B/Frame"


def read_format(pointer: int) -> AudioFormat:
    buf = ctypes.string_at(pointer, 40)
    tag, channels, rate, _avg, align, bits, _cb = struct.unpack_from(_WFX, buf, 0)
    is_float = tag == WAVE_FORMAT_IEEE_FLOAT
    if tag == WAVE_FORMAT_EXTENSIBLE:
        # Data1 des Subformat-GUID: 1 = PCM, 3 = IEEE float
        is_float = struct.unpack_from("<I", buf, _OFF_SUBFORMAT)[0] == WAVE_FORMAT_IEEE_FLOAT
    return AudioFormat(rate, channels, bits, is_float, align)


class LoopbackCapture:
    """Eine WASAPI-Loopback-Aufnahme des Standard-Wiedergabegeraets.

    Der einzige Weg, den Ton einer anderen App abzugreifen, ohne ein Kabel zu benutzen.
    Wird im Shared Mode geoeffnet: die laufende Wiedergabe bleibt unberuehrt, und der
    Rechner muss das Geraet nicht freigeben.
    """

    def __init__(self, device: c_void_p | None = None, buffer_ms: int = 100):
        self.device = device if device is not None else default_render_device()
        self._own_device = device is None
        self.buffer_ms = buffer_ms
        self.client = c_void_p()
        self.capture = c_void_p()
        self.format = None
        self.opened = False

    def open(self) -> AudioFormat:
        _method(self.device, VTBL_DEVICE_ACTIVATE, POINTER(_GUID), ctypes.c_uint,
                c_void_p, POINTER(c_void_p))(self.device, byref(_GUID.parse(IID_IAUDIO_CLIENT)),
                                             CLSCTX_ALL, None, byref(self.client))
        if not self.client:
            raise OSError("IAudioClient could not be activated")

        mix = c_void_p()
        _method(self.client, VTBL_AC_GET_MIX_FORMAT, POINTER(c_void_p))(self.client, byref(mix))
        if not mix:
            raise OSError("GetMixFormat returned nothing")
        try:
            self.format = read_format(mix.value)
            # LOOPBACK heisst: nicht selbst abspielen, sondern mitschneiden, was klingt.
            _method(self.client, VTBL_AC_INITIALIZE, ctypes.c_int, ctypes.c_uint,
                    REFERENCE_TIME, REFERENCE_TIME, c_void_p, c_void_p)(
                self.client, AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                self.buffer_ms * 10_000, 0, mix.value, None)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(mix)

        _method(self.client, VTBL_AC_GET_SERVICE, POINTER(_GUID), POINTER(c_void_p))(
            self.client, byref(_GUID.parse(IID_IAUDIO_CAPTURE_CLIENT)), byref(self.capture))
        if not self.capture:
            raise OSError("IAudioCaptureClient could not be obtained")
        self.opened = True
        return self.format

    def start(self) -> None:
        _method(self.client, VTBL_AC_START)(self.client)

    def stop(self) -> None:
        if self.client:
            _method(self.client, VTBL_AC_STOP)(self.client)

    def read(self) -> tuple[float, float, int, int]:
        """(rms, peak, frames, silence_flags) of everything available right now."""
        data = c_void_p()
        frames = ctypes.c_uint(0)
        flags = ctypes.c_uint(0)
        pos = ctypes.c_ulonglong(0)
        qpc = ctypes.c_ulonglong(0)
        total_frames = 0
        total_sq = 0.0
        peak = 0.0
        silenced = 0
        while True:
            hr = _method(self.capture, VTBL_CC_GET_BUFFER, POINTER(c_void_p),
                         POINTER(ctypes.c_uint), POINTER(ctypes.c_uint),
                         POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))(
                self.capture, byref(data), byref(frames), byref(flags), byref(pos), byref(qpc))
            if hr != 0 or frames.value == 0:
                break
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                silenced += 1
                total_frames += frames.value
            else:
                count = frames.value * self.format.channels
                raw = ctypes.string_at(data, count * (self.format.bits // 8))
                if self.format.is_float and self.format.bits == 32:
                    samples = struct.unpack(f"<{count}f", raw)
                elif self.format.bits == 16:
                    samples = [v / 32768.0 for v in struct.unpack(f"<{count}h", raw)]
                elif self.format.bits == 32:
                    samples = [v / 2147483648.0 for v in struct.unpack(f"<{count}i", raw)]
                else:
                    raise OSError(f"unexpected sample width {self.format.bits}")
                for value in samples:
                    total_sq += value * value
                    if abs(value) > peak:
                        peak = abs(value)
                total_frames += frames.value
            _method(self.capture, VTBL_CC_RELEASE_BUFFER, ctypes.c_uint)(self.capture, frames)
        rms = math.sqrt(total_sq / (total_frames * self.format.channels)) if total_frames else 0.0
        return rms, peak, total_frames, silenced

    def close(self) -> None:
        try:
            if self.opened:
                self.stop()
        except OSError:
            pass
        _release(self.capture)
        _release(self.client)
        if self._own_device:
            _release(self.device)


def capture_for(seconds: float, play=None) -> tuple[float, float, int, int]:
    """Neuen Client oeffnen, `seconds` sammeln, schliessen. Optional laeuft `play` dabei."""
    cap = LoopbackCapture()
    try:
        cap.open()
        cap.start()
        if play is not None:
            play()
        deadline = time.monotonic() + seconds
        best = (0.0, 0.0, 0, 0)
        while time.monotonic() < deadline:
            rms, peak, frames, silenced = cap.read()
            if rms > best[0]:
                best = (rms, peak, frames, silenced)
            elif frames and best[2] == 0:
                best = (rms, peak, frames, silenced)
            time.sleep(0.02)
        return best
    finally:
        cap.close()


# ---------------------------------------------------------------------------
# Die Pruefungen
# ---------------------------------------------------------------------------

def tone_player(seconds: float = 2.0, hz: float = 440.0, level: float = 0.25):
    """Spielt einen Ton ueber sounddevice auf dem Standardausgang."""
    import sounddevice as sd
    import numpy as np

    def play():
        rate = 48000
        t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
        wave = (level * np.sin(2 * math.pi * hz * t)).astype("float32")
        sd.play(wave, rate)

    return play


def main() -> int:
    if not sys.platform.startswith("win"):
        print("Nur Windows (WASAPI).")
        return 2

    print("=" * 78)
    print("WASAPI-Loopback ohne neue Abhaengigkeit - Machbarkeit")
    print("=" * 78)
    com_init()

    device = default_render_device()
    try:
        # --- 1. Format -----------------------------------------------------
        print()
        print("1) Standard-Wiedergabegeraet")
        try:
            print(f"   Endpoint-ID: {device_name(device)}")
        except OSError as exc:
            print(f"   Endpoint-ID: (nicht lesbar: {exc})")

        probe = LoopbackCapture(device)
        try:
            fmt = probe.open()
        except OSError as exc:
            print(f"   FEHLER - Loopback-Client liess sich nicht oeffnen: {exc}")
            print("   Damit ist der COM-Weg gestorben; dann bleibt nur eine Bibliothek.")
            return 1
        finally:
            probe.close()
        print(f"   Format:    {fmt}")
        print("   OK - Loopback-Client oeffnet im Shared Mode (kein Zusatzpaket)")

        # --- 2. Grundpegel -------------------------------------------------
        print()
        print("2) Grundpegel ohne eigenen Ton (2 s)")
        print("   Achtung: das ist kein Laborwert. Laeuft auf dem Rechner gerade etwas")
        print("   (Musik, Ruckus, ein Video), ist dieser Wert schon ein echter Pegel.")
        rms_before, peak_before, frames_before, silenced = capture_for(2.0)
        print(f"   rms={rms_before:.6f}  peak={peak_before:.6f}  "
              f"Frames/Abruf={frames_before}  stille Pakete={silenced}")
        if frames_before == 0:
            print("   HINWEIS - keine Pakete. Bei voelliger Stille liefert Windows manchmal")
            print("             nichts; kein Fehler, solange Schritt 3 ein Signal sieht.")
        else:
            print("   OK - der Client liefert Pakete")

        # --- 3. Echtes Signal ----------------------------------------------
        print()
        print("3) Mitschitt MIT eigenem Ton (440 Hz, 2 s) - der eigentliche Beweis")
        rms_tone, peak_tone, frames_tone, silenced_tone = capture_for(2.5, play=tone_player())
        print(f"   rms={rms_tone:.6f}  peak={peak_tone:.6f}  "
              f"Frames/Abruf={frames_tone}  stille Pakete={silenced_tone}")
        # Der eigene Ton ist deutlich lauter als alles, was sonst laeuft (0.25 gegen
        # typisch <0.06), also muss der Pegel klar steigen - sonst kommt der Ton nicht an.
        rise = 20 * math.log10(rms_tone / rms_before) if rms_before > 0 and rms_tone > 0 else None
        signal = rms_tone > max(rms_before * 2, 0.05)
        if rise is not None:
            print(f"   Pegel gegenueber Schritt 2: {rise:+.1f} dB")
        if signal:
            db = 20 * math.log10(rms_tone) if rms_tone > 0 else -999
            print(f"   OK - der eigene Ton kommt an ({db:.1f} dBFS RMS). Loopback traegt.")
        else:
            print("   FEHLER - der eigene Ton ist nicht zu sehen. Dann greift der Abgriff")
            print("            ein anderes Geraet ab als das, auf dem gespielt wird.")

        # --- 4. Stoert es sounddevice? -------------------------------------
        print()
        print("4) Stoert der Loopback-Client sounddevice?")
        try:
            import sounddevice as sd
            before = len(sd.query_devices())
            cap = LoopbackCapture()
            cap.open()
            cap.start()
            try:
                during = len(sd.query_devices())
                stream_ok = True
                try:
                    import numpy as np
                    out = sd.OutputStream(samplerate=48000, channels=2, dtype="float32")
                    out.start()
                    out.write(np.zeros((480, 2), dtype="float32"))
                    out.stop()
                    out.close()
                except Exception as exc:  # noqa: BLE001 - the report is the point
                    stream_ok = False
                    stream_error = exc
                rms_parallel, _, _, _ = cap.read()
            finally:
                cap.close()
            print(f"   Geraete vorher/nachher: {before} / {during}")
            if stream_ok:
                print("   OK - ein sounddevice-Ausgabestream lief parallel")
            else:
                print(f"   FEHLER - sounddevice-Stream scheiterte: {stream_error}")
            print(f"   Nebenbei gemessen: rms={rms_parallel:.6f} (waehrend der eigene Stream lief)")
        except Exception as exc:  # noqa: BLE001
            print(f"   FEHLER - {type(exc).__name__}: {exc}")

        # --- 5. Zweiter Client ---------------------------------------------
        print()
        print("5) Ein zweiter Mitschitt parallel?")
        first = LoopbackCapture()
        second = LoopbackCapture()
        try:
            first.open()
            first.start()
            try:
                second.open()
                second.start()
                print("   OK - zwei Loopback-Clients laufen gleichzeitig")
            except OSError as exc:
                print(f"   HINWEIS - ein zweiter Client scheitert: {exc}")
        finally:
            second.close()
            first.close()
    finally:
        _release(device)

    print()
    print("=" * 78)
    print("Fazit")
    print("  Als richtig gilt: 1 oeffnet, 3 hebt den Pegel klar, 4 stoert sounddevice nicht.")
    print("  Dann braucht der Musik-Bus KEINE neue Abhaengigkeit - soundcard und")
    print("  PyAudioWPatch entfallen. Es bleibt ctypes/COM nach dem Muster von")
    print("  soundboard/defaultdevice.py (dieselben zwei vtable-Aufrufe plus IAudioClient).")
    print("  Offen fuer F3 mit echten Geraeten: Exclusive Mode und Geraetewechsel.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
