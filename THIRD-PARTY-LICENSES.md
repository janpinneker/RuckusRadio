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

## Web-Oberfläche (webui/, ab B0)

- **RareUI** (swamimalode07/rare-ui) – MIT License mit Commons Clause. Verwendet: `fluid-orb`. Nutzung in der App erlaubt, Weiterverkauf der Komponenten selbst nicht. Sichtbarer Link zu https://rareui.com in Einstellungen → „Über“. Zurzeit Platzhalter: `webui/src/components/ui/fluid-orb.tsx` ist eine lokale Nachbildung mit derselben Schnittstelle, kein RareUI-Code; der echte Bezug wartet auf Freigabe.
- **React Bits** (reactbits.dev) – MIT License mit Commons Clause. Registry eingetragen; gilt für jede Komponente, die aus `@react-bits` nach `webui/src/components/ui/` kommt.
- **shadcn/ui** – MIT License. Komponenten als Quelltext unter `webui/src/components/ui/`.
- **React, Radix UI, Motion, Zustand, TanStack Virtual, dnd kit, Tailwind CSS, Vite, sonner, cmdk** – MIT License; **Lucide** – ISC License. Gebaut als statische Dateien in der exe enthalten.
- **Inter** (rsms/inter, Copyright 2016 The Inter Project Authors) – SIL Open Font License 1.1 (https://openfontlicense.org). Gebündelt über das npm-Paket `@fontsource-variable/inter` (variable Schnitte mit Achsen `wght` und `opsz`, als woff2 in den statischen Dateien der exe, kein Abruf zur Laufzeit). Der vollständige Lizenztext liegt als `webui/public/licenses/Inter-OFL.txt` bei und landet so neben den Schriftdateien im Build. Der Name „Inter“ ist kein Reserved Font Name; die Schrift wird unverändert verwendet.
