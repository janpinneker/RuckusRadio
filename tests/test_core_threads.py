"""Der Kern mit echten Threads: Geraete-Arbeit nur auf dem Geraete-Thread, Plays
warten hinter einem Umbau, der Kern antwortet waehrenddessen, Fehler toeten nichts,
Hotkeys aus fremden Threads, sauberes Beenden."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-threads-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, core, playback, protocol as p  # noqa: E402
import core_fakes  # noqa: E402

DEVICE_THREAD = "ruckus-devices"


class SlowBackend(core_fakes.FakeBackend):
    def __init__(self):
        super().__init__()
        self.rescan_delay = 0.0
        self.rescan_done_at = None
        self.fail_next_build = False

    def rescan(self, cfg, reinit):
        time.sleep(self.rescan_delay)
        result = super().rescan(cfg, reinit)
        self.rescan_done_at = time.monotonic()
        return result

    def build_sink(self, cfg, resolved):
        if self.fail_next_build:
            self.fail_next_build = False
            raise RuntimeError("driver hiccup")
        return super().build_sink(cfg, resolved)


class TimedEngine(core_fakes.FakeEngine):
    def __init__(self):
        super().__init__()
        self.play_times: list[float] = []

    def play(self, sound_id, volume=1.0):
        super().play(sound_id, volume)
        self.play_times.append(time.monotonic())


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.005)


def threaded_core():
    data = config._default_config()
    sounds_dir = config.get_app_data_dir() / "sounds"  # creates sounds/ and icons/
    for i in range(3):
        (sounds_dir / f"s{i}.mp3").write_bytes(b"x")
        data["sounds"].append({"id": f"s{i}", "name": f"Sound {i}", "file": f"sounds/s{i}.mp3",
                               "icon": f"icons/s{i}.png", "hotkey": "f9" if i == 0 else None,
                               "volume": 1.0,
                               "loudness": {"integrated": -20.0, "max_short": -20.0, "peak": -3.0}})
    engine = TimedEngine()
    engine.loaded = {"s0", "s1", "s2"}
    backend = SlowBackend()
    hotkeys = core_fakes.FakeHotkeys()
    c = core.create_core(inline=False, engine=engine, backend=backend, hotkey_manager=hotkeys,
                         store_data=data, autostart_module=core_fakes.FakeAutostart())
    events: list = []
    c.subscribe(events.append)
    c.start()
    wait_until(lambda: backend.sinks)
    return c, events, engine, backend, hotkeys


def test_device_work_only_on_the_device_thread():
    c, events, engine, backend, hotkeys = threaded_core()
    for sid in ("s0", "s1", "s2"):
        c.send(p.Play(sid))
    wait_until(lambda: len(engine.plays) == 3)
    wait_until(lambda: any(call == "playing_ids" for call, _ in engine.threads))
    c.send(p.StopAll())
    wait_until(lambda: engine.stopped == 1)
    assert {name for _call, name in engine.threads} == {DEVICE_THREAD}, engine.threads
    assert {name for _call, name in backend.threads} == {DEVICE_THREAD}, backend.threads
    assert c.shutdown() is True
    print("every engine and backend call ran on the device thread: OK")


def test_plays_wait_behind_a_rescan_and_the_core_keeps_answering():
    c, events, engine, backend, hotkeys = threaded_core()
    backend.rescan_delay = 0.4
    c.send(p.Rescan())
    time.sleep(0.05)
    started = time.monotonic()
    state = c.get_state()
    assert time.monotonic() - started < 0.3, "the core answers while the device thread is busy"
    assert state["sounds"][0]["id"] == "s0"
    for sid in ("s0", "s1"):
        c.send(p.Play(sid))
    wait_until(lambda: len(engine.plays) == 2)
    assert all(t >= backend.rescan_done_at for t in engine.play_times), "plays ran after it"
    assert c.shutdown() is True
    print("plays wait behind a rescan while the core keeps answering: OK")


def test_a_play_stuck_too_long_is_dropped():
    c, events, engine, backend, hotkeys = threaded_core()
    backend.rescan_delay = playback.MAX_PLAY_WAIT_S + 0.3
    c.send(p.Rescan())
    time.sleep(0.05)
    c.send(p.Play("s0"))
    wait_until(lambda: p.Notice(playback.PLAY_DROPPED) in events,
               timeout=playback.MAX_PLAY_WAIT_S + 3)
    assert engine.plays == []
    assert c.shutdown() is True
    print("a play stuck behind a very long rebuild is dropped with a hint: OK")


def test_a_device_failure_does_not_kill_anything():
    c, events, engine, backend, hotkeys = threaded_core()
    backend.fail_next_build = True
    c.send(p.Rescan())
    wait_until(lambda: p.Notice(core.INTERNAL_ERROR, "error") in events)
    c.send(p.Play("s1"))
    wait_until(lambda: engine.plays)
    assert c.alive
    assert c.shutdown() is True
    print("a failing device task becomes a notice, core and device thread live on: OK")


def test_a_hotkey_from_a_foreign_thread_plays():
    c, events, engine, backend, hotkeys = threaded_core()
    wait_until(lambda: "f9" in hotkeys.registered)
    hook = threading.Thread(target=hotkeys.registered["f9"], name="keyboard-hook")
    hook.start()
    hook.join()
    wait_until(lambda: engine.plays)
    assert engine.plays[0][0] == "s0"
    assert c.shutdown() is True
    print("a hotkey fired on a foreign thread plays through the core: OK")


def test_shutdown_flushes_and_stops_the_mixer():
    c, events, engine, backend, hotkeys = threaded_core()
    key = core_fakes.CABLE_KEY
    c.send(p.SetOutput(key, {"sounds_gain": 0.25}))  # debounced save still pending
    sink = backend.sinks[-1]
    assert c.shutdown(timeout=5.0) is True
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert saved["outputs"][key]["sounds_gain"] == 0.25
    assert sink.stopped
    assert hotkeys.registered == {}
    assert not c.alive
    c.send(p.StopAll())  # after shutdown: dropped, no error
    print("shutdown flushes the pending save, stops the mixer and the hooks: OK")


def test_a_hotkey_plays_while_the_ui_thread_is_blocked():
    """Spec §8: the interface's subscriber only queues (like RuckusRadioApp.call_in_ui)
    and its thread is stuck for a whole second - the hotkey still plays at once."""
    import queue

    c, events, engine, backend, hotkeys = threaded_core()
    ui_queue: queue.SimpleQueue = queue.SimpleQueue()
    c.subscribe(ui_queue.put)
    ui_busy = threading.Event()
    release = threading.Event()

    def blocked_ui():
        ui_busy.set()
        release.wait(2.0)  # never drains ui_queue while blocked

    ui = threading.Thread(target=blocked_ui, name="fake-tk")
    ui.start()
    ui_busy.wait(1.0)
    wait_until(lambda: "f9" in hotkeys.registered)
    started = time.monotonic()
    threading.Thread(target=hotkeys.registered["f9"], name="keyboard-hook").start()
    wait_until(lambda: engine.plays, timeout=0.3)
    assert time.monotonic() - started < 0.3
    assert engine.plays[0][0] == "s0"
    assert ui.is_alive(), "the UI thread was still blocked when the sound started"
    release.set()
    ui.join()
    assert not ui_queue.empty(), "events waited for the UI instead of blocking the core"
    assert c.shutdown() is True
    print("a hotkey plays within 0.3 s while the UI thread is blocked: OK")


def main():
    test_device_work_only_on_the_device_thread()
    test_plays_wait_behind_a_rescan_and_the_core_keeps_answering()
    test_a_play_stuck_too_long_is_dropped()
    test_a_device_failure_does_not_kill_anything()
    test_a_hotkey_from_a_foreign_thread_plays()
    test_shutdown_flushes_and_stops_the_mixer()
    test_a_hotkey_plays_while_the_ui_thread_is_blocked()
    print("\nALL CORE THREAD CHECKS PASSED")


if __name__ == "__main__":
    main()
