"""Start Ruckus Radio with Windows (HKCU Run key).

In cable mode the voice chat only has a microphone while Ruckus Radio runs, because
the app itself carries the mic. Autostart takes that sharp edge off. The HKCU Run
key needs no admin rights and no extra dependency - a .lnk in shell:startup would
need COM just to be written.
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RuckusRadio"

log = logging.getLogger(__name__)


def command() -> str:
    """Quoted command that starts this installation the same way it runs now: the frozen
    exe directly, else pythonw on run.py (the launcher that makes the package importable
    regardless of the working directory)."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    launcher = Path(__file__).resolve().parent.parent / "run.py"
    return f'"{interpreter}" "{launcher}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def enable() -> bool:
    """True when the Run entry is in place afterwards."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        return True
    except OSError:
        log.warning("could not write the autostart entry", exc_info=True)
        return False


def disable() -> bool:
    """True when no Run entry is left (including: there never was one)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.warning("could not remove the autostart entry", exc_info=True)
        return False


def apply(enabled: bool) -> bool:
    return enable() if enabled else disable()
