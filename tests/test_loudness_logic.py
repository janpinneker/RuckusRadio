"""Lautheitsmessung: Parser gegen echte ffmpeg-Ausgabe, Messung gegen die Fixture."""

import os
import sys
import tempfile
import types
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-loudness-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import loudness, paths  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Real stderr shape of `ffmpeg -af ebur128=peak=true:framelog=info` (shortened).
EBUR128_OUTPUT = """\
[Parsed_ebur128_0 @ 00000230092e8e40] t: 0.0999773  TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS       LRA:   0.0 LU  FTPK: -29.6 -23.6 dBFS  TPK: -29.6 -23.6 dBFS
[Parsed_ebur128_0 @ 00000230092e8e40] t: 3.0999773  TARGET:-23 LUFS    M: -11.2 S: -12.5     I: -11.9 LUFS       LRA:   1.0 LU  FTPK: -0.4 -0.3 dBFS  TPK: -0.2 -0.1 dBFS
[Parsed_ebur128_0 @ 00000230092e8e40] t: 3.1999773  TARGET:-23 LUFS    M:  -9.1 S:  -9.8     I: -11.1 LUFS       LRA:   2.0 LU  FTPK: -0.1 -0.2 dBFS  TPK: -0.1 -0.1 dBFS
[Parsed_ebur128_0 @ 0000023454b6bb00] Summary:

  Integrated loudness:
    I:         -10.8 LUFS
    Threshold: -21.9 LUFS

  Loudness range:
    LRA:         3.6 LU
    Threshold: -31.8 LUFS
    LRA low:   -13.7 LUFS
    LRA high:  -10.1 LUFS

  True peak:
    Peak:        0.0 dBFS
"""


def test_parse_reads_summary_and_loudest_short_term():
    result = loudness.parse_ebur128(EBUR128_OUTPUT)
    assert result == loudness.Loudness(integrated=-10.8, max_short=-9.8, peak=0.0), result
    print("parser reads integrated, max short-term and peak: OK")


def test_parse_without_summary_is_none():
    assert loudness.parse_ebur128("Error opening input file") is None
    print("parser returns None without a summary: OK")


def test_parse_ignores_the_silent_warmup():
    text = EBUR128_OUTPUT.replace("S: -12.5", "S:-120.7").replace("S:  -9.8", "S:-120.7")
    result = loudness.parse_ebur128(text)
    assert result.max_short == result.integrated, "no real short-term value: fall back to I"
    print("parser ignores the silent warm-up frames: OK")


def test_ffmpeg_is_found():
    assert paths.ffmpeg_path() is not None, "assets\\ffmpeg.exe or ffmpeg on PATH is required"
    print("ffmpeg found:", paths.ffmpeg_path())


def test_analyze_measures_the_fixture():
    result = loudness.analyze(FIXTURES / "test_tone.mp3")
    assert result is not None
    assert -70.0 < result.integrated < 0.0, result
    print("analyze measures a real file:", result)


def test_parse_reads_the_length_from_the_last_frame():
    """Klangbild K2: die Laenge kommt aus dem letzten Zeitstempel `t:` (100-ms-Frames).
    Geprueft an EBUR128_OUTPUT: letztes t 3.1999773."""
    assert abs(loudness.parse_duration(EBUR128_OUTPUT) - 3.1999773) < 1e-9
    assert loudness.parse_duration("Error opening input file") is None
    assert abs(loudness.parse_ebur128(EBUR128_OUTPUT).duration - 3.1999773) < 1e-9
    print("parser reads the length from the last frame: OK")


def test_measure_dict_marks_failures():
    """A missing file (or no ffmpeg, a timeout, OSError/SubprocessError) is transient or
    environmental - the sound must be retried later, not pinned at a permanent failure
    marker, so it returns None. Only ffmpeg actually running on an existing file and
    rejecting it (nonzero exit, unparsable output) is a real, permanent failure."""
    assert loudness.measure_dict(Path(_TMP) / "missing.mp3") is None
    bogus = Path(_TMP) / "not_really_audio.mp3"
    bogus.write_text("this is plain text, not an mp3", encoding="utf-8")
    assert loudness.measure_dict(bogus) == {"failed": True}
    good = loudness.measure_dict(FIXTURES / "test_tone.mp3")
    assert set(good) == {"integrated", "max_short", "peak", "duration"}, good
    assert abs(good["duration"] - 2.0) < 0.15, good
    print("measure_dict distinguishes a missing/transient result (None) from a real "
          "failure ({'failed': True}): OK")


def test_a_trim_measures_only_the_cut():
    """C8: after a cut the loudness must be measured on exactly the part that plays."""
    seen = []
    original = loudness.subprocess.run

    def fake_run(command, **_kwargs):
        seen.append(list(command))
        return types.SimpleNamespace(returncode=0, stderr=EBUR128_OUTPUT)

    loudness.subprocess.run = fake_run
    try:
        loudness.analyze(FIXTURES / "test_tone.mp3", ffmpeg="ffmpeg-fake",
                         trim={"start": 0.5, "end": 1.5})
        loudness.analyze(FIXTURES / "test_tone.mp3", ffmpeg="ffmpeg-fake")
    finally:
        loudness.subprocess.run = original
    cut, whole = seen
    assert cut[cut.index("-ss") + 1] == "0.500" and cut[cut.index("-t") + 1] == "1.000", cut
    assert cut.index("-ss") < cut.index("-i"), "Eingangs-Suche vor -i"
    assert "-ss" not in whole and "-t" not in whole
    real = loudness.measure_dict(FIXTURES / "test_tone.mp3", {"start": 0.5, "end": 1.5})
    assert set(real) == {"integrated", "max_short", "peak"}, real
    print("mit Zuschnitt wird nur der Schnitt gemessen: OK")


def test_a_broken_stored_trim_falls_back_to_the_whole_file_with_its_duration():
    """F2: measure_dict must decide whether it carries "duration" by whether the trim
    was actually usable (trimming.clean_trim(trim) is None), not by whether `trim`
    itself was None. A broken stored trim (hand-edited config) makes clean_trim return
    None, so the whole file is measured and its "duration" belongs in the result too -
    same as passing no trim at all."""
    broken = loudness.measure_dict(FIXTURES / "test_tone.mp3", {"start": "x"})
    assert set(broken) == {"integrated", "max_short", "peak", "duration"}, broken
    print("ein kaputter gespeicherter Zuschnitt liefert die volle Laenge mit: OK")


def test_parse_treats_silence_as_silent_not_failed():
    """Silence (integrated -inf) should be treated as SILENT_LUFS, not as a failed parse."""
    summary_text = """\
[Parsed_ebur128_0 @ 0000023454b6bb00] Summary:

  Integrated loudness:
    I:         -inf LUFS
    Threshold: -21.9 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold: -31.8 LUFS
    LRA low:   -13.7 LUFS
    LRA high:  -10.1 LUFS

  True peak:
    Peak:       -inf dBFS
"""
    result = loudness.parse_ebur128(summary_text)
    assert result == loudness.Loudness(integrated=-70.0, max_short=-70.0, peak=loudness.NO_PEAK_DB), result
    print("parser treats silence as silent, not failed: OK")


def test_pydub_starts_ffmpeg_without_a_console_window():
    import subprocess
    from pydub import AudioSegment
    paths.configure_ffmpeg()
    seen: list[int] = []
    real_init = subprocess.Popen.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        real_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = spy
    try:
        AudioSegment.from_file(str(FIXTURES / "test_tone.mp4"))  # ffprobe + ffmpeg
    finally:
        subprocess.Popen.__init__ = real_init
    assert seen, "pydub started no process"
    assert all(flags & subprocess.CREATE_NO_WINDOW for flags in seen), seen
    print("pydub runs ffmpeg/ffprobe without console windows: OK")


def main():
    test_parse_reads_the_length_from_the_last_frame()
    test_parse_reads_summary_and_loudest_short_term()
    test_parse_without_summary_is_none()
    test_parse_ignores_the_silent_warmup()
    test_parse_treats_silence_as_silent_not_failed()
    test_ffmpeg_is_found()
    test_analyze_measures_the_fixture()
    test_measure_dict_marks_failures()
    test_a_trim_measures_only_the_cut()
    test_a_broken_stored_trim_falls_back_to_the_whole_file_with_its_duration()
    test_pydub_starts_ffmpeg_without_a_console_window()
    print("\nALL LOUDNESS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
