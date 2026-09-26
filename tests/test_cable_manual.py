r"""Manueller Ende-zu-Ende-Test der Sink-Gruppe - oeffnet ECHTE Geraete, macht ECHTEN Ton.

    venv\\Scripts\\python.exe tests\\test_cable_manual.py

Berichtet, in dieser Reihenfolge: welche virtuellen Kabel gefunden wurden, ob ein Ton
tatsaechlich durch jedes davon geht, ob das Mikrofon einen Pegel liefert, und wie viel
Drift der Mixer je Ziel ausgleichen musste. Sprich waehrenddessen.
"""

import sys
import time
from pathlib import Path

import numpy as np

# Unlike the logic tests this one deliberately reads the REAL %APPDATA%\Soundboard
# config - checking the actual setup is the whole point. It only reads; set
# RUCKUS_DATA_DIR yourself to point it somewhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, devices, miccheck, sinkgroup  # noqa: E402


def tone(seconds=1.0, hz=440.0, samplerate=48000):
    t = np.arange(int(seconds * samplerate), dtype=np.float32) / samplerate
    wave = (0.3 * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.repeat(wave.reshape(-1, 1), 2, axis=1)


def main():
    cfg = config.load_config()
    resolved = devices.resolve_system_devices(cfg)
    pairs = resolved.get("virtual_mics") or []
    if not pairs:
        print("Kein virtuelles Mikrofon gefunden - nichts zu testen.")
        return 1

    all_ok = True
    for pair in pairs:
        print(f"Ziel:             {pair['label']} ({pair['key']})")
        print(f"App spielt in:    {pair['out_name']}  [{pair['out_index']}]")
        print(f"Sprachchat-Gerät:  {pair['in_name']}  [{pair['in_index']}]")
        path = miccheck.verify_path(pair["out_index"], pair["in_index"])
        print(f"Signalweg:        {'OK' if path['ok'] else 'FEHLER'} (RMS {path['rms']}) — {path['reason']}")
        print()
        all_ok = all_ok and path["ok"]

    mic = miccheck.verify_mic(resolved["mic"])
    print(f"Mikrofon:         {'OK' if mic['ok'] else 'FEHLER'} (RMS {mic['rms']}) — {mic['reason']}")

    group = sinkgroup.build(cfg, resolved)
    if group is None:
        print("\nFEHLER: Sink-Gruppe konnte nicht gebaut werden.")
        return 1
    print(f"\nSink-Gruppe läuft, Mikro {'live' if group.mic_active else 'AUS'}. "
          "Sprich jetzt, du solltest dich in der Gegenstelle hören.")
    group.add_source(tone(2.0), gain=1.0)
    time.sleep(4)
    for pair in pairs:
        target = group.target(pair["key"])
        print(f"Drift ({pair['key']}): {target.dropped_blocks} Blöcke verworfen, "
              f"{target.starved_blocks} Blöcke Stille eingeschoben")
    group.stop()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
