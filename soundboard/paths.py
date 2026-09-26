"""Resource path resolution for dev vs. the PyInstaller onefile build, and
pointing pydub at the bundled ffmpeg/ffprobe before any decode happens.

Never confuse this with config.get_app_data_dir(): resource_path() resolves
read-only files shipped with the app (assets/); it must never be used for
config/sounds/icons, which always live in the app data dir (see config.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path


def app_root() -> Path:
    """The project root in dev; the onefile build's extraction dir
    (sys._MEIPASS) when frozen — wiped between runs, read-only in practice."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def resource_path(rel: str) -> Path:
    return app_root() / rel


def configure_ffmpeg() -> str | None:
    """Point pydub's AudioSegment at the bundled assets/ffmpeg.exe + ffprobe.exe
    when present, and prepend the assets dir to PATH so ffprobe (which pydub
    also shells out to for format probing) is found the same way. If the
    bundled binaries are absent (dev checkout without assets/*.exe fetched),
    do nothing and let pydub fall back to system PATH silently.

    Returns the ffmpeg path pydub was pointed at, or None when falling back
    to PATH resolution."""
    hide_pydub_consoles()
    assets_dir = resource_path("assets")
    ffmpeg = assets_dir / "ffmpeg.exe"
    ffprobe = assets_dir / "ffprobe.exe"
    if not (ffmpeg.exists() and ffprobe.exists()):
        return None

    from pydub import AudioSegment

    AudioSegment.converter = str(ffmpeg)
    # no AudioSegment.ffprobe needed: pydub finds ffprobe.exe via the PATH prepend below
    os.environ["PATH"] = str(assets_dir) + os.pathsep + os.environ.get("PATH", "")
    return str(ffmpeg)


def hide_pydub_consoles() -> None:
    """pydub starts ffmpeg/ffprobe with a plain Popen. In the windowed exe every decode
    then opens a console window (on Windows 11 a Terminal window that can stay on top of
    the board). Give pydub a Popen that always adds CREATE_NO_WINDOW. Idempotent."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not flag:
        return
    from pydub import audio_segment, utils

    if getattr(utils.Popen, "hides_console", False):
        return

    class NoWindowPopen(subprocess.Popen):
        hides_console = True

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
            super().__init__(*args, **kwargs)

    utils.Popen = NoWindowPopen  # mediainfo/ffprobe: `from subprocess import Popen`
    # decode/export: audio_segment calls subprocess.Popen through its module reference
    audio_segment.subprocess = types.SimpleNamespace(**{**vars(subprocess), "Popen": NoWindowPopen})


def ffmpeg_path() -> str | None:
    """The ffmpeg that also decodes the sounds: bundled assets\\ffmpeg.exe, else the
    one on PATH, else None."""
    bundled = resource_path("assets") / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("ffmpeg")
