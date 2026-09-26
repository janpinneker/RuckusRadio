"""WebBridge ohne Fenster und ohne echten Kern: Befehle rein, Ereignisse gebuendelt raus."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-webbridge-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())  # rejected messages log on purpose

from soundboard import protocol as p  # noqa: E402
from soundboard.webbridge import JsApi, WebBridge  # noqa: E402


class FakeCore:
    def __init__(self, state=None, fail_state=None):
        self.sent = []
        self.subscribers = []
        self.state = state if state is not None else {"protocol": 1, "sounds": [], "playback": {"playing": [], "missing": []}}
        self.fail_state = fail_state

    def subscribe(self, cb):
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)

    def send(self, cmd):
        self.sent.append(cmd)

    def get_state(self, timeout=2.0):
        if self.fail_state:
            raise self.fail_state
        return self.state

    def emit(self, event):
        for cb in list(self.subscribers):
            cb(event)


class FakeWindow:
    def __init__(self, fail=False):
        self.scripts = []
        self.fail = fail

    def evaluate_js(self, script):
        if self.fail:
            raise RuntimeError("window is gone")
        self.scripts.append(script)

    def batches(self):
        out = []
        for s in self.scripts:
            prefix = "window.__ruckusEvent && window.__ruckusEvent("
            assert s.startswith(prefix) and s.endswith(")"), s
            out.append(json.loads(s[len(prefix):-1]))
        return out


def envelope(msg):
    return json.dumps(p.to_json(msg))


def make(**kw):
    core = FakeCore(**kw)
    bridge = WebBridge(core)
    window = FakeWindow()
    bridge.attach(window)
    return core, bridge, window


def notices(window):
    return [m for b in window.batches() for m in b if m["type"] == "Notice"]


def test_valid_command_reaches_the_core():
    core, bridge, _ = make()
    assert bridge.send(envelope(p.Play("s1", 0.5))) == {"ok": True}
    assert core.sent == [p.Play("s1", 0.5)]
    assert bridge.send(envelope(p.StopAll())) == {"ok": True}
    assert core.sent[-1] == p.StopAll()
    print("valid commands reach the core: OK")


def test_bad_input_is_rejected_with_a_notice():
    bad = [
        "{not json",
        json.dumps({"type": "Nope", "kind": "command", "v": 1, "data": {}}),
        json.dumps({"type": "Play", "kind": "command", "v": 2, "data": {"sound_id": "a"}}),
        envelope(p.PlaybackEnded("a")),  # kind == "event"
        json.dumps({"type": "PlaybackEnded", "kind": "command", "v": 1, "data": {"sound_id": "a"}}),  # event dressed as command
        json.dumps({"type": "Play", "kind": "command", "v": 1, "data": {"sound_id": "a", "evil": 1}}),
        json.dumps([1, 2]),
        None,
        42,
    ]
    core, bridge, window = make()
    bridge.get_state()  # the page is ready to receive events
    for text in bad:
        result = bridge.send(text)
        assert result["ok"] is False and result["error"], (text, result)
    assert core.sent == []
    bridge.flush()
    found = notices(window)
    assert len(found) == len(bad)
    assert all(n["data"]["level"] == "hint" and "ungültige Nachricht" in n["data"]["text"] for n in found)
    print("broken JSON, unknown type, wrong version, events and junk are rejected: OK")


def test_get_state_returns_json():
    core, bridge, _ = make()
    assert json.loads(bridge.get_state()) == core.state
    _, failing, _ = make(fail_state=TimeoutError("the core did not answer in time"))
    assert json.loads(failing.get_state()) == {"error": "the core did not answer in time"}
    print("get_state answers JSON, errors as {'error': ...}: OK")


def test_events_are_batched_in_order_and_state_is_coalesced():
    core, bridge, window = make()
    bridge.get_state()  # the page is ready to receive events
    core.emit(p.StateChanged({"n": 1}))
    core.emit(p.PlaybackStarted("a", 0.2))
    core.emit(p.StateChanged({"n": 2}))
    core.emit(p.PlaybackEnded("a"))
    core.emit(p.StateChanged({"n": 3}))
    assert bridge.flush() == 3
    [batch] = window.batches()
    assert [m["type"] for m in batch] == ["PlaybackStarted", "PlaybackEnded", "StateChanged"]
    assert batch[-1]["data"]["state"] == {"n": 3}
    assert all(m["kind"] == "event" and m["v"] == p.PROTOCOL_VERSION for m in batch)
    assert bridge.flush() == 0 and len(window.scripts) == 1  # nothing new: no call
    print("one evaluate_js per batch, order kept, newest StateChanged wins: OK")


def test_events_wait_for_the_window():
    core = FakeCore()
    bridge = WebBridge(core)
    core.emit(p.SoundAdded("a"))
    assert bridge.flush() == 0
    window = FakeWindow()
    bridge.attach(window)
    bridge.get_state()  # the page is ready to receive events
    assert bridge.flush() == 1
    assert window.batches()[0][0]["type"] == "SoundAdded"
    print("events queue until the window is attached: OK")


def test_a_dead_window_does_not_crash():
    core = FakeCore()
    bridge = WebBridge(core)
    bridge.attach(FakeWindow(fail=True))
    bridge.get_state()  # the page is ready to receive events
    core.emit(p.SoundAdded("a"))
    assert bridge.flush() == 1  # logged, dropped
    print("evaluate_js errors are logged, not raised: OK")


def test_events_wait_for_get_state_before_delivery():
    core, bridge, window = make()
    core.emit(p.SoundAdded("a"))
    core.emit(p.StateChanged({"n": 1}))
    core.emit(p.Notice("hi", "hint"))
    assert bridge.flush() == 0  # queued, not delivered: the page has not called get_state yet
    assert window.scripts == []
    bridge.get_state()
    assert bridge.flush() == 3
    [batch] = window.batches()
    assert [m["type"] for m in batch] == ["SoundAdded", "StateChanged", "Notice"]
    assert batch[1]["data"]["state"] == {"n": 1}
    assert batch[2]["data"]["text"] == "hi" and batch[2]["data"]["level"] == "hint"
    print("events before get_state wait, then arrive in order with notices intact: OK")


def test_page_reloaded_holds_events_again():
    core, bridge, window = make()
    bridge.get_state()
    core.emit(p.SoundAdded("a"))
    assert bridge.flush() == 1
    bridge.page_reloaded()
    core.emit(p.SoundAdded("b"))
    assert bridge.flush() == 0  # waiting for the new page's get_state
    bridge.get_state()
    assert bridge.flush() == 1
    delivered = [m["type"] for b in window.batches() for m in b]
    assert delivered == ["SoundAdded", "SoundAdded"]
    print("page_reloaded() holds events again until the next get_state: OK")


def test_a_fresh_page_resumes_hotkeys_the_old_page_left_suspended():
    core, bridge, window = make()
    bridge.get_state()
    assert core.sent == [], "a normal start does not re-register (no duplicate notices)"
    bridge.send(envelope(p.SuspendHotkeys()))  # capture field open ...
    bridge.page_reloaded()  # ... and the page reloads before it can resume
    bridge.get_state()
    assert core.sent == [p.SuspendHotkeys(), p.ResumeHotkeys()], core.sent
    bridge.get_state()  # a retry must not resume twice
    assert core.sent.count(p.ResumeHotkeys()) == 1, core.sent
    # a page that resumed by itself leaves nothing to do
    bridge.send(envelope(p.SuspendHotkeys()))
    bridge.send(envelope(p.ResumeHotkeys()))
    bridge.get_state()
    assert core.sent.count(p.ResumeHotkeys()) == 2, core.sent
    print("a fresh page resumes hotkeys the old page left suspended: OK")


def test_file_path_commands_are_rejected():
    core, bridge, window = make()
    bridge.get_state()  # the page is ready to receive events
    file_path_messages = [
        envelope(p.AddSound("C:/sounds/x.wav", "X")),
        envelope(p.SetSoundIcon("s1", "C:/icons/x.png")),
        envelope(p.ExportSounds("C:/export")),
        envelope(p.ImportPack("C:/pack.zip")),
    ]
    for text in file_path_messages:
        result = bridge.send(text)
        assert result["ok"] is False and result["error"], (text, result)
    assert core.sent == []  # never reached the core
    bridge.flush()
    found = notices(window)
    assert len(found) == len(file_path_messages)
    assert all(n["data"]["level"] == "hint" and "ungültige Nachricht" in n["data"]["text"] for n in found)
    # a normal command still passes
    assert bridge.send(envelope(p.Play("s1", 0.5))) == {"ok": True}
    assert core.sent == [p.Play("s1", 0.5)]
    print("AddSound/SetSoundIcon/ExportSounds/ImportPack are rejected before the core, other commands still pass: OK")


def test_pump_keeps_to_30_per_second():
    core = FakeCore()
    bridge = WebBridge(core, max_rate=30.0)
    window = FakeWindow()
    bridge.attach(window)
    bridge.get_state()  # the page is ready to receive events
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
    calls = len(window.scripts)
    delivered = [m["data"]["sound_id"] for b in window.batches() for m in b]
    assert calls <= 33, calls  # 30/s plus one flush at start and one in the 0.2 s tail
    assert calls >= 10, calls
    assert delivered == [f"s{i}" for i in range(sent)]
    assert core.subscribers == []
    print(f"pump: {sent} events in {calls} calls (<= ~30/s), none lost, close unsubscribes: OK")


def test_ready_records_info():
    _, bridge, _ = make()
    seen = []
    bridge.on_ready = seen.append
    bridge.ready(json.dumps({"ui": "web"}))
    assert bridge.ready_info == {"ui": "web"} and seen == [{"ui": "web"}]
    bridge.ready("not json")
    assert bridge.ready_info == {}
    print("ready() records the info and calls on_ready: OK")


def test_js_api_exposes_only_three_methods():
    _, bridge, _ = make()
    api = JsApi(bridge)
    public = sorted(n for n in dir(api) if not n.startswith("_"))
    assert public == ["get_state", "ready", "send"], public
    assert api.send(envelope(p.StopAll())) == {"ok": True}
    assert json.loads(api.get_state())["protocol"] == 1
    assert api.ready("{}") is None
    print("JsApi exposes exactly send/get_state/ready: OK")


def test_emit_from_many_threads():
    core, bridge, window = make()
    bridge.get_state()  # the page is ready to receive events
    threads = [threading.Thread(target=lambda i=i: [core.emit(p.SoundAdded(f"{i}-{k}")) for k in range(200)]) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bridge.flush()
    assert sum(len(b) for b in window.batches()) == 800
    print("events from several threads are all delivered: OK")


def test_origin_of():
    from soundboard import webmain

    assert webmain.origin_of("http://127.0.0.1:61234/index.html") == "http://127.0.0.1:61234"
    assert webmain.origin_of("http://127.0.0.1:61234/assets/x.js?y=1") == "http://127.0.0.1:61234"
    assert webmain.origin_of("https://rareui.com/") == "https://rareui.com"
    assert webmain.origin_of(None) == ""
    assert webmain.origin_of("about:blank") == "about:"
    print("origin_of: OK")


def test_index_path_names_the_fix():
    from soundboard import webmain

    original = webmain.dist_dir
    webmain.dist_dir = lambda: Path(_TMP) / "no-dist"
    try:
        webmain.index_path()
    except FileNotFoundError as exc:
        assert "npm run build" in str(exc)
    else:
        raise AssertionError("a missing webui/dist must raise")
    finally:
        webmain.dist_dir = original
    print("missing webui/dist raises with the fix in the message: OK")


def test_run_webui_reports_failures():
    from soundboard import main as app_main

    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Keep(level=logging.ERROR)
    logging.getLogger("soundboard.main").addHandler(handler)
    try:
        shown = []

        def boom():
            raise FileNotFoundError("web interface not built: index.html is missing")

        assert app_main.run_webui(runner=boom, show=shown.append) is False
        assert len(shown) == 1 and "not built" in shown[0] and "ohne --webui" in shown[0]
        assert records and records[-1].exc_info is not None  # traceback in ruckus.log
        assert app_main.run_webui(runner=lambda: None, show=shown.append) is True
        assert len(shown) == 1
    finally:
        logging.getLogger("soundboard.main").removeHandler(handler)
    print("run_webui logs and shows start failures, returns True on success: OK")


def main():
    test_valid_command_reaches_the_core()
    test_bad_input_is_rejected_with_a_notice()
    test_get_state_returns_json()
    test_events_are_batched_in_order_and_state_is_coalesced()
    test_events_wait_for_the_window()
    test_a_dead_window_does_not_crash()
    test_events_wait_for_get_state_before_delivery()
    test_page_reloaded_holds_events_again()
    test_a_fresh_page_resumes_hotkeys_the_old_page_left_suspended()
    test_file_path_commands_are_rejected()
    test_ready_records_info()
    test_js_api_exposes_only_three_methods()
    test_emit_from_many_threads()
    test_origin_of()
    test_index_path_names_the_fix()
    test_run_webui_reports_failures()
    test_pump_keeps_to_30_per_second()
    print("\nALL WEBBRIDGE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
