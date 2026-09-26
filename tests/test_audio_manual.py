"""Stage-1 manual test: decode MP3/MP4 and play to a chosen output device.

Run from the project root:
    venv/Scripts/python.exe tests/test_audio_manual.py

Prints device list, decodes both test fixtures, plays each to a
user-selected device index so the result can be verified against
VoiceMeeter's level meters or by ear on a normal output device.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import devices
from soundboard.audio import AudioEngine, decode_audio

FIXTURES = Path(__file__).parent / "fixtures"


def print_devices():
    print("\n=== Output devices ===")
    for d in devices.list_output_devices():
        print(f"  [{d['index']}] {d['name']} (channels: {d['channels']})")


def decode_check():
    print("\n=== Decode check ===")
    for name in ("test_tone.mp3", "test_tone.mp4"):
        path = FIXTURES / name
        decoded = decode_audio(path)
        duration = len(decoded.samples) / decoded.samplerate
        print(f"  {name}: {decoded.samples.shape} samples @ {decoded.samplerate} Hz "
              f"(~{duration:.2f}s)")


def play_single(device_index: int):
    engine = AudioEngine(voicemeeter_device=device_index, monitor_device=None)
    engine.preload("mp3", FIXTURES / "test_tone.mp3")
    engine.preload("mp4", FIXTURES / "test_tone.mp4")

    print(f"\nPlaying MP3 (440Hz sine) to device {device_index} ...")
    p1 = engine.play("mp3")
    while not p1.is_finished():
        time.sleep(0.05)

    print(f"Playing MP4 audio track (880Hz sine, video ignored) to device {device_index} ...")
    p2 = engine.play("mp4")
    while not p2.is_finished():
        time.sleep(0.05)

    print("Done.")


def play_dual(voicemeeter_index: int, monitor_index: int):
    engine = AudioEngine(
        voicemeeter_device=voicemeeter_index,
        monitor_device=monitor_index,
        monitor_volume=0.5,
    )
    engine.preload("mp3", FIXTURES / "test_tone.mp3")
    print(
        f"\nDual-output: device {voicemeeter_index} @100%% + device {monitor_index} @50%% ..."
    )
    p = engine.play("mp3")
    while not p.is_finished():
        time.sleep(0.05)
    print("Done.")


def stop_all_check(device_index: int):
    engine = AudioEngine(voicemeeter_device=device_index, monitor_device=None)
    engine.preload("long", FIXTURES / "test_tone.mp3")
    print(f"\nPlaying then stopping immediately after 0.3s on device {device_index} ...")
    engine.play("long")
    time.sleep(0.3)
    engine.stop_all()
    print("stop_all() called — audio should have cut off immediately.")
    time.sleep(0.5)


if __name__ == "__main__":
    print_devices()
    decode_check()

    default_out = devices.default_output_device()
    choice = input(
        f"\nEnter device index to test playback on (default {default_out}): "
    ).strip()
    idx = int(choice) if choice else default_out

    play_single(idx)
    stop_all_check(idx)

    dual_choice = input(
        "\nTest dual-output too? Enter a second device index, or leave empty to skip: "
    ).strip()
    if dual_choice:
        play_dual(idx, int(dual_choice))
