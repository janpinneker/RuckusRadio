"""Ein Fortschrittsbegriff fuer alle langen Arbeiten."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-jobs-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import jobs as jobs_module  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def wire():
    """Der fertige Kern, wie create_core() ihn baut: das Job-Service ist dort schon
    verdrahtet. Ein zweites hier anzulegen wuerde an add_state("jobs") scheitern -
    genau das ist der Beweis, dass die Einbindung passiert ist."""
    c, events = core_fakes.make_core()
    return c, events, c.jobs


def test_a_job_runs_from_start_to_finish():
    c, events, service = wire()
    job_id = service.start("voicebox", "Stimme erzeugen")
    assert job_id
    assert service.snapshot()["jobs"][0]["state"] == "running"
    assert service.snapshot()["jobs"][0]["label"] == "Stimme erzeugen"
    service.progress(job_id, fraction=0.5, text="Modell lädt")
    service.finish(job_id, ok=True, text="Fertig")
    assert service.snapshot()["jobs"][0]["state"] == "done"
    kinds = [e.job["state"] for e in core_fakes.of_type(events, p.JobChanged)]
    assert kinds == ["running", "running", "done"], kinds
    print("ein Job laeuft von Anfang bis Ende: OK")


def test_a_finished_job_is_kept_briefly_then_dropped():
    c, events, service = wire()
    job_id = service.start("update", "Update laden")
    service.finish(job_id, ok=False, text="Kein Netz")
    assert service.snapshot()["jobs"][0]["ok"] is False
    service.prune()
    assert service.snapshot()["jobs"] == []
    print("erledigte Jobs werden weggeraeumt: OK")


def test_the_oldest_finished_job_goes_first_when_the_list_grows():
    c, events, service = wire()
    ids = [service.start("voicebox", f"Job {i}") for i in range(jobs_module.MAX_JOBS + 3)]
    for job_id in ids:
        service.finish(job_id, ok=True)
    kept = [j["id"] for j in service.snapshot()["jobs"]]
    assert len(kept) == jobs_module.MAX_JOBS, len(kept)
    assert ids[0] not in kept and ids[-1] in kept
    print("die Liste waechst nicht unbegrenzt: OK")


def test_cancel_marks_the_job_and_calls_the_hook():
    c, events, service = wire()
    called: list[str] = []
    job_id = service.start("voicebox", "Stimme erzeugen", on_cancel=lambda: called.append(job_id))
    assert service.cancel(job_id) is True
    assert called == [job_id]
    assert service.snapshot()["jobs"][0]["state"] == "cancelled"
    assert service.cancel(job_id) is False, "zweimal abbrechen ist kein Fehler, aber ohne Wirkung"
    print("Abbruch markiert den Job und ruft den Haken: OK")


def test_progress_on_an_unknown_job_is_harmless():
    c, events, service = wire()
    service.progress("gibtsnicht", fraction=0.5)
    service.finish("gibtsnicht", ok=True)
    assert service.snapshot()["jobs"] == []
    print("Fortschritt an einem unbekannten Job tut nichts: OK")


def test_jobs_live_in_the_volatile_group():
    c, events, service = wire()
    assert "jobs" in c.state("volatile")
    assert "jobs" not in c.state("library")
    service.start("voicebox", "x")
    assert core_fakes.of_type(events, p.PartChanged) == [], \
        "Jobs gehoeren in den fluechtigen Teil, nicht in eine eigene Gruppe"
    print("Jobs liegen im fluechtigen Teil: OK")


def test_shutdown_cancels_running_jobs():
    c, events, service = wire()
    called: list[str] = []
    job_id = service.start("voicebox", "lang", on_cancel=lambda: called.append("x"))
    c.shutdown()
    assert called == ["x"], "ein laufender Job muss beim Beenden abgebrochen werden"
    assert service.get(job_id)["state"] == "cancelled"
    print("das Beenden bricht laufende Jobs ab: OK")


def main():
    test_a_job_runs_from_start_to_finish()
    test_a_finished_job_is_kept_briefly_then_dropped()
    test_the_oldest_finished_job_goes_first_when_the_list_grows()
    test_cancel_marks_the_job_and_calls_the_hook()
    test_progress_on_an_unknown_job_is_harmless()
    test_jobs_live_in_the_volatile_group()
    test_shutdown_cancels_running_jobs()
    print("\nALL JOBS CHECKS PASSED")


if __name__ == "__main__":
    main()
