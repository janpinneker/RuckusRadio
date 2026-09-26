"""Ausfuehrer: Reihenfolge, Timer, Fehlerhuelle, Beenden - mit echten Threads."""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-executors-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import executors  # noqa: E402


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.005)


def test_serial_runs_in_order_on_its_own_thread():
    ex = executors.SerialExecutor("test-serial")
    seen = []
    for i in range(50):
        ex.submit(lambda i=i: seen.append((i, threading.current_thread().name)))
    wait_until(lambda: len(seen) == 50)
    assert [i for i, _ in seen] == list(range(50))
    assert {name for _, name in seen} == {"test-serial"}
    assert not ex.is_current()
    assert ex.stop()
    print("serial executor keeps order on one named thread: OK")


def test_serial_survives_a_failing_task():
    errors = []
    ex = executors.SerialExecutor("test-errors", on_error=errors.append)
    done = []
    ex.submit(lambda: 1 / 0)
    ex.submit(lambda: done.append(True))
    wait_until(lambda: done)
    assert len(errors) == 1 and isinstance(errors[0], ZeroDivisionError), errors
    assert ex.alive
    assert ex.stop()
    print("a failing task is reported and the thread lives on: OK")


def test_serial_timers_fire_in_due_order_and_cancel():
    ex = executors.SerialExecutor("test-timers")
    seen = []
    ex.call_later(0.06, seen.append, "late")
    ex.call_later(0.01, seen.append, "early")
    cancelled = ex.call_later(0.03, seen.append, "cancelled")
    cancelled.cancel()
    ex.submit(seen.append, "now")
    wait_until(lambda: len(seen) == 3)
    time.sleep(0.05)
    assert seen == ["now", "early", "late"], seen
    assert ex.stop()
    print("timers fire in due order, cancelled ones never: OK")


def test_serial_stop_runs_queued_work_first():
    ex = executors.SerialExecutor("test-stop")
    seen = []
    gate = threading.Event()
    ex.submit(gate.wait)
    for i in range(5):
        ex.submit(seen.append, i)
    gate.set()
    assert ex.stop(timeout=3.0)
    assert seen == [0, 1, 2, 3, 4], seen
    assert not ex.alive
    ex.submit(seen.append, "after stop")  # must not raise and must not run
    assert seen == [0, 1, 2, 3, 4]
    print("stop runs what was queued, then ends; later submits are ignored: OK")


def test_inline_runs_now_and_timers_on_advance():
    errors = []
    ex = executors.InlineExecutor(on_error=errors.append)
    seen = []
    ex.submit(seen.append, "now")
    assert seen == ["now"]
    ex.call_later(1.0, seen.append, "one")
    ex.call_later(0.5, lambda: ex.call_later(0.2, seen.append, "chained"))
    t = ex.call_later(0.8, seen.append, "cancelled")
    t.cancel()
    assert ex.pending_timers() == 2
    ex.advance(0.6)
    assert seen == ["now"] and ex.pending_timers() == 2
    ex.advance(0.5)
    assert seen == ["now", "chained", "one"], seen
    assert abs(ex.now - 1.1) < 1e-9
    ex.submit(lambda: 1 / 0)
    assert len(errors) == 1
    assert ex.is_current() and ex.alive and ex.stop()
    print("inline executor runs now, timers only on advance(): OK")


def test_worker_pool_runs_in_parallel_and_reports_errors():
    errors = []
    pool = executors.WorkerPool(workers=2, on_error=errors.append)
    both = threading.Barrier(2, timeout=3.0)
    names = []

    def job():
        names.append(threading.current_thread().name)
        both.wait()  # only passes when two jobs run at the same time

    pool.submit(job)
    pool.submit(job)
    pool.submit(lambda: 1 / 0)
    wait_until(lambda: len(names) == 2 and errors)
    assert all(n.startswith("ruckus-worker") for n in names), names
    assert isinstance(errors[0], ZeroDivisionError)
    assert pool.stop(timeout=3.0)
    print("worker pool runs jobs in parallel and reports errors: OK")


def test_serial_on_start_runs_first_on_its_own_thread():
    seen = []
    ex = executors.SerialExecutor(
        "test-on-start", on_start=lambda: seen.append(("start", threading.current_thread().name)))
    ex.submit(lambda: seen.append(("task", threading.current_thread().name)))
    wait_until(lambda: len(seen) == 2)
    assert seen == [("start", "test-on-start"), ("task", "test-on-start")], seen
    ex.stop()


def test_serial_survives_a_failing_on_start():
    errors, seen = [], []

    def boom():
        raise RuntimeError("no COM")

    ex = executors.SerialExecutor("test-on-start-fail", errors.append, on_start=boom)
    ex.submit(lambda: seen.append(1))
    wait_until(lambda: seen == [1])
    assert len(errors) == 1 and "no COM" in str(errors[0])
    ex.stop()


def main():
    test_serial_runs_in_order_on_its_own_thread()
    test_serial_survives_a_failing_task()
    test_serial_timers_fire_in_due_order_and_cancel()
    test_serial_stop_runs_queued_work_first()
    test_inline_runs_now_and_timers_on_advance()
    test_worker_pool_runs_in_parallel_and_reports_errors()
    test_serial_on_start_runs_first_on_its_own_thread()
    test_serial_survives_a_failing_on_start()
    print("\nALL EXECUTORS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
