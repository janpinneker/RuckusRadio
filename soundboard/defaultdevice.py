"""Welches Wiedergabegeraet ist gerade Windows-Standard? Per COM ueber ctypes.

PortAudio kennt nur den Stand beim Start. Damit die Kopfhoerer dem Windows-Standard
live folgen, fragt die GUI alle zwei Sekunden die Endpoint-ID ab und baut bei einem
Wechsel den Mixer neu auf. Keine neue Abhaengigkeit: nur die zwei benoetigten
vtable-Aufrufe (IMMDeviceEnumerator::GetDefaultAudioEndpoint, IMMDevice::GetId).
Laeuft auf dem Tk-Thread.
"""

from __future__ import annotations

import ctypes
import logging
import uuid
from ctypes import POINTER, byref, c_void_p, wintypes
from typing import Callable

log = logging.getLogger(__name__)

CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
CLSCTX_ALL = 23
COINIT_APARTMENTTHREADED = 0x2
E_RENDER = 0
E_CONSOLE = 0  # the device Windows Sound settings calls "Standardgeraet"
VTBL_RELEASE = 2
VTBL_GET_DEFAULT_AUDIO_ENDPOINT = 4
VTBL_GET_ID = 5


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
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])


def _release(obj: c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[VTBL_RELEASE])(obj)


def default_render_id() -> str | None:
    """Endpoint ID of the default playback device, or None when COM is unavailable."""
    try:
        ole32 = ctypes.windll.ole32
    except AttributeError:
        return None
    # COM stays initialised on the Tk thread for the app's lifetime, so there is intentionally no CoUninitialize.
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)  # S_FALSE / changed mode: fine
    enumerator = c_void_p()
    device = c_void_p()
    try:
        hr = ole32.CoCreateInstance(byref(_GUID.parse(CLSID_MM_DEVICE_ENUMERATOR)), None,
                                    CLSCTX_ALL, byref(_GUID.parse(IID_IMM_DEVICE_ENUMERATOR)),
                                    byref(enumerator))
        if hr != 0 or not enumerator:
            return None
        _method(enumerator, VTBL_GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int, ctypes.c_int,
                POINTER(c_void_p))(enumerator, E_RENDER, E_CONSOLE, byref(device))
        if not device:
            return None
        text = ctypes.c_wchar_p()
        _method(device, VTBL_GET_ID, POINTER(ctypes.c_wchar_p))(device, byref(text))
        value = text.value
        ole32.CoTaskMemFree(ctypes.cast(text, c_void_p))
        return value
    except (OSError, ValueError):
        log.debug("default render endpoint unavailable", exc_info=True)
        return None
    finally:
        _release(device)
        _release(enumerator)


class DefaultDeviceWatcher:
    """Remembers the last endpoint ID. A change stays pending until the caller has
    taken it over with done() - it may have to wait until nothing plays.

    observe() takes an ID someone else read (the core reads on the device thread and
    decides on the core thread); poll() reads through `read_id` itself."""

    def __init__(self, read_id: Callable[[], str | None] | None = None,
                 initial: str | None = None):
        self._read = read_id
        self._last = read_id() if read_id is not None else initial
        self.pending = False

    def observe(self, current: str | None) -> bool:
        if current is not None and current != self._last:
            self._last = current
            self.pending = True
        return self.pending

    def poll(self) -> bool:
        if self._read is None:
            raise RuntimeError("poll() needs read_id; use observe() instead")
        return self.observe(self._read())

    def done(self) -> None:
        self.pending = False
