"""Musik-Bus: der Ton anderer Apps (Spotify, Browser) wird per WASAPI-Loopback
mitgeschnitten und in den Mixer gegeben, damit Discord/CS ihn hoeren.

Kern-Anbindung (Spec "musik-bus-kern", 2026-09-27): `MusicBusService` meldet die
Befehle `SetMusicBus`/`SetMusicBusGain` am Kern, haelt den State-Teil `musicbus`
(volatile) und die Config (`musicbus_enabled`/`musicbus_gain`) und wird in
`create_core(..., musicbus_factory=...)` montiert - Tests liefern einen Fake,
im gruenen Lauf laeuft nie eine echte WASAPI-Aufnahme. Der Mischpfad haengt ueber
einen `on_block`-Haken an der SinkGroup (Musik im Sound-Zweig, `sinkgroup.py`).
`MusicBus` selbst bleibt unhaengig davon nutzbar:

    bus = MusicBus()
    bus.start()          # eigener Thread mit COM, greift das Standard-
                         # Wiedergabegeraet ab (Shared Mode, die Wiedergabe
                         # laeuft unberuehrt weiter)
    block = bus.pull(480)   # (480, 2) float32 in -1..1, wie virtualmic.MixSource
    bus.close()

`pull` hat dieselbe Gestalt wie `virtualmic.MixSource.pull` (frames rein, Block
raus) und laeuft auf dem PortAudio-Thread des Mischers: er ruehrt sich nur an
seinem eigenen Puffer, gibt bei Leere **Stille** statt None zurueck und ist
dauerhaft gefuellt, solange der Bus laeuft. Damit laesst er sich dort einhaengen,
wo heute schon Sounds summiert werden (`virtualmic.mix_blocks`).

Technik (alles aus `tests/test_loopback_manual.py`, live gemessen am 2026-09-27:
48000 Hz / 2 ch / 32-bit float, zwei Loopback-Clients parallel, `sounddevice`
daneben):

- **Reines ctypes/COM**, keine neue Abhaengigkeit (`soundcard`/`PyAudioWPatch`
  sind bewusst verworfen). Muster wie `soundboard/defaultdevice.py`.
- **WAVEFORMATEX nur ueber feste Offsets lesen** (0 Tag / 2 Kanaele / 4 Rate /
  8 AvgBytes / 12 BlockAlign / 14 Bits / 16 cbSize / 18 validBits /
  20 Kanal-Mask / 24 SubFormat-GUID). Eine `ctypes.Structure` richtet aus und
  schiebt hinter den 18 Byte zwei Byte Padding ein - dann liest `SubFormat.Data1`
  den Kanal-Mask (3 fuer Stereo) statt des Formats, und ein Float-Format wird
  still als Integer gedeutet. Diese Falle kostete schon einen Messlauf; der
  Regressionstest dafuer steht in `tests/test_musicbus_logic.py`.
- **Der Abgriff haengt an der Endpoint-ID vom Oeffnen.** Wechselt das Windows-
  Standard-Geraet, ist der Client tot - `MusicBus.rebuild()` baut ihn neu auf
  (dieselbe Aufgabe, die `defaultdevice.DefaultDeviceWatcher` fuer den
  Kopfhoerer loest). Exclusive Mode eines anderen Programms ist ungeprueft
  (F3).
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
import uuid
from ctypes import POINTER, byref, c_void_p, wintypes
from collections import deque
from typing import Callable

import numpy as np

from . import virtualmic
from .config import clamp_gain
from .protocol import SetMusicBus, SetMusicBusGain

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# COM-Grundlagen (dieselbe minimale Bauart wie soundboard/defaultdevice.py
# und tests/test_loopback_manual.py)
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
VTBL_AC_GET_MIX_FORMAT = 8
VTBL_AC_START = 10
VTBL_AC_STOP = 11
VTBL_AC_GET_SERVICE = 14

# IAudioCaptureClient
VTBL_CC_GET_BUFFER = 3
VTBL_CC_RELEASE_BUFFER = 4

# IMMDeviceEnumerator
VTBL_ENUM_GET_DEFAULT_AUDIO_ENDPOINT = 4

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

SECOND = 10_000_000  # 100-ns-Einheiten (REFERENCE_TIME)
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


def _method(obj: c_void_p, index: int, *argtypes):
    """One vtable call. `ctypes.HRESULT` as the return type raises on failure."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])


def _release(obj: c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[VTBL_RELEASE])(obj)


def com_init() -> None:
    """MTA wie in `devices.init_thread_com` - PortAudio laeuft ebenfalls damit."""
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)


def default_render_device() -> c_void_p:
    """IMMDevice des Standard-Wiedergabegeraets (dort klingt die Musik)."""
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
        # GetDefaultAudioEndpoint gibt eine eigene Referenz heraus, der
        # Enumerator darf weg.
        return device
    finally:
        _release(enumerator)


def device_name(device: c_void_p) -> str:
    text = ctypes.c_wchar_p()
    _method(device, VTBL_DEVICE_GET_ID, POINTER(ctypes.c_wchar_p))(device, byref(text))
    value = text.value or "?"
    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(text, c_void_p))
    return value.split("{")[-1].rstrip("}") if value.startswith("{") else value


# ---------------------------------------------------------------------------
# Format und Samples (rein, ohne COM - deshalb hart testbar)
# ---------------------------------------------------------------------------

_WFX = "<HHIIHHH"
_OFF_VALID_BITS = 18
_OFF_CHANNEL_MASK = 20
_OFF_SUBFORMAT = 24


class AudioFormat:
    """Das Mix-Format, so weit der Bus es braucht: Rate, Kanaele, Bittiefe."""

    def __init__(self, rate: int, channels: int, bits: int, is_float: bool, block_align: int):
        self.rate, self.channels, self.bits = rate, channels, bits
        self.is_float, self.block_align = is_float, block_align

    def __repr__(self) -> str:
        kind = "float" if self.is_float else "int"
        return (f"AudioFormat({self.rate} Hz, {self.channels} ch, "
                f"{self.bits}-bit {kind}, {self.block_align} B/Frame)")

    def __eq__(self, other) -> bool:
        return isinstance(other, AudioFormat) and repr(self) == repr(other)


def read_waveformat(buf: bytes) -> AudioFormat:
    """WAVEFORMATEX(-EXTENSIBLE) aus Rohtext - feste Offsets, siehe Modul-Doku.

    `ctypes.Structure` waere hier falsch: Ausrichtung schiebt hinter den 18 Byte
    von WAVEFORMATEX zwei Byte Padding ein, und `SubFormat.Data1` laese den
    Kanal-Mask (3 fuer Stereo) statt des Formats. Regressionstest:
    `tests/test_musicbus_logic.py::test_float_extensible_survives_the_padding_trap`.
    """
    if len(buf) < _OFF_SUBFORMAT + 4:
        raise ValueError(f"waveformat buffer too short: {len(buf)} bytes")
    tag, channels, rate, _avg, align, bits, _cb = struct.unpack_from(_WFX, buf, 0)
    is_float = tag == WAVE_FORMAT_IEEE_FLOAT
    if tag == WAVE_FORMAT_EXTENSIBLE:
        # Data1 des Subformat-GUID: 1 = PCM, 3 = IEEE float
        is_float = struct.unpack_from("<I", buf, _OFF_SUBFORMAT)[0] == WAVE_FORMAT_IEEE_FLOAT
    elif tag != WAVE_FORMAT_PCM:
        raise ValueError(f"unexpected wave format tag 0x{tag:04x}")
    return AudioFormat(rate, channels, bits, is_float, align)


def decode_frames(raw: bytes, fmt: AudioFormat) -> np.ndarray:
    """Rohtext -> float32 (Frames x Kanaele), Werte in -1..1."""
    if fmt.is_float and fmt.bits == 32:
        out = np.frombuffer(raw, dtype="<f4")
    elif fmt.bits == 16:
        out = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif fmt.bits == 32:
        out = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unexpected sample width {fmt.bits}")
    return out.reshape(-1, fmt.channels)


def resample_linear(block: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Lineares Nachtasten - gut genug fuer Musik im Mixer, ohne neue Abhaengigkeit.

    Gleiche Rate gibt denselben Block zurueck (keine Kopie, keine Arbeit).
    """
    if from_rate == to_rate or len(block) == 0:
        return block
    out_frames = max(1, round(len(block) * to_rate / from_rate))
    old_x = np.arange(len(block), dtype=np.float64)
    new_x = np.arange(out_frames, dtype=np.float64) * (from_rate / to_rate)
    out = np.empty((out_frames, block.shape[1]), dtype=np.float32)
    for ch in range(block.shape[1]):
        out[:, ch] = np.interp(new_x, old_x, block[:, ch].astype(np.float64)).astype(np.float32)
    return out


# ---------------------------------------------------------------------------
# Loopback-Aufnahme
# ---------------------------------------------------------------------------

class LoopbackCapture:
    """WASAPI-Loopback des Standard-Wiedergabegeraets, Shared Mode.

    Der Shared Mode ist wichtig: die laufende Wiedergabe bleibt unberuehrt und
    der Rechner muss das Geraet nicht freigeben. `read_blocks` liefert alles, was
    gerade bereitliegt (leerer Block bei Stille/Leere).
    """

    def __init__(self, device: c_void_p | None = None, buffer_ms: int = 100):
        self.device = device if device is not None else default_render_device()
        self._own_device = device is None
        self.buffer_ms = buffer_ms
        self.client = c_void_p()
        self.capture = c_void_p()
        self.format: AudioFormat | None = None
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
            self.format = read_waveformat(ctypes.string_at(mix.value, 40))
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

    def read_blocks(self) -> np.ndarray:
        """Alles verfuegbare als float32 (Frames x Kanaele); leerer Block bei Leere."""
        chunks: list[np.ndarray] = []
        data = c_void_p()
        frames = ctypes.c_uint(0)
        flags = ctypes.c_uint(0)
        pos = ctypes.c_ulonglong(0)
        qpc = ctypes.c_ulonglong(0)
        while True:
            hr = _method(self.capture, VTBL_CC_GET_BUFFER, POINTER(c_void_p),
                         POINTER(ctypes.c_uint), POINTER(ctypes.c_uint),
                         POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))(
                self.capture, byref(data), byref(frames), byref(flags), byref(pos), byref(qpc))
            if hr != 0 or frames.value == 0:
                break
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                chunks.append(np.zeros((frames.value, self.format.channels), dtype=np.float32))
            else:
                count = frames.value * self.format.channels
                raw = ctypes.string_at(data, count * (self.format.bits // 8))
                chunks.append(decode_frames(raw, self.format))
            _method(self.capture, VTBL_CC_RELEASE_BUFFER, ctypes.c_uint)(self.capture, frames)
        if not chunks:
            return np.zeros((0, self.format.channels if self.format else 1), dtype=np.float32)
        return np.vstack(chunks)

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
        self.opened = False


# ---------------------------------------------------------------------------
# Der Bus
# ---------------------------------------------------------------------------

class MusicBus:
    """Laufender Abgriff mit kleinem Puffer, `pull`-Schnittstelle wie MixSource.

    Der Puffer faengt Taktunterschiede zwischen WASAPI und PortAudio ab. Faellt
    der Bus zurueck (mehr als `max_blocks` Blöcke), wirft er die aeltesten weg -
    dieselbe Regel wie `virtualmic.MIC_QUEUE_BLOCKS`: lieber ein Sprung als
    aufwachsende Verzoegerung.
    """

    def __init__(self, *, gain: float = 1.0,
                 block_frames: int = virtualmic.BLOCKSIZE,
                 target_rate: int = virtualmic.TARGET_SAMPLERATE,
                 max_blocks: int = 50,
                 buffer_ms: int = 100,
                 capture_factory: Callable[[], LoopbackCapture] = LoopbackCapture):
        self._gain = float(gain)
        self._block_frames = block_frames
        self._target_rate = target_rate
        self._max_blocks = max_blocks
        self._capture_factory = capture_factory
        self._buffer_ms = buffer_ms
        self._ring: deque[np.ndarray] = deque()
        self._partial: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: LoopbackCapture | None = None
        self.format: AudioFormat | None = None
        self.error: BaseException | None = None
        # Gerufen aus dem Abgriff-Faden, wenn er mit einem Fehler endet - der
        # Service meldet das als Notice, "nie still sterben" (Spec §2).
        self.on_error: Callable[[BaseException], None] | None = None
        # Mischpfad (Spec §4): jeder normalisierte Block geht zusätzlich zum
        # Ringpuffer hier raus - `sinkgroup.SinkGroup.distribute_music`.
        self.on_block: Callable[[np.ndarray], None] | None = None

    # ---- Lebenszyklus (Aufrufer-Thread) ----

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def gain(self) -> float:
        return self._gain

    def set_gain(self, gain: float) -> None:
        self._gain = float(gain)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, name="ruckus-musicbus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def close(self) -> None:
        self.stop()
        self._drain()

    def rebuild(self) -> None:
        """Nach einem Standard-Geraetewechsel: der Abgriff haengt an der
        Endpoint-ID vom Oeffnen, also neu aufbauen (Modul-Doku)."""
        was_running = self.running
        self.stop()
        if was_running:
            self.start()

    # ---- Abgriff-Thread ----

    def _run(self) -> None:
        try:
            com_init()
        except OSError:
            pass  # ohne COM laeuft nichts; der Fehler unten sichtbar machen
        try:
            self._capture = self._capture_factory()
            if hasattr(self._capture, "buffer_ms"):
                self._capture.buffer_ms = self._buffer_ms
            self.format = self._capture.open()
            self._capture.start()
            while not self._stop.is_set():
                self._pump_once()
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001 - der Thread darf nicht still sterben
            self.error = exc
            hook = self.on_error
            if hook is not None:
                try:
                    hook(exc)
                except Exception:
                    log.exception("musicbus on_error hook failed")
        finally:
            capture, self._capture = self._capture, None
            if capture is not None:
                try:
                    capture.close()
                except OSError:
                    pass

    def _pump_once(self) -> None:
        """Ein Lesezyklus: abgreifen, normalisieren, in den Puffer. Trennbar
        vom Thread, damit die Logik ohne Geraete testbar ist."""
        capture = self._capture
        if capture is None:
            return
        data = capture.read_blocks()
        if len(data):
            self._offer(data)

    def _offer(self, block: np.ndarray) -> None:
        """Block auf 48-kHz-Stereo-Float32 bringen und einreihen (Gain inkl.)."""
        block = virtualmic.to_stereo(np.asarray(block, dtype=np.float32))
        if self._capture is not None and self._capture.format is not None:
            block = resample_linear(block, self._capture.format.rate, self._target_rate)
        if self._gain != 1.0:
            block = block * self._gain
        hook = self.on_block
        if hook is not None:
            hook(block)  # Muster Mic-Verteilung: eine Queue je Ziel, niemand stiehlt
        with self._lock:
            self._ring.append(block)
            while len(self._ring) > self._max_blocks:
                self._ring.popleft()

    def _drain(self) -> None:
        with self._lock:
            self._ring.clear()
            self._partial = None

    # ---- Misch-Thread ----

    def pull(self, frames: int) -> np.ndarray:
        """(frames, 2) float32. Bei Leere Stille - der Mischstrom reisst nie ab."""
        out = np.zeros((frames, virtualmic.TARGET_CHANNELS), dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames:
                if self._partial is not None and len(self._partial):
                    take = min(frames - filled, len(self._partial))
                    out[filled:filled + take] = self._partial[:take]
                    filled += take
                    self._partial = self._partial[take:]
                    continue
                if not self._ring:
                    break
                self._partial = self._ring.popleft()
        return out


# ---------------------------------------------------------------------------
# Kern-Anbindung
# ---------------------------------------------------------------------------

MUSICBUS_ERROR = "Musik-Bus abgebrochen – Details stehen in ruckus.log."


class MusicBusService:
    """Befehle, Zustandsteil, Config und Lebenszyklus fuer den Musik-Bus.

    Der Bus laeuft in eigenem Faden mit eigenem COM (dokumentierte Abweichung,
    Spec §3): er fasst kein PortAudio an und laeuft sauber neben dem
    Geraete-Thread. Der Mischpfad haengt ueber `attach_sink` an der jeweiligen
    SinkGroup - bei JEDEM Neuaufbau (Geraetewechsel, Pruefung) muss der Haken
    erneut gesetzt werden, sonst verstummt die Musik still.
    """

    def __init__(self, core, *, factory=None):
        self._core = core
        self._error = ""
        self.bus = (factory or MusicBus)(gain=clamp_gain(core.store.data.get("musicbus_gain")))
        self.bus.on_error = self._bus_failed
        core.handle(SetMusicBus, self.set_music_bus)
        core.handle(SetMusicBusGain, self.set_music_bus_gain)
        core.add_state("musicbus", self.snapshot, group="volatile")
        core.on_start(self.start)
        core.on_shutdown(self.shutdown)

    # ---- Befehle (Kern-Thread) ----

    def set_music_bus(self, cmd: SetMusicBus) -> None:
        self._core.store.data["musicbus_enabled"] = bool(cmd.enabled)
        if cmd.enabled:
            self._error = ""
            self.bus.start()
        else:
            self.bus.stop()
        self._core.store.save_now()
        self._core.changed("volatile")

    def set_music_bus_gain(self, cmd: SetMusicBusGain) -> None:
        gain = clamp_gain(cmd.gain)
        self._core.store.data["musicbus_gain"] = gain
        self.bus.set_gain(gain)
        self._core.store.save_soon()
        self._core.changed("volatile")

    # ---- Lebenszyklus (Kern-Thread) ----

    def start(self) -> None:
        self.bus.set_gain(clamp_gain(self._core.store.data.get("musicbus_gain")))
        if self._core.store.data.get("musicbus_enabled"):
            self._error = ""
            self.bus.start()

    def shutdown(self) -> None:
        self.bus.close()

    def attach_sink(self, sink) -> None:
        """Der Mischpfad-Haken: `sink.distribute_music` bekommt jeden Bus-Block
        (aufrufen aus dem Abgriff-Faden). `sink=None` haengt ab."""
        self.bus.on_block = getattr(sink, "distribute_music", None) if sink is not None else None

    # ---- Fehler und Zustand ----

    def _bus_failed(self, exc: BaseException) -> None:  # Abgriff-Faden
        self._core.executor.submit(self._report_failure, exc)

    def _report_failure(self, exc: BaseException) -> None:  # Kern-Thread
        self._error = str(exc) or type(exc).__name__
        self._core.notice(MUSICBUS_ERROR, "error")
        self._core.changed("volatile")

    def snapshot(self) -> dict:
        """Reiner Lesevorgang: kein Netz, keine Datei, kein Geraet."""
        live = getattr(self.bus, "error", None)
        error = (str(live) or type(live).__name__) if live else self._error
        return {
            "enabled": bool(self._core.store.data.get("musicbus_enabled")),
            "running": bool(self.bus.running),
            "error": error,
            "gain": clamp_gain(self._core.store.data.get("musicbus_gain")),
        }
