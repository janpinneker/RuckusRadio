"""Nur eine Ruckus-Instanz gleichzeitig: zwei wuerden sich um dieselben Kabel und das
Mikrofon streiten (z. B. Autostart plus Doppelklick). Benannter Windows-Mutex per
ctypes, keine Abhaengigkeit. Den Hinweis an den Nutzer zeigt der Aufrufer."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

DEFAULT_NAME = "Local\\RuckusRadioSingleInstance"
ERROR_ALREADY_EXISTS = 183


class InstanceLock:
    def __init__(self, handle):
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _kernel32().CloseHandle(handle)


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def acquire(name: str = DEFAULT_NAME) -> InstanceLock | None:
    """The lock, or None when another instance already holds it. If Windows refuses
    to create the mutex at all, the start is not blocked (empty lock)."""
    try:
        kernel32 = _kernel32()
    except (AttributeError, OSError):
        return InstanceLock(None)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        log.warning("single-instance mutex could not be created (error %s)",
                    ctypes.get_last_error())
        return InstanceLock(None)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return InstanceLock(handle)
