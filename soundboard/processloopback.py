"""Process-Loopback: nur den Ton eines Prozessbaums abgreifen (Spec
"audio-routing-spotify" §3). Der Musik-Bus greift damit Spotify ab statt des
ganzen Standard-Wiedergabegeraets - CS-, Spiel-, Discord- und Systemton kommen
nie ins Kabel.

Windows ab 10/2004: `ActivateAudioInterfaceAsync(VAD\\Process_Loopback, IAudioClient,
PROPVARIANT(VT_BLOB -> AUDIOCLIENT_ACTIVATION_PARAMS), handler)`. Die Aktivierung
ist asynchron; der Handler ist ein handgebautes COM-Objekt (IUnknown +
IActivateAudioInterfaceCompletionHandler + IAgileObject), das nur ein Ereignis setzt.
`GetMixFormat` liefert hier nichts (Microsoft-Beispiel "ApplicationLoopback"),
deshalb ein festes Format: 48 kHz / Stereo / Float32 = Mischformat des Kerns.

Reines ctypes, keine neue Abhaengigkeit. Lesen/Starten/Stoppen erbt
`ProcessLoopbackCapture` von `musicbus.LoopbackCapture`.
"""

from __future__ import annotations

import ctypes
import struct
import threading
from ctypes import POINTER, byref, c_void_p, wintypes

from .musicbus import (AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                       IID_IAUDIO_CAPTURE_CLIENT, IID_IAUDIO_CLIENT, REFERENCE_TIME,
                       VTBL_AC_GET_SERVICE, VTBL_AC_INITIALIZE, WAVE_FORMAT_IEEE_FLOAT,
                       AudioFormat, LoopbackCapture, _GUID, _method, _release)

SPOTIFY_EXE = "Spotify.exe"
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"
IID_IACTIVATE_COMPLETION_HANDLER = "41D949AB-9862-444A-80F6-C261334DA5EB"
IID_IAGILE_OBJECT = "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
VT_BLOB = 65
VTBL_QUERY_INTERFACE = 0
VTBL_AC_SET_EVENT_HANDLE = 13
VTBL_OP_GET_ACTIVATE_RESULT = 3

S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002 als vorzeichenbehaftetes HRESULT
ACTIVATE_TIMEOUT_S = 5.0

FIXED_FORMAT = AudioFormat(48000, 2, 32, True, 8)

TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = c_void_p(-1).value

# Jeder Handler bleibt fuer die Lebenszeit des Prozesses am Leben: Windows kann
# ActivateCompleted/Release auch nach der Rueckkehr aus activate_process_loopback
# noch rufen (Erfolg oder Zeitueberschreitung), waere der Handler dann schon vom
# Python-GC eingesammelt, waere das ein Use-after-free. Aktivierungen sind selten
# (nur wenn Spotify gefunden wird), der Leck sind ein paar hundert Byte je Aufruf.
_KEEP_ALIVE: list = []


# ---------------------------------------------------------------------------
# Bytes (rein, ohne COM - hart testbar)
# ---------------------------------------------------------------------------

def activation_params(pid: int) -> bytes:
    """AUDIOCLIENT_ACTIVATION_PARAMS: Typ, TargetProcessId, Modus - je 4 Byte."""
    return struct.pack("<iIi", AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, pid,
                       PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE)


def float_waveformat(fmt: AudioFormat = FIXED_FORMAT) -> bytes:
    """WAVEFORMATEX (18 Byte, cbSize 0) fuer ein Float-Format."""
    return struct.pack("<HHIIHHH", WAVE_FORMAT_IEEE_FLOAT, fmt.channels, fmt.rate,
                       fmt.rate * fmt.block_align, fmt.block_align, fmt.bits, 0)


def pick_root_pid(entries: list[tuple[int, int, str]], exe: str = SPOTIFY_EXE) -> int | None:
    """Der oberste Prozess mit diesem Namen: sein Elternprozess heisst nicht so.
    `INCLUDE_TARGET_PROCESS_TREE` nimmt die Kinder mit - bei Spotify kommt der Ton
    aus einem Kind. Final-Fix F4: mehrere Wurzeln koennen vorkommen (z. B. ein
    verwaister Rest-Prozess vom letzten Neustart neben der echten, laufenden
    Instanz) - die Wurzel mit den meisten DIREKTEN Spotify.exe-Kindern gewinnt,
    weil dort der Ton herkommt; bei Gleichstand die kleinste PID, damit die Wahl
    stabil bleibt."""
    name = exe.casefold()
    pids = {pid for pid, _parent, e in entries if e.casefold() == name}
    roots = [pid for pid, parent, e in entries if e.casefold() == name and parent not in pids]
    if not roots:
        return None

    def child_count(root: int) -> int:
        return sum(1 for _pid, parent, e in entries if parent == root and e.casefold() == name)

    return min(roots, key=lambda root: (-child_count(root), root))


# ---------------------------------------------------------------------------
# Prozessliste (kernel32 Toolhelp)
# ---------------------------------------------------------------------------

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_K32 = None


def _k32():
    """Eigene kernel32-Instanz mit Signaturen (nicht das geteilte ctypes.windll)."""
    global _K32
    if _K32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        for name in ("Process32FirstW", "Process32NextW"):
            fn = getattr(k32, name)
            fn.restype = wintypes.BOOL
            fn.argtypes = [wintypes.HANDLE, POINTER(_PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        _K32 = k32
    return _K32


def process_entries() -> list[tuple[int, int, str]]:
    """(pid, parent_pid, exe) aller laufenden Prozesse."""
    k32 = _k32()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        out: list[tuple[int, int, str]] = []
        ok = k32.Process32FirstW(snap, byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k32.Process32NextW(snap, byref(entry))
        return out
    finally:
        k32.CloseHandle(snap)


def find_spotify_pid() -> int | None:
    return pick_root_pid(process_entries())


# ---------------------------------------------------------------------------
# Asynchrone Aktivierung
# ---------------------------------------------------------------------------

class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD), ("wReserved3", wintypes.WORD),
                ("blob", _BLOB)]


_ACTIVATE_FN = None


def _activate_fn():
    """`Mmdevapi!ActivateAudioInterfaceAsync` mit Signatur, einmal geladen (wie `_k32()`)."""
    global _ACTIVATE_FN
    if _ACTIVATE_FN is None:
        fn = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        fn.restype = ctypes.HRESULT
        fn.argtypes = [wintypes.LPCWSTR, POINTER(_GUID), POINTER(_PROPVARIANT), c_void_p,
                      POINTER(c_void_p)]
        _ACTIVATE_FN = fn
    return _ACTIVATE_FN


_QI_T = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(_GUID), POINTER(c_void_p))
_REF_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_DONE_T = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_void_p)


class _Vtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI_T), ("AddRef", _REF_T), ("Release", _REF_T),
                ("ActivateCompleted", _DONE_T)]


class _HandlerObj(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_Vtbl))]


class _CompletionHandler:
    """Statisches COM-Objekt: Referenzzaehlung ist Schein (immer 1), die Python-
    Referenzen halten vtable und Callbacks am Leben. IAgileObject sagt COM, dass
    der Aufruf aus einem fremden Faden kommen darf."""

    def __init__(self):
        self.done = threading.Event()
        self._iids = {bytes(_GUID.parse(i)) for i in
                      (IID_IUNKNOWN, IID_IACTIVATE_COMPLETION_HANDLER, IID_IAGILE_OBJECT)}
        self._callbacks = (_QI_T(self._query), _REF_T(lambda _this: 1),
                           _REF_T(lambda _this: 1), _DONE_T(self._completed))
        self._vtbl = _Vtbl(*self._callbacks)
        self._obj = _HandlerObj(ctypes.pointer(self._vtbl))
        self.pointer = c_void_p(ctypes.addressof(self._obj))

    def _query(self, this, riid, ppv):
        if bytes(riid.contents) in self._iids:
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _completed(self, _this, _operation):
        self.done.set()
        return S_OK


def activate_process_loopback(pid: int, timeout_s: float = ACTIVATE_TIMEOUT_S) -> c_void_p:
    """IAudioClient fuer den Prozessbaum `pid`. Wirft OSError/TimeoutError."""
    params = ctypes.create_string_buffer(activation_params(pid), 12)
    prop = _PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = 12
    prop.blob.pBlobData = ctypes.addressof(params)
    handler = _CompletionHandler()
    _KEEP_ALIVE.append(handler)
    operation = c_void_p()
    _activate_fn()(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(_GUID.parse(IID_IAUDIO_CLIENT)),
                  byref(prop), handler.pointer, byref(operation))
    try:
        if not handler.done.wait(timeout_s):
            raise TimeoutError(f"process loopback activation for pid {pid} did not complete")
        result = ctypes.c_long(0)
        unknown = c_void_p()
        _method(operation, VTBL_OP_GET_ACTIVATE_RESULT, POINTER(ctypes.c_long),
                POINTER(c_void_p))(operation, byref(result), byref(unknown))
        if result.value != 0 or not unknown:
            _release(unknown)
            raise OSError(f"process loopback activation failed: 0x{result.value & 0xFFFFFFFF:08x}")
        client = c_void_p()
        try:
            _method(unknown, VTBL_QUERY_INTERFACE, POINTER(_GUID), POINTER(c_void_p))(
                unknown, byref(_GUID.parse(IID_IAUDIO_CLIENT)), byref(client))
        finally:
            _release(unknown)
        return client
    finally:
        _release(operation)


# ---------------------------------------------------------------------------
# Aufnahme
# ---------------------------------------------------------------------------

class ProcessLoopbackCapture(LoopbackCapture):
    """Process-Loopback eines Prozessbaums, Shared Mode, festes Format.
    Lesen/Starten/Stoppen wie `LoopbackCapture` (gleiche Felder client/capture/format)."""

    def __init__(self, pid: int, buffer_ms: int = 100):
        # bewusst ohne super().__init__: kein Standardgeraet oeffnen
        self.pid = pid
        self.device = None
        self._own_device = False
        self.buffer_ms = buffer_ms
        self.client = c_void_p()
        self.capture = c_void_p()
        self.format: AudioFormat | None = None
        self.opened = False
        self._event = None

    def open(self) -> AudioFormat:
        if self.opened:
            return self.format
        self.client = activate_process_loopback(self.pid)
        wfx = ctypes.create_string_buffer(float_waveformat())
        _method(self.client, VTBL_AC_INITIALIZE, ctypes.c_int, ctypes.c_uint,
                REFERENCE_TIME, REFERENCE_TIME, c_void_p, c_void_p)(
            self.client, AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            self.buffer_ms * 10_000, 0, ctypes.addressof(wfx), None)
        # EVENTCALLBACK verlangt ein Ereignis vor Start(); gelesen wird trotzdem im
        # Takt des Bus-Fadens (musicbus.MusicBus._pump_once).
        self._event = _k32().CreateEventW(None, False, False, None)
        if not self._event:
            raise ctypes.WinError(ctypes.get_last_error())
        _method(self.client, VTBL_AC_SET_EVENT_HANDLE, c_void_p)(self.client, self._event)
        _method(self.client, VTBL_AC_GET_SERVICE, POINTER(_GUID), POINTER(c_void_p))(
            self.client, byref(_GUID.parse(IID_IAUDIO_CAPTURE_CLIENT)), byref(self.capture))
        if not self.capture:
            raise OSError("IAudioCaptureClient could not be obtained")
        self.format = FIXED_FORMAT
        self.opened = True
        return self.format

    def close(self) -> None:
        super().close()
        self.client = c_void_p()
        self.capture = c_void_p()
        event, self._event = self._event, None
        if event:
            _k32().CloseHandle(event)
