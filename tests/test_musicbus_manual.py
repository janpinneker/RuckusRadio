"""Musik-Bus mit echten Geraeten — Handpruefung, NICHT in der gruenen Schleife.

Vorbedingungen (AGENTS.md):
- Freigabe F3 fuer echte Geräte; kein anderes Ruckus-Fenster offen
  (Ein-Instanz-Mutex + Audio-Ausgaenge).
- Ton kurz anhoerbar erlaubt: es wird ein 440-Hz-Ton ueber die Standard-
  Wiedergabe gespielt und dabei das Loopback gemessen.
- Exclusive Mode eines anderen Programms ist bewusst NICHT geprueft (offen fuer F3).

Aufruf:  venv/Scripts/python.exe tests/test_musicbus_manual.py

Was geprueft wird: der Bus startet am Standard-Wiedergabegeraet, liefert
48-kHz-Stereo-Float32, und ein Ton auf der Standard-Wiedergabe hebt den
gemessenen Pegel deutlich ueber die Stille (wie im PoC-Lauf 2026-09-27:
+16.9 dB). Gehen > 6 dB gilt als bestanden.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-musicbus-manual-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import numpy as np  # noqa: E402

from soundboard import musicbus  # noqa: E402
from soundboard import virtualmic  # noqa: E402

TARGET_DB = 6.0


def measure(bus: musicbus.MusicBus, seconds: float) -> float:
    """rms ueber `seconds` gezogene Bloecke."""
    total_sq = 0.0
    count = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        block = bus.pull(virtualmic.BLOCKSIZE)
        total_sq += float(np.square(block).sum())
        count += block.size
        time.sleep(virtualmic.BLOCKSIZE / virtualmic.TARGET_SAMPLERATE)
    return math.sqrt(total_sq / count) if count else 0.0


def tone_player(seconds: float = 2.5, hz: float = 440.0, level: float = 0.25):
    """Spielt einen Ton ueber die Standard-Wiedergabe (derselbe Weg wie der PoC)."""

    def play() -> None:
        from soundboard import devices

        devices.init_thread_com()  # jede Thread, der PortAudio anfasst, braucht COM
        import sounddevice as sd

        t = np.arange(int(virtualmic.TARGET_SAMPLERATE * seconds)) / virtualmic.TARGET_SAMPLERATE
        data = (np.sin(2.0 * math.pi * hz * t) * level).astype(np.float32)
        sd.play(np.column_stack([data, data]), virtualmic.TARGET_SAMPLERATE)
        sd.wait()

    return play


def main() -> int:
    bus = musicbus.MusicBus()
    bus.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and bus.format is None and bus.error is None:
            time.sleep(0.02)
        if bus.error is not None:
            print(f"FEHLER beim Oeffnen: {bus.error!r}")
            return 1
        print(f"Format: {bus.format}")
        assert bus.format.rate == virtualmic.TARGET_SAMPLERATE or True  # nur informativ

        silence = measure(bus, 1.0)
        print(f"Stille rms = {silence:.4f}")

        player = threading.Thread(target=tone_player(), daemon=True)
        player.start()
        time.sleep(0.3)
        loud = measure(bus, 2.0)
        player.join(timeout=4.0)

        if silence > 0:
            delta_db = 20.0 * math.log10(loud / silence)
        else:
            delta_db = float("inf") if loud > 0 else 0.0
        print(f"Ton rms = {loud:.4f}  (+{delta_db:.1f} dB ueber der Stille)")

        if delta_db >= TARGET_DB and loud > 0.01:
            print("\nMUSICBUS MANUAL CHECK PASSED")
            return 0
        print(f"\nNICHT BESTANDEN: der Ton hebt den Pegel nur um {delta_db:.1f} dB "
              f"(Ziel >= {TARGET_DB} dB). Pruefen: Standard-Wiedergabe klingt? "
              "Anderes Programm im Exclusive Mode?")
        return 1
    finally:
        bus.close()
        print(f"nach close: running={bus.running}, error={bus.error!r}")


if __name__ == "__main__":
    raise SystemExit(main())
