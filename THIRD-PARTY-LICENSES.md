# Drittanbieter-Lizenzen

Ruckus Radio bündelt oder nutzt folgende Software.

## FFmpeg (ffmpeg.exe, ffprobe.exe)

In der fertigen `RuckusRadio.exe` stecken `ffmpeg.exe` und `ffprobe.exe` aus dem „essentials“-Build von gyan.dev (https://www.gyan.dev/ffmpeg/builds/). Ruckus Radio ruft sie als eigenständige Programme auf, es linkt sie nicht.
Lizenz: GNU General Public License v3 (https://www.gnu.org/licenses/gpl-3.0.html). Quellcode: https://ffmpeg.org/download.html bzw. die zum Build gehörende Quelle unter https://github.com/GyanD/codexffmpeg/releases.

## Python-Pakete

| Paket | Lizenz |
|---|---|
| sounddevice (inkl. PortAudio) | MIT |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| pydub | MIT |
| audioop-lts | PSF-2.0 |
| customtkinter | CC0-1.0 (Creative Commons Zero v1.0 Universal) |
| pillow | MIT-CMU (HPND) |
| keyboard | MIT |
| PyInstaller (nur zum Bauen; Bootloader-Ausnahme der GPL) | GPL-2.0-or-later mit Bootloader-Ausnahme |
| Python | PSF-2.0 |
