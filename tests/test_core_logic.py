"""Kern ohne Fachmodule: Registratur, Fehlerhuelle, Zustand, Lebenszyklus."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-core-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, core, protocol as p  # noqa: E402
import core_fakes  # noqa: E402


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.005)


def test_commands_reach_their_handler():
    c, events = core_fakes.bare_core()
    seen = []
    c.handle(p.StopAll, seen.append)
    c.send(p.StopAll())
    assert seen == [p.StopAll()]
    try:
        c.handle(p.StopAll, seen.append)
    except ValueError:
        pass
    else:
        raise AssertionError("a second handler for the same command must be refused")
    print("commands reach their handler, one handler per command: OK")


def test_failures_become_notices_and_the_core_lives_on():
    c, events = core_fakes.bare_core()
    c.handle(p.Rescan, lambda _cmd: 1 / 0)
    seen = []
    c.handle(p.StopAll, seen.append)
    c.send(p.Rescan())
    c.send(p.ToggleMicMute())  # no handler registered
    c.send(p.StopAll())
    notices = core_fakes.of_type(events, p.Notice)
    assert [n.text for n in notices] == [core.INTERNAL_ERROR, core.INTERNAL_ERROR], notices
    assert all(n.level == "error" for n in notices)
    assert seen == [p.StopAll()], "later commands still run"
    print("failing or unknown commands become error notices: OK")


def test_state_parts_and_inline_state_changed():
    c, events = core_fakes.bare_core()
    c.add_state("demo", lambda: {"n": 1})
    c.state_changed()
    states = core_fakes.of_type(events, p.StateChanged)
    assert states == [p.StateChanged({"protocol": p.PROTOCOL_VERSION, "demo": {"n": 1}})]
    json.dumps(p.to_json(states[0]))
    try:
        c.add_state("demo", lambda: None)
    except ValueError:
        pass
    else:
        raise AssertionError("state part names must be unique")
    print("state is assembled from registered parts: OK")


def test_subscribers_can_leave_and_a_broken_one_hurts_nobody():
    c, events = core_fakes.bare_core()
    other = []

    def broken(_event):
        raise RuntimeError("subscriber bug")

    c.subscribe(broken)
    unsubscribe = c.subscribe(other.append)
    c.notice("eins")
    unsubscribe()
    c.notice("zwei")
    assert [e.text for e in other] == ["eins"]
    assert [e.text for e in core_fakes.of_type(events, p.Notice)] == ["eins", "zwei"]
    print("unsubscribe works, a failing subscriber does not stop delivery: OK")


def test_save_failure_is_reported():
    c, events = core_fakes.bare_core()
    original = config.save_config

    def broken(_data):
        raise OSError("locked")

    config.save_config = broken
    try:
        c.store.save_now()
    finally:
        config.save_config = original
    assert core_fakes.of_type(events, p.Notice) == [p.Notice(core.SAVE_FAILED, "hint")]
    print("a failed save becomes a hint notice: OK")


def test_lifecycle_order_inline():
    c, events = core_fakes.bare_core()
    order = []
    c.on_start(lambda: order.append("start-1"))
    c.on_start(lambda: order.append("start-2"))
    c.before_shutdown(lambda: order.append(("before", threading.current_thread().name)))
    c.on_shutdown(lambda: order.append("shutdown"))
    c.on_shutdown(lambda: 1 / 0)  # a failing step must not stop the others
    c.on_shutdown(lambda: order.append("shutdown-2"))
    c.start()
    c.store.data["autostart"] = True
    c.store.save_soon()
    assert c.shutdown() is True
    assert order == ["start-1", "start-2", ("before", threading.current_thread().name),
                     "shutdown", "shutdown-2"], order
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert saved["autostart"] is True, "shutdown flushes a pending save"
    seen = []
    c.handle(p.StopAll, seen.append)
    c.send(p.StopAll())
    assert seen == [], "commands after shutdown are dropped"
    print("start and shutdown run in order, shutdown flushes and closes the door: OK")


def test_threaded_core_coalesces_state_and_answers_get_state():
    c = core.Core(inline=False, store_data=config._default_config())
    events = []
    c.subscribe(events.append)
    c.add_state("demo", lambda: {"ok": True})
    burst_done = threading.Event()

    def burst(_cmd):
        for _ in range(20):
            c.state_changed()
        burst_done.set()

    c.handle(p.Rescan, burst)
    c.send(p.Rescan())
    wait_until(burst_done.is_set)
    wait_until(lambda: core_fakes.of_type(events, p.StateChanged))
    time.sleep(core.STATE_INTERVAL_S * 3)
    assert len(core_fakes.of_type(events, p.StateChanged)) == 1, "20 changes, one StateChanged"
    assert c.get_state()["demo"] == {"ok": True}
    assert c.alive
    assert c.shutdown(timeout=3.0) is True
    assert not c.alive
    print("threaded core: 20 changes collapse into one event, get_state answers: OK")


def test_shutdown_waits_for_workers_before_flushing():
    c = core.Core(inline=False, store_data=config._default_config())
    submitted = threading.Event()

    def on_rescan(_cmd):
        def job():
            time.sleep(0.3)

            def apply():
                c.store.data["stop_all_hotkey"] = "f12"
                c.store.save_soon()

            c.executor.submit(apply)

        c.workers.submit(job)
        submitted.set()

    c.handle(p.Rescan, on_rescan)
    c.send(p.Rescan())
    submitted.wait(3.0)
    assert c.shutdown(timeout=5.0) is True
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert saved["stop_all_hotkey"] == "f12", "a worker result racing shutdown must not be dropped"
    print("shutdown waits for workers before flushing the store: OK")


def test_shutdown_is_refused_on_the_core_thread_and_idempotent():
    c = core.Core(inline=False, store_data=config._default_config())
    caught: list = []
    recorded = threading.Event()
    before_calls: list = []
    c.before_shutdown(lambda: before_calls.append(1))

    def on_rescan(_cmd):
        try:
            c.shutdown()
        except Exception as exc:  # noqa: BLE001 - recording the exception type
            caught.append(type(exc))
        finally:
            recorded.set()

    c.handle(p.Rescan, on_rescan)
    c.send(p.Rescan())
    wait_until(recorded.is_set)
    assert caught == [RuntimeError], caught

    first = c.shutdown()
    assert first is True
    assert len(before_calls) == 1, before_calls
    second = c.shutdown()
    assert second == first, "the second call must return the first call's result"
    assert len(before_calls) == 1, "before_shutdown must not run again on the second call"
    print("shutdown is refused on the core thread and idempotent: OK")


def test_get_state_fails_fast_when_the_core_is_gone():
    c = core.Core(inline=False, store_data=config._default_config())
    assert c.shutdown(timeout=5.0) is True
    start = time.monotonic()
    try:
        c.get_state(timeout=5)
    except RuntimeError:
        pass
    else:
        raise AssertionError("get_state must fail fast once the core thread is gone")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, elapsed
    print("get_state fails fast once the core is gone: OK")


def test_a_stuck_worker_does_not_starve_the_shutdown_steps():
    c = core.Core(inline=False, store_data=config._default_config())
    started = threading.Event()
    flags = {"device_ran": False}

    def on_rescan(_cmd):
        def stuck_job():
            time.sleep(1.5)

        c.workers.submit(stuck_job)
        started.set()

    def on_shutdown_step():
        def device_task():
            flags["device_ran"] = True

        c.devices.submit(device_task)
        c.store.data["stop_all_hotkey"] = "f11"
        c.store.save_soon()

    c.handle(p.Rescan, on_rescan)
    c.on_shutdown(on_shutdown_step)
    c.send(p.Rescan())
    started.wait(3.0)

    c.shutdown(timeout=1.0)  # may return False: the stuck worker is still sleeping

    wait_until(lambda: flags["device_ran"], timeout=3.0)
    saved = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert saved.get("stop_all_hotkey") == "f11", saved
    print("a stuck worker does not starve the shutdown steps: OK")


def test_shutdown_marks_itself_done_even_when_a_step_raises():
    c = core.Core(inline=False, store_data=config._default_config())

    def broken_stop(_timeout=5.0):
        raise RuntimeError("boom")

    c.workers.stop = broken_stop
    try:
        c.shutdown(timeout=1.0)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the injected failure to propagate")
    assert c._shutdown_state == "done", c._shutdown_state
    assert c._shutdown_result is False
    # a later call must not hang or re-run the sequence - it returns the stored result
    started = time.monotonic()
    second = c.shutdown(timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert second is False
    print("shutdown marks itself done even when an internal step raises: OK")


def test_a_broken_state_part_does_not_break_the_snapshot():
    c, _events = core_fakes.bare_core()
    c.add_state("good", lambda: 1)
    c.add_state("bad", lambda: 1 / 0)
    snapshot = c.state()
    assert snapshot["good"] == 1, snapshot
    assert snapshot["bad"] is None, snapshot
    print("a broken state part does not break the snapshot: OK")


def main():
    test_commands_reach_their_handler()
    test_failures_become_notices_and_the_core_lives_on()
    test_state_parts_and_inline_state_changed()
    test_subscribers_can_leave_and_a_broken_one_hurts_nobody()
    test_save_failure_is_reported()
    test_lifecycle_order_inline()
    test_threaded_core_coalesces_state_and_answers_get_state()
    test_shutdown_waits_for_workers_before_flushing()
    test_shutdown_is_refused_on_the_core_thread_and_idempotent()
    test_get_state_fails_fast_when_the_core_is_gone()
    test_a_stuck_worker_does_not_starve_the_shutdown_steps()
    test_shutdown_marks_itself_done_even_when_a_step_raises()
    test_a_broken_state_part_does_not_break_the_snapshot()
    print("\nALL CORE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
