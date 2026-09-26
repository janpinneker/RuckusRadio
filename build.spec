# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Ruckus Radio — onefile, windowed.

Run via build.ps1 (creates/uses the venv, fetches ffmpeg if missing) or
directly once assets\\ffmpeg.exe/ffprobe.exe/icon.ico exist:
    venv\\Scripts\\pyinstaller.exe build.spec --noconfirm

Entry point is the top-level run.py, not soundboard/main.py — see run.py for
why (frozen package imports). Datas: customtkinter's theme JSON files, and
sounddevice's PortAudio DLLs (shipped as the separate `_sounddevice_data`
package sounddevice.py loads by path at runtime — must be data, not a hidden
import, or the DLL never lands in the bundle). ffmpeg/ffprobe are pulled in
as binaries so PyInstaller doesn't try to inspect them for Python deps.
"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("customtkinter")
datas += collect_data_files("_sounddevice_data")

binaries = [
    ("assets/ffmpeg.exe", "assets"),
    ("assets/ffprobe.exe", "assets"),
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RuckusRadio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
