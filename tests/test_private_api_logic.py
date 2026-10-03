"""Waechter fuer private APIs von Fremdpaketen, die Ruckus bewusst nutzt.

Ein Upgrade von sounddevice oder customtkinter kann sie still entfernen:
`devices.rescan_system_devices` schluckt jeden Fehler (Neuscan saehe dann neue Treiber
nie), und die Tk-Nadel braeche erst beim Zeichnen. Faellt dieser Test, die Aufrufstelle
an die neue Version anpassen, bevor die Pin-Version in requirements.txt steigt."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk  # noqa: E402
import sounddevice as sd  # noqa: E402


def test_sounddevice_can_restart_portaudio():
    # soundboard/devices.py: rescan_system_devices(reinit=True)
    assert callable(getattr(sd, "_terminate", None)), sd.__version__
    assert callable(getattr(sd, "_initialize", None)), sd.__version__
    print("sounddevice._terminate/_initialize vorhanden: OK")


def test_customtkinter_frames_know_their_scaling():
    # soundboard/views.py: TunerBanner zeichnet die Nadel in Geraetepixeln
    assert callable(getattr(ctk.CTkFrame, "_get_widget_scaling", None)), ctk.__version__
    print("CTkFrame._get_widget_scaling vorhanden: OK")


def main():
    test_sounddevice_can_restart_portaudio()
    test_customtkinter_frames_know_their_scaling()
    print("\nALL PRIVATE API CHECKS PASSED")


if __name__ == "__main__":
    main()
