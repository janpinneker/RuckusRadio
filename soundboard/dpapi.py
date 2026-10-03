"""Windows DPAPI über ctypes: verschlüsselt Bytes für das angemeldete Windows-Konto.

Nur Standardbibliothek. Ein anderes Windows-Konto oder eine Kopie der Datei auf einem
anderen Rechner kann den Inhalt nicht lesen. Gegen Programme, die als derselbe Nutzer
laufen, schützt DPAPI nicht vollständig, aber der Klartext liegt nicht mehr offen auf
der Platte. Fehler kommen als OSError zurück, damit `store` sie wie Dateifehler behandelt.
"""

from __future__ import annotations

import ctypes
import sys

CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Fixed per app: a blob of another program cannot be decrypted with our call, and ours
# not with a bare CryptUnprotectData from elsewhere.
_ENTROPY = b"RuckusRadio secrets v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _api():
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _call(func_name: str, data: bytes) -> bytes:
    crypt32, kernel32 = _api()
    data_in, keep_in = _blob(data)
    entropy, keep_entropy = _blob(_ENTROPY)
    data_out = _Blob()
    func = getattr(crypt32, func_name)
    ok = func(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(data_out.pbData, ctypes.c_void_p))
        del keep_in, keep_entropy


def protect(data: bytes) -> bytes:
    """Encrypt for the current Windows account."""
    return _call("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    """Decrypt a blob from `protect`; OSError when it is not ours or is damaged."""
    return _call("CryptUnprotectData", blob)
