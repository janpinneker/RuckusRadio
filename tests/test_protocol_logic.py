"""Protokoll ohne Kern: jede Nachricht uebersteht den Weg durch JSON unveraendert."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-protocol-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import protocol as p  # noqa: E402

EXAMPLES = [
    p.AddSound("C:/a.mp3", "Airhorn"), p.AddSound("C:/a.mp4", "Horn", "C:/i.png"),
    p.DeleteSound("id1"), p.RenameSound("id1", "Neu"), p.SetSoundIcon("id1", "C:/i.png"),
    p.SetSoundVolume("id1", 0.5), p.SetHotkey("id1", "ctrl+shift+1"), p.RemoveHotkey("id1"),
    p.SuspendHotkeys(), p.ResumeHotkeys(), p.ExportSounds("C:/x.ruckuspack"),
    p.ExportSounds("C:/x.ruckuspack", ("id1", "id2")), p.ImportPack("C:/x.ruckuspack"),
    p.Play("id1"), p.Play("id1", 1.5), p.Stop("id1"), p.StopAll(),
    p.SetOutput("CABLE Output", {"mic": False, "sounds_gain": 0.5}),
    p.SetLevels({"sounds_offset_db": -9.0}), p.SetMicrophone("Mikrofon (NVIDIA Broadcast)"),
    p.SetMicrophone("Mikrofon (NVIDIA Broadcast)", apply=False),
    p.ToggleMicMute(), p.RunSignalCheck(), p.Rescan(), p.SetOnboardingActive(True),
    p.CompleteOnboarding(), p.SetAutostart(False),
    p.CheckForUpdates(), p.InstallUpdate(),
    p.StateChanged({"sounds": [{"id": "a", "loudness": {"integrated": -20.0}}], "n": None}),
    p.PlaybackStarted("id1", 0.25), p.PlaybackEnded("id1"), p.SoundMissing("id1"),
    p.SoundAdded("id1"), p.SignalCheckDone(({"key": "CABLE", "ok": True, "rms": 0.1},)),
    p.DevicesChanged({"found": True, "name": "CABLE Input (VB-Audio Virtual Cable)",
                      "label": "VB-CABLE", "discord_device_name": "CABLE Output",
                      "mic_name": "Mikrofon (Endorfy Solum Voice S Mic)",
                      "monitor_name": "Kopfhörer (KT USB Audio)", "mixer_running": True}),
    p.HeadphonesSwitched("Kopfhörer (KT USB Audio)"),
    p.UpdateAvailable("1.2.0", "Neu: Updates."), p.UpdateAvailable("1.2.0"),
    p.UpdateReady("C:/RuckusRadioSetup-1.2.0.exe"),
    p.Notice("Hallo"), p.Notice("Fehler", "error"),
]


def roundtrip(msg):
    return p.from_json(json.loads(json.dumps(p.to_json(msg))))


def test_every_example_roundtrips():
    for msg in EXAMPLES:
        assert roundtrip(msg) == msg, msg
    print("every message survives JSON unchanged: OK")


def test_envelope_shape():
    data = p.to_json(p.Play("id1", 0.5))
    assert data == {"type": "Play", "kind": "command", "v": p.PROTOCOL_VERSION,
                    "data": {"sound_id": "id1", "volume": 0.5, "preview": False}}, data
    assert p.to_json(p.PlaybackEnded("x"))["kind"] == "event"
    print("envelope carries type, kind, version and data: OK")


def test_every_registered_message_has_an_example():
    covered = {type(m).__name__ for m in EXAMPLES}
    assert covered == set(p._REGISTRY), set(p._REGISTRY) ^ covered
    print("examples cover every registered message: OK")


def test_messages_are_frozen():
    msg = p.Play("id1")
    try:
        msg.sound_id = "other"
    except Exception:
        pass
    else:
        raise AssertionError("messages must be immutable")
    print("messages are immutable: OK")


def expect_error(data, needle):
    try:
        p.from_json(data)
    except p.ProtocolError as exc:
        assert needle in str(exc), exc
    else:
        raise AssertionError(f"expected ProtocolError for {data!r}")


def test_decoding_rejects_bad_input():
    expect_error([], "object")
    expect_error({"type": "Nope", "v": 1, "data": {}}, "unknown message type")
    expect_error({"type": "Play", "v": 99, "data": {"sound_id": "a"}}, "protocol version")
    expect_error({"type": "Play", "v": 1, "data": {"sound_id": "a", "evil": 1}}, "unknown field")
    expect_error({"type": "Play", "v": 1, "data": {}}, "Play")
    expect_error({"type": "Play", "v": 1}, "data")
    print("decoding rejects bad input: OK")


def test_encoding_rejects_non_json():
    for bad in (p.Play("a", float("nan")), p.StateChanged({"s": {1, 2}}),
                p.StateChanged({1: "int key"})):
        try:
            p.to_json(bad)
        except p.ProtocolError:
            continue
        raise AssertionError(f"expected ProtocolError for {bad!r}")
    print("encoding rejects values that are not JSON: OK")


def test_notice_level_is_checked():
    try:
        p.Notice("x", "loud")
    except p.ProtocolError:
        pass
    else:
        raise AssertionError("unknown notice level must be rejected")
    print("notice level is validated: OK")


def test_duplicate_names_are_rejected():
    try:
        @p.message
        class Play(p.Command):  # noqa: F811 - the point of the test
            sound_id: str
    except p.ProtocolError:
        pass
    else:
        raise AssertionError("a second message named Play must be rejected")
    print("duplicate message names are rejected: OK")


def main():
    test_every_example_roundtrips()
    test_envelope_shape()
    test_every_registered_message_has_an_example()
    test_messages_are_frozen()
    test_decoding_rejects_bad_input()
    test_encoding_rejects_non_json()
    test_notice_level_is_checked()
    test_duplicate_names_are_rejected()
    print("\nALL PROTOCOL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
