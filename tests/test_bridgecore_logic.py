"""Bruecke ohne Fenster: zwei Clients, Faehigkeiten, Buendelung je Gruppe."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-bridgecore-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

from soundboard import access  # noqa: E402
from soundboard import protocol as p  # noqa: E402
from soundboard.bridgecore import MAX_PENDING, BridgeCore  # noqa: E402
import core_fakes  # noqa: E402


class FakeCore:
    def __init__(self, fail_state=None):
        self.sent = []
        self.subscribers = []
        self.fail_state = fail_state

    def subscribe(self, cb):
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)

    def send(self, cmd):
        self.sent.append(cmd)

    def get_state(self, timeout=2.0):
        if self.fail_state:
            raise self.fail_state
        return {"protocol": 1, "sounds": [], "playback": {}}

    def emit(self, event):
        for cb in list(self.subscribers):
            cb(event)


class FakeAccess:
    def __init__(self, window="window-key", view="view-key"):
        self.window_token = window
        self.view_token = view

    def role_for(self, token):
        if token == self.window_token:
            return access.ROLE_WINDOW
        if token == self.view_token:
            return access.ROLE_VIEW
        return None

    def may_send(self, role, command):
        return access.Access.may_send(self, role, command)

    def rotate_view_token(self):
        self.view_token += "2"
        return self.view_token


def make(**kw):
    core = FakeCore(**kw)
    boxes: list = []
    bridge = BridgeCore(core, lambda cid, msgs: boxes.append((cid, msgs)),
                        access=FakeAccess())
    return core, bridge, boxes


def envelope(msg):
    return json.dumps(p.to_json(msg))


def notices(boxes):
    return [m for _, batch in boxes for m in batch if m["type"] == "Notice"]


def test_two_roles_and_unknown_keys_are_refused():
    _, bridge, _ = make()
    assert bridge.add_client("win", bridge.window_token) is not None
    assert bridge.add_client("view", bridge.view_token) is not None
    assert bridge.add_client("boese", "falsch") is None
    assert bridge.add_client("leer", None) is None
    print("zwei Rollen, unbekannter Schluessel abgewiesen: OK")


def test_the_view_client_cannot_manage_but_may_play():
    core, bridge, boxes = make()
    bridge.add_client("view", bridge.view_token)
    bridge.get_state("view")
    result = bridge.send("view", envelope(p.DeleteSound("s1")))
    assert result["ok"] is False and result["error"], result
    assert core.sent == [], core.sent
    bridge.flush()
    found = notices(boxes)
    assert len(found) == 1 and "nur abspielen" in found[0]["data"]["text"], found
    assert bridge.send("view", envelope(p.Play("s1", 0.5))) == {"ok": True}
    assert core.sent == [p.Play("s1", 0.5)]
    print("die Ansicht wird abgewiesen und bekommt einen Hinweis: OK")


def test_a_command_without_a_capability_is_refused_for_everyone():
    core, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    original = dict(access.COMMAND_CAPABILITY)
    access.COMMAND_CAPABILITY.pop(p.StopAll)
    try:
        assert bridge.send("win", envelope(p.StopAll()))["ok"] is False
        assert core.sent == []
    finally:
        access.COMMAND_CAPABILITY.clear()
        access.COMMAND_CAPABILITY.update(original)
    print("ein Befehl ohne Faehigkeit ist fuer beide Rollen verboten: OK")


def test_events_reach_every_ready_client():
    core, bridge, boxes = make()
    bridge.add_client("win", bridge.window_token)
    bridge.add_client("view", bridge.view_token)
    core.emit(p.SoundAdded("a"))
    assert bridge.flush() == 0
    bridge.get_state("win")
    bridge.get_state("view")
    assert bridge.flush() == 2
    assert sorted(cid for cid, _ in boxes) == ["view", "win"]
    print("Ereignisse erreichen alle bereiten Clients: OK")


def test_coalescing_is_per_group_and_per_job():
    core, bridge, boxes = make()
    bridge.add_client("win", bridge.window_token)
    bridge.get_state("win")
    core.emit(p.StateChanged({"n": 1}))
    core.emit(p.StateChanged({"n": 2}))
    core.emit(p.PartChanged("library", {"sounds": [1]}))
    core.emit(p.PartChanged("library", {"sounds": [1, 2]}))
    core.emit(p.PartChanged("music", {"tracks": [1]}))
    core.emit(p.JobChanged({"id": "a", "state": "running"}))
    core.emit(p.JobChanged({"id": "a", "state": "done"}))
    core.emit(p.JobChanged({"id": "b", "state": "running"}))
    core.emit(p.PlaybackStarted("a", 0.1))
    assert bridge.flush() == 6
    [batch] = [b for _, b in boxes]
    # Abweichung von der Plan-Vorlage: dort stand PlaybackStarted an erster Stelle,
    # obwohl es zuletzt (und ohne Zusammenfassen) eingereiht wird. Die Reihenfolge
    # ist die der Einreihung, ein neueres zusammengefasstes Ereignis ersetzt das alte
    # an dessen Stelle nicht, es landet am Ende.
    assert [m["type"] for m in batch] == ["StateChanged", "PartChanged", "PartChanged",
                                          "JobChanged", "JobChanged", "PlaybackStarted"], batch
    assert batch[0]["data"]["state"] == {"n": 2}
    assert [m["data"]["part"] for m in batch if m["type"] == "PartChanged"] == \
        [{"sounds": [1, 2]}, {"tracks": [1]}]
    assert [m["data"]["job"]["state"] for m in batch if m["type"] == "JobChanged"] == \
        ["done", "running"]
    print("zusammengefasst wird je Gruppe und je Job: OK")


def test_a_client_that_never_fetches_its_state_cannot_grow_without_bound():
    core, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    for i in range(MAX_PENDING + 50):
        core.emit(p.SoundAdded(f"s{i}"))
    pending = bridge._clients["win"].pending
    assert len(pending) == MAX_PENDING, len(pending)
    assert pending[-1]["data"]["sound_id"] == f"s{MAX_PENDING + 49}", pending[-1]
    print("die Warteschlange eines Clients ohne get_state bleibt begrenzt: OK")


def test_a_borrowed_client_can_be_removed_without_undoing_a_suspension():
    core, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    bridge.send("win", envelope(p.SuspendHotkeys()))
    bridge.remove_client("win", resume_hotkeys=False)
    assert core.sent == [p.SuspendHotkeys()], core.sent
    # a real window client still releases it on its startup read
    bridge.add_client("win2", bridge.window_token)
    bridge.get_state("win2")
    assert core.sent == [p.SuspendHotkeys(), p.ResumeHotkeys()], core.sent
    print("ein geliehener Client laesst eine Sperre unangetastet: OK")


def test_a_client_that_dies_while_capturing_resumes_hotkeys():
    core, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    bridge.send("win", envelope(p.SuspendHotkeys()))
    bridge.remove_client("win")
    assert core.sent == [p.SuspendHotkeys(), p.ResumeHotkeys()], core.sent
    print("ein sterbender Client setzt die Hotkeys wieder scharf: OK")


def test_a_reloading_page_resumes_hotkeys():
    core, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    bridge.send("win", envelope(p.SuspendHotkeys()))
    bridge.client_unready("win")
    bridge.get_state("win")
    assert core.sent == [p.SuspendHotkeys(), p.ResumeHotkeys()], core.sent
    bridge.get_state("win")
    assert core.sent.count(p.ResumeHotkeys()) == 1
    print("eine neu geladene Seite setzt die Hotkeys wieder scharf: OK")


def test_file_paths_and_broken_input_are_refused_without_raising():
    core, bridge, boxes = make()
    bridge.add_client("win", bridge.window_token)
    bridge.get_state("win")
    bad = ["{kein json", None, 42, json.dumps([1, 2]),
           json.dumps({"type": "Play", "kind": "command", "v": 2, "data": {"sound_id": "a"}}),
           envelope(p.PlaybackEnded("a")),
           envelope(p.AddSound("C:/x.wav", "X")), envelope(p.SetSoundIcon("s1", "C:/x.png")),
           envelope(p.ExportSounds("C:/out")), envelope(p.ImportPack("C:/p.zip"))]
    for text in bad:
        assert bridge.send("win", text)["ok"] is False, text
    assert core.sent == []
    bridge.flush()
    assert len(notices(boxes)) == len(bad)
    print("kaputte Nachrichten und Dateipfade werden abgewiesen, ohne zu werfen: OK")


def test_get_state_names_the_role_and_the_view_url():
    _, bridge, _ = make()
    bridge.add_client("view", bridge.view_token)
    state = json.loads(bridge.get_state("view"))
    assert state["role"] == access.ROLE_VIEW
    assert bridge.view_url(47800) == f"http://127.0.0.1:47800/#t={bridge.view_token}"
    print("get_state nennt die Rolle, view_url den Link: OK")


def test_view_url_uses_the_current_view_token():
    core, bridge, _ = make()
    first = bridge.view_url(47800)
    bridge.rotate_view_token()
    assert bridge.view_url(47800) != first
    print("view_url folgt einem erneuerten Schluessel: OK")


def test_get_state_returns_json_and_errors_as_json():
    _, bridge, _ = make()
    bridge.add_client("win", bridge.window_token)
    assert json.loads(bridge.get_state("win"))["protocol"] == 1
    _, failing, _ = make(fail_state=TimeoutError("keine Antwort"))
    failing.add_client("win", failing.window_token)
    assert json.loads(failing.get_state("win")) == {"error": "keine Antwort"}
    assert json.loads(bridge.get_state("unbekannt")) == {"error": "unknown client"}
    print("get_state liefert JSON, Fehler als {'error': ...}: OK")


def test_the_pump_keeps_to_the_rate_and_stops_cleanly():
    core = FakeCore()
    boxes: list = []
    bridge = BridgeCore(core, lambda cid, msgs: boxes.append(msgs), access=FakeAccess(),
                        max_rate=30.0)
    bridge.add_client("win", bridge.window_token)
    bridge.get_state("win")
    pump = bridge.start_pump()
    stop_at = time.monotonic() + 1.0
    sent = 0
    while time.monotonic() < stop_at:
        core.emit(p.SoundAdded(f"s{sent}"))
        sent += 1
        time.sleep(0.001)
    time.sleep(0.2)
    bridge.close()
    pump.join(timeout=2)
    assert not pump.is_alive()
    delivered = [m["data"]["sound_id"] for batch in boxes for m in batch]
    assert len(boxes) <= 33, len(boxes)
    assert len(boxes) >= 10, len(boxes)
    assert delivered == [f"s{i}" for i in range(sent)]
    assert core.subscribers == []
    print(f"Pumpe: {sent} Ereignisse in {len(boxes)} Aufrufen, keins verloren: OK")


def test_a_held_notice_reaches_the_first_window_stream_once():
    # Startup notices (port fell back, "updated to X") exist before any page does. They
    # wait for the first window stream; a view or a later window does not get them.
    _, bridge, boxes = make()
    bridge.hold_notice("Ruckus Radio wurde aktualisiert.", "info")
    bridge.add_client("view", bridge.view_token)
    bridge.mark_ready("view")
    bridge.stream_opened("view")
    bridge.add_client("win", bridge.window_token)
    bridge.mark_ready("win")
    bridge.stream_opened("win")
    bridge.flush()
    found = notices(boxes)
    assert [(n["data"]["text"], n["data"]["level"]) for n in found] == [
        ("Ruckus Radio wurde aktualisiert.", "info")], found
    assert [cid for cid, _ in boxes] == ["win"], boxes
    bridge.add_client("win2", bridge.window_token)
    bridge.mark_ready("win2")
    bridge.stream_opened("win2")
    boxes.clear()
    bridge.flush()
    assert notices(boxes) == [], "a held notice is shown once"
    print("ein Start-Hinweis erreicht einmal den ersten Fenster-Strom: OK")


def test_viewer_count_counts_ready_clients():
    c, _ = core_fakes.make_core()
    bridge = BridgeCore(c, lambda *_: None)
    assert bridge.viewer_count() == 0
    bridge.add_client("w", bridge.window_token)
    assert bridge.viewer_count() == 0, "a client counts once its page asked for the state"
    bridge.mark_ready("w")
    assert bridge.viewer_count() == 1
    bridge.remove_client("w")
    assert bridge.viewer_count() == 0
    print("viewer_count counts ready clients: OK")


def main():
    test_viewer_count_counts_ready_clients()
    test_a_held_notice_reaches_the_first_window_stream_once()
    test_two_roles_and_unknown_keys_are_refused()
    test_the_view_client_cannot_manage_but_may_play()
    test_a_command_without_a_capability_is_refused_for_everyone()
    test_events_reach_every_ready_client()
    test_coalescing_is_per_group_and_per_job()
    test_a_client_that_never_fetches_its_state_cannot_grow_without_bound()
    test_a_borrowed_client_can_be_removed_without_undoing_a_suspension()
    test_a_client_that_dies_while_capturing_resumes_hotkeys()
    test_a_reloading_page_resumes_hotkeys()
    test_file_paths_and_broken_input_are_refused_without_raising()
    test_get_state_returns_json_and_errors_as_json()
    test_get_state_names_the_role_and_the_view_url()
    test_view_url_uses_the_current_view_token()
    test_the_pump_keeps_to_the_rate_and_stops_cleanly()
    print("\nALL BRIDGECORE CHECKS PASSED")


if __name__ == "__main__":
    main()
