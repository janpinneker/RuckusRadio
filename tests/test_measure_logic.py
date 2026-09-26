"""Messmodus B0 ohne Fenster, ohne Geraete: Plaene, Auswertung, Bericht."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-measure-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
os.environ.pop("RUCKUS_MEASURE_DIR", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import measure  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def test_disabled_without_env():
    assert not measure.enabled() and measure.out_dir() is None
    assert measure.write_json("x.json", {"a": 1}) is None
    measure.mark_ready("tk")  # no-op, no crash
    print("measure mode is off without RUCKUS_MEASURE_DIR: OK")


def test_writes_when_enabled():
    out = Path(_TMP) / "m"
    os.environ[measure.ENV] = str(out)
    try:
        path = measure.write_json("tk.json", {"a": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
        assert not list(out.glob("*.tmp"))
        measure.mark_ready("web")
        ready = json.loads((out / "ready-web.json").read_text(encoding="utf-8"))
        assert ready["ui"] == "web" and ready["t"] > 0
    finally:
        os.environ.pop(measure.ENV)
    print("write_json is atomic, mark_ready writes ready-<ui>.json: OK")


def test_resize_plan():
    plan = measure.resize_plan()
    assert len(plan) == 20
    assert all(960 <= w <= 1600 and 620 <= h <= 1000 for w, h in plan)
    assert all(a != b for a, b in zip(plan, plan[1:]))  # every step really changes the size
    print("resize plan: 20 distinct steps within the minimum size: OK")


def test_summarize():
    s = measure.summarize([10.0, 20.0, 60.0, 30.0])
    assert s == {"steps": 4, "max_ms": 60.0, "median_ms": 25.0, "over_limit": 1, "limit_ms": 50.0, "ok": False}
    assert measure.summarize([])["ok"] is False  # nothing measured is not a pass
    assert measure.summarize([49.9])["ok"] is True
    print("summarize: max, median, over limit, empty is not ok: OK")


class FakeCore:
    def __init__(self):
        self.subs = []

    def subscribe(self, cb):
        self.subs.append(cb)
        return lambda: self.subs.remove(cb)

    def emit(self, ev):
        for cb in list(self.subs):
            cb(ev)


def test_playback_probe():
    core = FakeCore()
    ticks = iter([5.0, 6.0])
    probe = measure.PlaybackProbe(core, "s1", clock=lambda: next(ticks))
    core.emit(p.PlaybackStarted("other", 0.1))
    core.emit(p.PlaybackStarted("s1", 0.1))
    core.emit(p.PlaybackStarted("s1", 0.1))  # only the first one counts
    assert probe.wait(0.1) == 5.0
    probe.close()
    assert core.subs == []
    silent = measure.PlaybackProbe(FakeCore(), "s1")
    assert silent.wait(0.05) is None
    print("PlaybackProbe takes the first start of its sound, None on timeout: OK")


def test_dropped_blocks():
    class T:
        def __init__(self, key, n):
            self.key, self.dropped_blocks = key, n

    class Sink:
        targets = [T("CABLE", 3), T("VM", 0)]

    class Routing:
        _sink = Sink()

    class Core:
        routing = Routing()

    assert measure.dropped_blocks(Core()) == {"CABLE": 3, "VM": 0}
    assert measure.dropped_blocks(object()) == {}
    print("dropped_blocks reads the sink targets, {} without a sink: OK")


def good(ui):
    return {"ui": ui, "resize": measure.summarize([12.0, 18.0]), "hotkey": {"latency_ms": 8.0, "blocked_s": 3.0},
            "cable": {"played": True, "signal_ok": True, "results": [{"key": "CABLE", "ok": True}], "dropped_delta": {"CABLE": 0}},
            "security": {"api_keys": ["get_state", "ready", "send"], "api_ok": True, "csp_violations": []},
            "cold_s": 4.2, "ram_mb": 180.0}


def test_verdict():
    assert measure.verdict(good("tk"), good("web")) == []
    web = good("web")
    web["resize"] = measure.summarize([80.0])
    web["hotkey"]["latency_ms"] = None
    web["cable"]["signal_ok"] = False
    web["cable"]["dropped_delta"] = {"CABLE": 4}
    fails = measure.verdict(good("tk"), web)
    assert len(fails) == 4, fails
    assert any("50 ms" in f for f in fails) and any("Hotkey" in f for f in fails)
    assert any("Kabel" in f for f in fails) and any("dropped" in f for f in fails)
    print("verdict names every missed hard value: OK")


def test_report():
    text = measure.render_report(good("tk"), good("web"), {"before_mb": 108.0, "after_mb": 131.5}, "2026-09-27 10:00")
    for needle in ("| Fenster-Anpassen", "| Hotkey", "| Ton am Kabel", "| dropped_blocks", "| RAM", "| Kaltstart", "| exe-Größe",
                   "| JS-API und CSP", "API ok, CSP-Verstöße: 0", "108.0 MB", "131.5 MB", "bestanden"):
        assert needle in text, needle
    broken = good("web")
    broken["resize"] = measure.summarize([])
    assert "nicht bestanden" in measure.render_report(good("tk"), broken, {}, "x")
    print("report has every row and the decision: OK")


def test_attach_tk_is_a_no_op_without_env():
    """Review R4 (D7): without RUCKUS_MEASURE_DIR the Tk start is untouched."""
    import threading

    class FakeApp:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*args, **kwargs):
                self.calls.append(name)
            return record

    assert not measure.enabled()
    before = threading.active_count()
    app = FakeApp()
    measure.attach_tk(app)
    assert app.calls == [], app.calls
    assert threading.active_count() == before
    print("attach_tk without RUCKUS_MEASURE_DIR touches nothing, starts no thread: OK")


def test_refused_without_data_dir():
    """RUCKUS_MEASURE_DIR alone must not measure against the real %APPDATA%\\Soundboard."""
    import threading

    class FakeApp:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*args, **kwargs):
                self.calls.append(name)
            return record

    class FakeBridge:
        on_ready = None

    out = Path(_TMP) / "refused"
    data = os.environ.pop("RUCKUS_DATA_DIR")
    os.environ[measure.ENV] = str(out)
    try:
        assert measure.out_dir() == out
        assert not measure.enabled()
        assert measure.write_json("tk.json", {"a": 1}) is None
        measure.mark_ready("tk")
        before = threading.active_count()
        app = FakeApp()
        measure.attach_tk(app)
        assert app.calls == [] and threading.active_count() == before, app.calls
        bridge = FakeBridge()
        measure.attach_web(object(), bridge, object())
        assert bridge.on_ready is None
        assert not out.exists()
    finally:
        os.environ.pop(measure.ENV)
        os.environ["RUCKUS_DATA_DIR"] = data
    print("RUCKUS_MEASURE_DIR without RUCKUS_DATA_DIR: measurement refused, nothing written: OK")


def test_main_does_not_load_pywebview():
    """Review R4 (D7): the normal start never imports pywebview (only --webui does)."""
    import soundboard.main  # noqa: F401

    assert "webview" not in sys.modules
    print("importing soundboard.main does not load pywebview: OK")


class KeyEvent:
    def __init__(self, name, event_type="down"):
        self.name, self.event_type = name, event_type


def test_key_press_probe():
    ticks = iter([7.0, 8.0])
    probe = measure.KeyPressProbe("ctrl+alt+shift+f9", clock=lambda: next(ticks))
    assert probe.key == "f9"
    probe.on_event(KeyEvent("ctrl"))
    probe.on_event(KeyEvent("f9", "up"))
    assert probe.wait(0.05) is None
    probe.on_event(KeyEvent("F9"))
    probe.on_event(KeyEvent("f9"))  # only the first press counts
    assert probe.wait(0.1) == 7.0
    print("KeyPressProbe times the first press of the hotkey's main key: OK")


def test_manual_hotkey_mode():
    os.environ.pop(measure.MANUAL_ENV, None)
    assert measure.manual_hotkey() is False and measure.block_seconds() == measure.BLOCK_S
    os.environ[measure.MANUAL_ENV] = "1"
    try:
        assert measure.manual_hotkey() is True
        assert measure.block_seconds() == measure.MANUAL_BLOCK_S > measure.BLOCK_S
        rows = measure.render_report(
            {"hotkey": {"latency_ms": 20.0, "manual": True}},
            {"hotkey": {"latency_ms": 30.0, "manual": True}}, {}, "now")
        assert "von Hand gedrückt" in rows, rows
    finally:
        os.environ.pop(measure.MANUAL_ENV, None)
    print("manual hotkey mode: longer block, marked in the report: OK")


def main():
    test_disabled_without_env()
    test_attach_tk_is_a_no_op_without_env()
    test_refused_without_data_dir()
    test_main_does_not_load_pywebview()
    test_writes_when_enabled()
    test_resize_plan()
    test_summarize()
    test_playback_probe()
    test_key_press_probe()
    test_manual_hotkey_mode()
    test_dropped_blocks()
    test_verdict()
    test_report()
    print("\nALL MEASURE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
