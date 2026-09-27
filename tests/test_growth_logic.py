"""Wachstumsmessung: die Kurve muss abflachen, Neuladen zurueck auf die Grundlinie."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-growth-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

from soundboard import measure  # noqa: E402


def reading(count, mb, phase="load"):
    return {"sounds": count, "ram_mb": mb, "phase": phase}


def flat():
    return [reading(0, 300.0), reading(500, 330.0), reading(2000, 351.0),
            reading(2000, 353.0, phase="idle"), reading(2000, 305.0, phase="reload")]


def test_a_flat_curve_passes():
    text = measure.growth_report(flat())
    assert "Bestanden" in text, text
    assert "300" in text and "2000" in text
    print("eine flache Kurve besteht: OK")


def test_growth_with_the_library_fails():
    text = measure.growth_report([reading(0, 300.0), reading(500, 420.0),
                                  reading(2000, 900.0), reading(2000, 905.0, phase="idle"),
                                  reading(2000, 880.0, phase="reload")])
    assert "Nicht bestanden" in text, text
    assert "Bibliothek" in text, text
    print("lineares Wachstum faellt durch: OK")


def test_a_leak_during_idle_fails():
    text = measure.growth_report([reading(0, 300.0), reading(2000, 340.0),
                                  reading(2000, 520.0, phase="idle"),
                                  reading(2000, 345.0, phase="reload")])
    assert "Nicht bestanden" in text and "Leerlauf" in text, text
    print("ein Leck im Leerlauf faellt durch: OK")


def test_a_reload_that_does_not_return_fails():
    text = measure.growth_report([reading(0, 300.0), reading(2000, 340.0),
                                  reading(2000, 342.0, phase="idle"),
                                  reading(2000, 600.0, phase="reload")])
    assert "Nicht bestanden" in text and "Neuladen" in text, text
    print("ein Neuladen ohne Rueckkehr faellt durch: OK")


def test_a_missing_baseline_is_reported():
    text = measure.growth_report([reading(2000, 340.0)])
    assert "Grundlinie" in text, text
    print("ohne Grundlinie wird das gesagt: OK")


def split(count, ui_mb, app_mb, phase="load", **extra):
    """A reading as measure_growth.ps1 writes it: the tree total plus the per-name split."""
    return {"sounds": count, "ram_mb": ui_mb + app_mb, "phase": phase,
            "by_name": [{"name": "msedgewebview2.exe", "count": 6, "mb": ui_mb},
                        {"name": "RuckusRadio.exe", "count": 2, "mb": app_mb}], **extra}


def test_the_app_process_is_reported_not_judged():
    # The interface stays flat; Python grows with the decoded audio cache (by design).
    text = measure.growth_report([split(0, 350.0, 145.0), split(500, 354.0, 295.0),
                                  split(2000, 366.0, 745.0),
                                  split(2000, 368.0, 746.0, phase="idle"),
                                  split(2000, 380.0, 740.0, phase="reload")])
    assert "Bestanden" in text and "Nicht bestanden" not in text, text
    assert "App-Prozess" in text, text
    assert "0.30 MB je Sound" in text, text  # (745 - 145) / 2000
    print("der App-Prozess wird berichtet, nicht beurteilt: OK")


def test_the_interface_growing_with_the_library_fails():
    text = measure.growth_report([split(0, 350.0, 145.0), split(2000, 600.0, 150.0)])
    assert "Nicht bestanden" in text and "Oberfläche" in text, text
    print("waechst die Oberflaeche mit der Bibliothek, faellt es durch: OK")


def test_a_reload_is_judged_on_the_interface():
    # A restart at 2000 sounds decodes the library again: the app sits high, the view not.
    text = measure.growth_report([split(0, 350.0, 145.0), split(2000, 366.0, 745.0),
                                  split(2000, 402.0, 745.0, phase="reload")])
    assert "Bestanden" in text and "Nicht bestanden" not in text, text
    text = measure.growth_report([split(0, 350.0, 145.0), split(2000, 366.0, 745.0),
                                  split(2000, 480.0, 745.0, phase="reload")])
    assert "Nicht bestanden" in text and "Neuladen" in text, text
    print("das Neuladen wird an der Oberflaeche gemessen: OK")


def test_a_process_that_died_during_idle_fails():
    text = measure.growth_report([split(0, 350.0, 145.0), split(2000, 366.0, 745.0),
                                  {"sounds": 2000, "ram_mb": 0.0, "phase": "idle", "by_name": [],
                                   "alive": False, "died_after_s": 900}])
    assert "Nicht bestanden" in text, text
    assert "Leerlauf" in text and "900" in text and "beendet" in text, text
    # An older growth.json has no "alive" flag: an empty tree is the same verdict.
    text = measure.growth_report([split(0, 350.0, 145.0), split(2000, 366.0, 745.0),
                                  {"sounds": 2000, "ram_mb": 0.0, "phase": "idle", "by_name": []}])
    assert "Nicht bestanden" in text and "beendet" in text, text
    print("stirbt der Prozess im Leerlauf, wird das gesagt: OK")


def test_a_reading_taken_while_still_preloading_fails():
    text = measure.growth_report([split(0, 350.0, 145.0),
                                  split(2000, 366.0, 300.0, settled=False)])
    assert "Nicht bestanden" in text and "Vorladen" in text, text
    print("eine Messung mitten im Vorladen gilt nicht: OK")


def test_the_limits_are_named_constants():
    assert measure.GROWTH_LIMIT_MB_PER_1000 == 60.0
    assert measure.IDLE_LIMIT_MB == 40.0
    assert measure.RELOAD_TOLERANCE_MB == 60.0
    print("die Grenzen stehen als Konstanten: OK")


def test_growth_counts_comes_from_the_environment():
    os.environ["RUCKUS_MEASURE_GROWTH_SOUNDS"] = "100, 400 ,quatsch"
    try:
        assert measure.growth_counts() == [100, 400]
    finally:
        os.environ.pop("RUCKUS_MEASURE_GROWTH_SOUNDS", None)
    assert measure.growth_counts() == [500, 2000]
    print("die Soundzahlen kommen aus der Umgebung: OK")


def main():
    test_a_flat_curve_passes()
    test_growth_with_the_library_fails()
    test_a_leak_during_idle_fails()
    test_a_reload_that_does_not_return_fails()
    test_a_missing_baseline_is_reported()
    test_the_app_process_is_reported_not_judged()
    test_the_interface_growing_with_the_library_fails()
    test_a_reload_is_judged_on_the_interface()
    test_a_process_that_died_during_idle_fails()
    test_a_reading_taken_while_still_preloading_fails()
    test_the_limits_are_named_constants()
    test_growth_counts_comes_from_the_environment()
    print("\nALL GROWTH CHECKS PASSED")


if __name__ == "__main__":
    main()
