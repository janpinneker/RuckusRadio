"""Ein eigener Ausgang fuer Netzaufrufe: ffmpeg wird nicht von einer 300-s-Anfrage blockiert."""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-net-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import executors  # noqa: E402


def test_it_runs_work_in_parallel_with_the_workers():
    ran: list[str] = []
    pool = executors.CancellablePool(2)
    try:
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(0.4)
            ran.append("net")

        pool.submit(slow)
        assert started.wait(1.0), "der Netzjob startet"
        deadline = time.monotonic() + 2.0
        while not ran and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ran == ["net"]
    finally:
        pool.stop(2.0)
    print("der Netzausgang laeuft: OK")


def test_a_running_job_sees_the_cancel():
    """Der Abbruchvertrag: ein laufender Job merkt es selbst, zwischen zwei Schritten.

    Der Test aus dem Plan konnte das nicht zeigen - er rief `cancel_all()` **vor** dem
    zweiten `submit()`, und `submit` loescht die Marke fuer den naechsten Lauf. Hier
    laeuft der Job waehrend des Abbruchs, so wie im echten Betrieb."""
    seen = threading.Event()
    gave_up = threading.Event()
    pool = executors.CancellablePool(1)
    try:
        def cooperative():
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if pool.cancelled():
                    seen.set()
                    return
                time.sleep(0.01)
            gave_up.set()

        pool.submit(cooperative)
        time.sleep(0.15)
        pool.cancel_all()
        assert seen.wait(1.0), "ein laufender Job muss den Abbruch sehen"
        assert not gave_up.is_set(), "er gab wegen des Zeitlimits auf, nicht wegen des Abbruchs"
    finally:
        pool.stop(2.0)
    print("ein laufender Job sieht den Abbruch: OK")


def test_a_job_submitted_after_a_cancel_still_runs():
    """Ein Abbruch gilt dem, was gerade laeuft. Wer danach neu anfaengt, darf nicht in
    den alten Abbruch laufen - sonst waere nach jedem Abbrechen jeder weitere Aufruf
    stillschweigend wirkungslos."""
    ran = threading.Event()
    pool = executors.CancellablePool(1)
    try:
        def fresh():
            if not pool.cancelled():
                ran.set()

        pool.cancel_all()
        pool.submit(fresh)
        assert ran.wait(1.0), "nach einem Abbruch muss der naechste Job laufen"
    finally:
        pool.stop(2.0)
    print("ein neuer Job nach dem Abbruch startet frisch: OK")


def test_a_cancel_does_not_wait_for_a_running_job_to_finish():
    finished: list[str] = []
    pool = executors.CancellablePool(1)
    pool.submit(lambda: time.sleep(5.0))
    time.sleep(0.1)
    start = time.monotonic()
    pool.cancel_all()
    elapsed = time.monotonic() - start
    pool.stop(2.0)
    assert elapsed < 1.0, f"cancel_all wartete {elapsed:.2f} s"
    assert finished == []
    print("cancel_all wartet nicht auf den laufenden Job: OK")


def test_the_core_has_its_own_net_egress_in_test_mode():
    c, _ = core_fakes.make_core()
    ran: list[str] = []
    c.net.submit(ran.append, "x")
    assert ran == ["x"], "im Testmodus laeuft der Netzausgang inline"
    assert c.net is not c.workers, "Netz und ffmpeg teilen sich nicht denselben Ausgang"
    print("der Kern hat einen eigenen Netzausgang: OK")


def main():
    test_it_runs_work_in_parallel_with_the_workers()
    test_a_running_job_sees_the_cancel()
    test_a_job_submitted_after_a_cancel_still_runs()
    test_a_cancel_does_not_wait_for_a_running_job_to_finish()
    test_the_core_has_its_own_net_egress_in_test_mode()
    print("\nALL NET EXECUTOR CHECKS PASSED")


if __name__ == "__main__":
    main()
