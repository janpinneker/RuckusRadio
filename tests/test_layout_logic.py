"""Pure layout logic: slot count, search filter, tuner render, data-dir override."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-test-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, layout  # noqa: E402
from soundboard.layout import (  # noqa: E402
    ellipsize,
    filter_sounds,
    keycap_text,
    needle_for_index,
    slots_total,
)
from soundboard.tuner import needle_x, render_tuner  # noqa: E402


def test_data_dir_override():
    d = config.get_app_data_dir()
    assert d == Path(_TMP), d
    assert (d / "sounds").is_dir() and (d / "icons").is_dir()
    assert config.config_path().parent == Path(_TMP)
    print("data dir override: OK")


def test_slots_total():
    expected = {0: 18, 5: 18, 12: 18, 13: 24, 16: 24, 18: 24, 19: 30}
    for n, want in expected.items():
        got = slots_total(n)
        assert got == want, f"slots_total({n}) = {got}, want {want}"
    print("slots_total: OK")


def test_filter_sounds():
    sounds = [{"name": "Airhorn"}, {"name": "Bruh"}, {"name": "AIR raid"}, {"name": "Wow"}]
    assert [s["name"] for s in filter_sounds(sounds, "air")] == ["Airhorn", "AIR raid"]
    assert filter_sounds(sounds, "") == sounds
    assert filter_sounds(sounds, "   ") == sounds
    assert filter_sounds(sounds, " bRu ") == [{"name": "Bruh"}]
    assert filter_sounds(sounds, "xyz") == []
    print("filter_sounds: OK")


def test_ellipsize_and_keycap():
    assert ellipsize("Airhorn", 14) == "Airhorn"
    assert ellipsize("A" * 14, 14) == "A" * 14
    assert ellipsize("Sehr langer Soundname", 14) == "Sehr langer S…"
    assert len(ellipsize("Sehr langer Soundname", 14)) == 14
    assert keycap_text("ctrl+alt+1") == "CTRL+ALT+1"
    assert keycap_text(None) is None
    assert keycap_text("ctrl+ß") == "CTRL+ß", "ß must not become SS"
    assert keycap_text("") is None
    print("ellipsize + keycap_text: OK")


def test_needle_for_index():
    assert needle_for_index(0, 4) == 0.125
    assert needle_for_index(3, 4) == 0.875
    assert needle_for_index(0, 0) == 0.5
    print("needle_for_index: OK")


def test_render_tuner():
    img = render_tuner(800, 110, 0.5)
    assert img.size == (800, 110), img.size
    rgb = img.convert("RGB")
    y = 20  # above the wordmark/scale baseline region, clear of text at x=400
    x = needle_x(800, 0.5)
    assert needle_x(800, 0.0) < needle_x(800, 0.5) < needle_x(800, 1.0) <= 799
    needle = rgb.getpixel((x, y))
    left, right = rgb.getpixel((x - 12, y)), rgb.getpixel((x + 12, y))
    assert needle != left and needle != right, (needle, left, right)
    assert sum(needle) > sum(left) and sum(needle) > sum(right), "needle should be brighter"
    for w, h in [(1, 1), (300, 110), (1600, 110)]:
        assert render_tuner(w, h, 0.0).size == (w, h)
    render_tuner(400, 110, 1.0)
    render_tuner(400, 110, -3)  # clamped
    print("render_tuner: OK")


def test_output_rows_lists_cables_then_monitor():
    cfg = config._default_config()
    config.set_output_settings(cfg, "CABLE Output (VB-Audio Virtual Cable)",
                               sounds_gain=0.9, mic=True)
    resolved = {
        "monitor": 10,
        "monitor_name": "Kopfhörer (KT USB Audio)",
        "virtual_mics": [
            {"key": "CABLE Output (VB-Audio Virtual Cable)", "label": "CABLE",
             "out_index": 29, "in_index": 39,
             "out_name": "CABLE Input (VB-Audio Virtual Cable)",
             "in_name": "CABLE Output (VB-Audio Virtual Cable)"},
        ],
    }
    rows = layout.output_rows(resolved, cfg)
    assert [r["key"] for r in rows] == ["CABLE Output (VB-Audio Virtual Cable)",
                                        config.MONITOR_KEY], rows
    assert rows[0]["label"] == "CABLE"
    assert rows[0]["subtitle"] == "CABLE Output (VB-Audio Virtual Cable)", \
        "the subtitle names the device to pick in Discord"
    assert rows[0]["is_monitor"] is False
    assert abs(rows[0]["settings"]["sounds_gain"] - 0.9) < 1e-9

    monitor = rows[1]
    assert monitor["is_monitor"] is True
    assert monitor["label"] == "Kopfhörer"
    assert monitor["subtitle"] == "Kopfhörer (KT USB Audio)"
    print("output_rows lists cables then monitor: OK")


def test_output_rows_without_monitor():
    rows = layout.output_rows({"monitor": None, "virtual_mics": []}, config._default_config())
    assert rows == [], rows
    print("output_rows without monitor: OK")


def test_output_rows_names_an_unknown_monitor():
    rows = layout.output_rows({"monitor": 10, "virtual_mics": []},
                              config._default_config())
    assert len(rows) == 1 and rows[0]["subtitle"] == "Standardgerät", rows
    print("output_rows names an unknown monitor: OK")


# Hardware finding: the dock's "Prüfen" button used to check ONE recommended cable and
# report its verdict for the whole app - a dead Discord cable could hide behind a
# working CS one. signal_check_summary is the pure decision logic behind the fix: one
# miccheck.verify_path-shaped result per destination the mixer actually opened in,
# green only when every single one passed.
def test_signal_check_summary_all_ok():
    results = [
        {"key": "a", "label": "VB-CABLE", "ok": True, "rms": 0.2, "reason": "x"},
        {"key": "b", "label": "Hi-Fi Cable", "ok": True, "rms": 0.3, "reason": "x"},
    ]
    text, tone = layout.signal_check_summary(results)
    assert tone == "ok", tone
    assert "VB-CABLE" in text and "Hi-Fi Cable" in text, text
    print("signal_check_summary all ok:", text)


def test_signal_check_summary_names_the_failing_destination():
    results = [
        {"key": "a", "label": "VB-CABLE", "ok": True, "rms": 0.2, "reason": "x"},
        {"key": "b", "label": "Hi-Fi Cable", "ok": False, "rms": 0.0, "reason": "x"},
    ]
    text, tone = layout.signal_check_summary(results)
    assert tone == "warn", tone
    assert "Hi-Fi Cable" in text, text
    assert "VB-CABLE" not in text, f"must not blame the destination that passed: {text}"
    print("signal_check_summary names the failing destination:", text)


def test_signal_check_summary_every_destination_failing():
    results = [
        {"key": "a", "label": "VB-CABLE", "ok": False, "rms": 0.0, "reason": "x"},
        {"key": "b", "label": "Hi-Fi Cable", "ok": False, "rms": 0.0, "reason": "x"},
    ]
    text, tone = layout.signal_check_summary(results)
    assert tone == "warn", tone
    assert "VB-CABLE" in text and "Hi-Fi Cable" in text, text
    print("signal_check_summary every destination failing:", text)


def test_signal_check_summary_single_destination():
    ok_text, ok_tone = layout.signal_check_summary(
        [{"key": "a", "label": "VB-CABLE", "ok": True, "rms": 0.2, "reason": "x"}])
    assert ok_tone == "ok" and "VB-CABLE" in ok_text, (ok_text, ok_tone)
    bad_text, bad_tone = layout.signal_check_summary(
        [{"key": "a", "label": "VB-CABLE", "ok": False, "rms": 0.0, "reason": "x"}])
    assert bad_tone == "warn" and "VB-CABLE" in bad_text, (bad_text, bad_tone)
    print("signal_check_summary single destination: OK")


def test_signal_check_summary_empty():
    text, tone = layout.signal_check_summary([])
    assert tone == "off", tone
    print("signal_check_summary empty:", text)


def test_microphone_hint_recommends_broadcast():
    endorfy = "Mikrofon (Endorfy Solum Voice S Mic)"
    broadcast = "Mikrofon (NVIDIA Broadcast)"
    assert "gefunden" in layout.microphone_hint([endorfy, broadcast], endorfy)
    assert "aktiv" in layout.microphone_hint([endorfy, broadcast], broadcast)
    assert "Tipp" in layout.microphone_hint([endorfy], endorfy)
    print("microphone hint recommends NVIDIA Broadcast: OK")


if __name__ == "__main__":
    test_data_dir_override()
    test_slots_total()
    test_filter_sounds()
    test_ellipsize_and_keycap()
    test_needle_for_index()
    test_render_tuner()
    test_output_rows_lists_cables_then_monitor()
    test_output_rows_without_monitor()
    test_output_rows_names_an_unknown_monitor()
    test_signal_check_summary_all_ok()
    test_signal_check_summary_names_the_failing_destination()
    test_signal_check_summary_every_destination_failing()
    test_signal_check_summary_single_destination()
    test_signal_check_summary_empty()
    test_microphone_hint_recommends_broadcast()
    print("\nALL LAYOUT LOGIC CHECKS PASSED")
