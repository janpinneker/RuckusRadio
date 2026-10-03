"""C8 Zuschnitt ohne Kern: Grenzen, Form, Schnitt auf Samples, Spitzenwerte, Dekodieren
und Export mit echtem ffmpeg an der Fixture (2 s Ton)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-trimming-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from soundboard import audio, trimming  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "test_tone.mp3"


def test_check_trim_enforces_bounds_and_the_minimum():
    ok = trimming.check_trim
    assert ok(0.0, 2.0, 2.0) is None
    assert ok(0.5, 1.5, 2.0) is None
    assert ok(0, 2, 2.0) is None, "ganze Zahlen aus JSON gelten"
    assert ok(0.0, 2.0005, 2.0) is None, "Rundung der Oberflaeche am Ende wird geduldet"
    assert ok(1.0, 1.1, 2.0) is None, "genau 0,1 s ist erlaubt"
    for start, end in ((-0.1, 1.0), (1.0, 1.0), (1.5, 0.5), (0.0, 2.5),
                       (float("nan"), 1.0), (True, 1.0), ("0", 1.0), (None, 1.0)):
        assert ok(start, end, 2.0) == trimming.TRIM_INVALID, (start, end)
    assert ok(1.0, 1.05, 2.0) == trimming.TRIM_TOO_SHORT
    print("Grenzen 0 <= start < end <= Laenge und Mindestlaenge 0,1 s: OK")


def test_clean_trim_keeps_only_a_well_formed_cut():
    assert trimming.clean_trim({"start": 0.5, "end": 1.25}) == {"start": 0.5, "end": 1.25}
    assert trimming.clean_trim({"start": 0.12345, "end": 1}) == {"start": 0.123, "end": 1.0}
    for raw in (None, "x", [], {}, {"start": "0", "end": 1}, {"start": -1, "end": 1},
                {"start": 1, "end": 1.05}, {"start": 1.5, "end": 0.5}):
        assert trimming.clean_trim(raw) is None, raw
    print("clean_trim: kanonische Form oder None: OK")


def test_a_cut_over_the_whole_length_is_no_cut():
    assert trimming.is_full_length(0.0, 2.0, 1.9999)
    assert trimming.is_full_length(0.0005, 1.9995, 2.0)
    assert not trimming.is_full_length(0.1, 2.0, 2.0)
    assert not trimming.is_full_length(0.0, 1.9, 2.0)
    print("ganze Laenge = kein Zuschnitt: OK")


def test_apply_trim_cuts_frames_and_clamps():
    frames = np.arange(4000, dtype=np.float32).reshape(-1, 2)  # 2000 Frames bei 1000 Hz = 2 s
    cut = trimming.apply_trim(frames, 1000, {"start": 0.5, "end": 1.5})
    assert cut.shape == (1000, 2) and cut[0, 0] == frames[500, 0]
    assert not np.shares_memory(cut, frames), "eine Kopie: der Rest darf frei werden"
    assert trimming.apply_trim(frames, 1000, None) is frames
    assert trimming.apply_trim(frames, 1000, {"start": "kaputt"}) is frames
    assert len(trimming.apply_trim(frames, 1000, {"start": 1.5, "end": 9.0})) == 500, "Ende geklemmt"
    assert trimming.apply_trim(frames, 1000, {"start": 5.0, "end": 9.0}) is frames, \
        "Start hinter dem Dateiende: lieber alles als Stille"
    print("apply_trim schneidet, kopiert und klemmt: OK")


def test_peaks_are_min_max_pairs_of_the_mono_mix():
    frames = np.concatenate([np.full((1000, 2), 0.5), np.full((1000, 2), -0.25)]).astype(np.float32)
    assert trimming.peaks(frames, buckets=2) == (0.5, 0.5, -0.25, -0.25)
    stereo = np.array([[1.0, 0.0], [0.0, -1.0], [0.2, 0.2]], dtype=np.float32)
    assert trimming.peaks(stereo, buckets=1000) == (0.5, 0.5, -0.5, -0.5, 0.2, 0.2), \
        "hoechstens ein Paar je Frame"
    assert trimming.peaks(np.zeros((0, 2), dtype=np.float32)) == ()
    assert len(trimming.peaks(np.zeros((96000, 2), dtype=np.float32))) == 2 * trimming.PEAK_BUCKETS
    print("Spitzenwerte aus bekanntem Signal: OK")


def test_decode_applies_the_trim_after_decoding():
    full = audio.decode_audio(FIXTURE)
    cut = audio.decode_audio(FIXTURE, {"start": 0.5, "end": 1.5})
    assert cut.samplerate == 48000 and len(cut.samples) == 48000, len(cut.samples)
    assert np.array_equal(cut.samples, full.samples[24000:72000])
    assert len(audio.decode_audio(FIXTURE, None).samples) == len(full.samples)
    print("decode_audio schneidet nach dem Dekodieren (kein zweiter ffmpeg-Lauf): OK")


def test_export_trimmed_writes_a_new_file_with_the_cut():
    dest = Path(_TMP) / "out" / "kurz.mp3"
    audio.export_trimmed(FIXTURE, dest, {"start": 0.5, "end": 1.5})
    seconds = len(audio.decode_audio(dest).samples) / 48000
    assert 0.95 < seconds < 1.1, seconds  # MP3-Polster: ein paar ms mehr
    whole = Path(_TMP) / "ganz.mp3"
    audio.export_trimmed(FIXTURE, whole, None)
    assert whole.read_bytes() == FIXTURE.read_bytes(), "ohne Zuschnitt: unveraenderte Kopie"
    print("export_trimmed schreibt eine neue Datei: OK")


def test_export_trimmed_with_a_start_past_the_end_exports_the_whole_sound():
    """K5: playback (apply_trim) already prefers the whole sound over silence when a
    stored trim's start is past the file's end (foreign/corrupt pack data) - the export
    must agree, not silently write a near-empty/silent MP3 for the same trim."""
    dest = Path(_TMP) / "export-k5" / "voll.mp3"
    audio.export_trimmed(FIXTURE, dest, {"start": 5.0, "end": 9.0})  # fixture is ~2 s
    seconds = len(audio.decode_audio(dest).samples) / 48000
    assert 1.9 < seconds < 2.1, seconds
    print("export_trimmed mit Start hinter dem Dateiende exportiert den ganzen Sound: OK")


def test_export_trimmed_refuses_to_overwrite_the_source():
    """A regressed guard must never be able to reach the real fixture: both cases run
    against a throwaway copy in the temp dir, and the copy's bytes are checked against
    the untouched fixture afterwards, not just "still exists"."""
    original_bytes = FIXTURE.read_bytes()
    copy_path = Path(_TMP) / "overwrite-guard-copy.mp3"
    copy_path.write_bytes(original_bytes)

    try:
        audio.export_trimmed(copy_path, copy_path, {"start": 0.5, "end": 1.5})
    except ValueError:
        pass
    else:
        raise AssertionError("export must never overwrite the original")
    assert copy_path.stat().st_size == len(original_bytes) and copy_path.read_bytes() == original_bytes, \
        "a rejected export must leave the (copied) source completely untouched"

    # resolved-equal path (different spelling, same file) must also be refused
    indirect = copy_path.parent / ".." / copy_path.parent.name / copy_path.name
    try:
        audio.export_trimmed(copy_path, indirect, None)
    except ValueError:
        pass
    else:
        raise AssertionError("a resolved-equal dest must be refused even spelled differently")
    assert copy_path.read_bytes() == original_bytes, "the copy must stay untouched: still == the fixture"
    print("export_trimmed verweigert das Ueberschreiben der Quelle: OK")


def test_export_trimmed_leaves_no_partial_file_on_failure():
    """The failure must land AFTER the temp file is on disk (a broken source fails before
    any file is written and would never exercise the unlink-on-failure branch): patch
    `os.replace` - the step right after the temp export succeeds - to raise."""
    dest = Path(_TMP) / "failcase" / "kaputt.mp3"
    original_replace = audio.os.replace

    def failing_replace(_src, _dst):
        raise OSError("simulated replace failure")

    audio.os.replace = failing_replace
    try:
        try:
            audio.export_trimmed(FIXTURE, dest, {"start": 0.0, "end": 0.5})
        except OSError:
            pass
        else:
            raise AssertionError("a failing os.replace must propagate")
    finally:
        audio.os.replace = original_replace
    assert not dest.exists(), "no 0-byte/partial dest must remain after a failed export"
    leftovers = list(dest.parent.glob("*")) if dest.parent.exists() else []
    assert leftovers == [], f"no temp files must remain: {leftovers}"
    print("export_trimmed laesst bei einem Fehler keine Datei zurueck: OK")


def test_peaks_does_not_copy_the_buffer_to_float64():
    # Measures the real allocation peak (numpy reports to tracemalloc), whatever call
    # would build the copy. 8 MB stereo float32: the float32 mono mix is 4 MB; any
    # float64 copy of the buffer or of the mono mix needs 8 MB more (probe: 12/24 MB).
    import tracemalloc
    big = np.zeros((1_000_000, 2), dtype=np.float32)
    tracemalloc.start()
    try:
        trimming.peaks(big)
        peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()
    assert peak_mb < 6.0, f"peaks allocated {peak_mb:.1f} MB - a float64 copy of the buffer?"
    print("peaks baut keine float64-Kopie des ganzen Puffers: OK")


def main():
    test_check_trim_enforces_bounds_and_the_minimum()
    test_clean_trim_keeps_only_a_well_formed_cut()
    test_a_cut_over_the_whole_length_is_no_cut()
    test_apply_trim_cuts_frames_and_clamps()
    test_peaks_are_min_max_pairs_of_the_mono_mix()
    test_decode_applies_the_trim_after_decoding()
    test_export_trimmed_writes_a_new_file_with_the_cut()
    test_export_trimmed_with_a_start_past_the_end_exports_the_whole_sound()
    test_export_trimmed_refuses_to_overwrite_the_source()
    test_export_trimmed_leaves_no_partial_file_on_failure()
    test_peaks_does_not_copy_the_buffer_to_float64()
    print("\nALL TRIMMING CHECKS PASSED")


if __name__ == "__main__":
    main()
