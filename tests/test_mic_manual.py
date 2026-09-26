r"""Live-Pegelanzeige fuer den Mikrofon-Weg. Manueller Test, kein Teil der Suite.

Startet die echte Sink-Gruppe mit der echten %APPDATA%\Soundboard\config.json (nur
lesend, wie test_cable_manual.py) und zeigt eine Pegelzeile je Ziel:

    MIKRO    = was das Mikrofon liefert
    <Ziel>   = was am Aufnahme-Ende des jeweiligen virtuellen Kabels ankommt, also
               genau das, was der dortige Sprachchat hoert

Sprich normal. Ein Ziel mit ausgeschaltetem Mikrofon muss dabei sichtbar STUMM
bleiben, waehrend die anderen ausschlagen - das ist der Beweis, dass die Matrix auf
der Einstellungen-Seite wirkt.

Laeuft 8 Sekunden und faellt dann sein Urteil; andere Dauer als erstes Argument.
Ruckus Radio vorher schliessen - dieses Skript baut seine eigene Sink-Gruppe auf, und
zwei Gruppen wuerden beide ins selbe Kabel schreiben.
"""

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, devices, sinkgroup, virtualmic  # noqa: E402

NEWLINE = chr(10)
CR = chr(13)


def bar(level: float, width: int = 30) -> str:
    filled = min(width, int(round(level / 0.15 * width)))
    return "#" * filled + "." * (width - filled)


def main() -> int:
    cfg = config.load_config()
    resolved = devices.resolve_system_devices(cfg)

    group = sinkgroup.build(cfg, resolved)
    if group is None:
        print("FEHLER: Kein Ziel gefunden (kein virtuelles Kabel installiert?).")
        return 1

    # Was am Aufnahme-Ende jedes Kabels ankommt - also das, was der Sprachchat hoert.
    levels = {}
    listeners = []
    for pair in resolved.get("virtual_mics") or []:
        levels[pair["key"]] = 0.0

        def watch(indata, frames, time_info, status, key=pair["key"]):
            levels[key] = float(np.sqrt(np.mean(np.square(indata))))

        stream = sd.InputStream(
            samplerate=virtualmic.TARGET_SAMPLERATE, device=pair["in_index"],
            channels=1, dtype="float32", blocksize=virtualmic.BLOCKSIZE, callback=watch)
        stream.start()
        listeners.append(stream)

    mic_level = [0.0]
    original = group._input_callback

    def watch_mic(indata, frames, time_info, status):
        mic_level[0] = float(np.sqrt(np.mean(np.square(indata))))
        original(indata, frames, time_info, status)

    if group.mic_active:
        group._in_stream.stop()
        group._in_stream.close()
        group._in_stream = sd.InputStream(
            samplerate=virtualmic.TARGET_SAMPLERATE, device=group.mic_device, channels=1,
            dtype="float32", blocksize=group.blocksize, callback=watch_mic)
        group._in_stream.start()

    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    print("SPRICH JETZT NORMAL INS MIKROFON - %.0f Sekunden lang." % seconds)
    print()
    peaks = {k: 0.0 for k in levels}
    mic_peak = 0.0
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            mic_peak = max(mic_peak, mic_level[0])
            parts = ["MIKRO %s %.4f" % (bar(mic_level[0]), mic_level[0])]
            for key, value in levels.items():
                peaks[key] = max(peaks[key], value)
                parts.append("%s %s %.4f" % (key.split(" (")[0], bar(value), value))
            print(CR + "   ".join(parts), end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for stream in listeners:
            stream.stop()
            stream.close()
        group.stop()

    print(NEWLINE)
    print("Spitzenpegel Mikrofon : %.5f" % mic_peak)
    for key, peak in peaks.items():
        target = group.target(key)
        print("  %-44s %.5f   (Mikro %s, Sounds %s, verworfen %d, ausgehungert %d)" % (
            key, peak, "an" if target.mic else "aus", "an" if target.sounds else "aus",
            target.dropped_blocks, target.starved_blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
