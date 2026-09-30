"""Process-Loopback mit echtem Spotify - Handpruefung mit Jan (F3), NICHT im gruenen Lauf.

Vorbedingungen: Ruckus geschlossen (Ein-Instanz-Mutex, Geraete), Python OHNE Sandbox
(die Sandbox liefert reine Stille), Spotify-Desktop-App offen.

Aufruf:  venv/Scripts/python.exe tests/test_process_loopback_manual.py

Teil A: Spotify spielt -> Pegel >= 6 dB ueber der Stille (Spotify pausiert).
Teil B: Spotify pausiert, gleichzeitig ein 440-Hz-Ton aus DIESEM Python-Prozess
        (ein anderes Programm als Spotify) -> Pegel bleibt ~Stille (< 3 dB darueber).
Teil C: Spotify beenden und neu starten -> der Waechter haengt sich wieder an.
Spec §6 Schritt 1 / §7.1 / §7.2.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-procloop-manual-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO)

from soundboard import musicbus, processloopback  # noqa: E402
from test_musicbus_manual import measure, tone_player  # noqa: E402

TARGET_DB = 6.0
FOREIGN_MAX_DB = 3.0


def db(loud: float, quiet: float) -> float:
    if quiet <= 0:
        return float("inf") if loud > 0 else 0.0
    return 20.0 * math.log10(loud / quiet)


def ask(text: str) -> None:
    input(f"\n>>> {text}  [Enter] ")


def main() -> int:
    tree = [e for e in processloopback.process_entries() if e[2].casefold() == "spotify.exe"]
    print(f"Spotify-Prozesse (pid, parent, exe): {tree}")
    pid = processloopback.find_spotify_pid()
    print(f"gewaehlte Wurzel: {pid}")
    if pid is None:
        print("NICHT BESTANDEN: Spotify laeuft nicht.")
        return 1

    bus = musicbus.spotify_bus(gain=1.0)
    bus.start()
    try:
        deadline = time.time() + 6.0
        while time.time() < deadline and bus.format is None and bus.error is None:
            time.sleep(0.05)
        if bus.error is not None:
            print(f"FEHLER bei der Aktivierung: {bus.error!r}")
            return 1
        print(f"Format: {bus.format}  waiting={bus.waiting}")

        ask("Spotify PAUSIEREN")
        silence = measure(bus, 1.5)
        print(f"Stille rms = {silence:.5f}")

        ask("Spotify ABSPIELEN (normale Lautstaerke)")
        music = measure(bus, 3.0)
        a = db(music, silence)
        print(f"A: Spotify rms = {music:.5f}  (+{a:.1f} dB)")

        ask("Spotify wieder PAUSIEREN - gleich spielt Python 3 s einen 440-Hz-Ton")
        player = threading.Thread(target=tone_player(seconds=3.0), daemon=True)
        player.start()
        time.sleep(0.3)
        foreign = measure(bus, 2.2)
        player.join(timeout=5.0)
        b = db(foreign, silence)
        print(f"B: fremder Ton rms = {foreign:.5f}  (+{b:.1f} dB, erlaubt < {FOREIGN_MAX_DB})")

        ask("Spotify BEENDEN (Datei > Beenden bzw. Tray > Beenden)")
        deadline = time.time() + 10.0
        while time.time() < deadline and not bus.waiting:
            time.sleep(0.2)
        c1 = bus.waiting
        print(f"C1: waiting nach dem Beenden = {c1}")

        ask("Spotify neu STARTEN und ABSPIELEN")
        deadline = time.time() + 20.0
        while time.time() < deadline and bus.waiting:
            time.sleep(0.2)
        time.sleep(1.0)
        again = measure(bus, 3.0)
        c2 = (not bus.waiting) and db(again, silence) >= TARGET_DB
        print(f"C2: wieder angehaengt = {not bus.waiting}, rms = {again:.5f} "
              f"(+{db(again, silence):.1f} dB), error={bus.error!r}")

        ok = a >= TARGET_DB and music > 0.005 and b < FOREIGN_MAX_DB and c1 and c2
        print("\nPROCESS LOOPBACK MANUAL CHECK PASSED" if ok else "\nNICHT BESTANDEN")
        return 0 if ok else 1
    finally:
        bus.close()
        print(f"nach close: running={bus.running}, error={bus.error!r}")


if __name__ == "__main__":
    raise SystemExit(main())
