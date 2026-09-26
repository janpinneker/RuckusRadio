"""Wiedergabe im Testmodus: Kern, Geraete und Worker laufen sofort, Timer per advance()."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-playback-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, dynamics, playback, protocol as p, store  # noqa: E402
from soundboard.layout import needle_for_index  # noqa: E402
import core_fakes  # noqa: E402


def setup(n=2, loaded=True, files=True, integrated=-20.0, engine=None):
    c, events = core_fakes.bare_core(engine=engine)
    service = playback.PlaybackService(c)
    sounds = []
    for i in range(n):
        s = {"id": f"s{i}", "name": f"Sound {i}", "file": f"sounds/s{i}.mp3",
             "icon": f"icons/s{i}.png", "hotkey": None, "volume": 1.0,
             "loudness": {"integrated": integrated, "max_short": integrated, "peak": -3.0}}
        path = Path(_TMP) / s["file"]
        if files:
            path.write_bytes(b"x")
        else:
            path.unlink(missing_ok=True)
        sounds.append(s)
    c.store.data["sounds"] = sounds
    if loaded:
        c.engine.loaded = {s["id"] for s in sounds}
    return c, events, service


def test_play_uses_the_normalized_gain_and_reports_start():
    c, events, service = setup()
    c.send(p.Play("s0"))
    assert c.engine.plays == [("s0", 1.0)], c.engine.plays
    assert core_fakes.of_type(events, p.PlaybackStarted) == [
        p.PlaybackStarted("s0", needle_for_index(0, 2))]
    assert c.state()["playback"] == {"playing": ["s0"], "missing": []}
    c2, _events2, _ = setup(integrated=-26.0)
    c2.send(p.Play("s1", 0.5))
    expected = 0.5 * dynamics.db_to_gain(6.0)
    assert abs(c2.engine.plays[0][1] - expected) < 1e-9, c2.engine.plays
    print("play applies volume x normalization and reports the start: OK")


def test_end_of_playback_is_polled_on_the_device():
    c, events, service = setup()
    c.send(p.Play("s0"))
    c.executor.advance(playback.POLL_S)
    assert core_fakes.of_type(events, p.PlaybackEnded) == [], "still playing"
    c.engine.playing.clear()
    c.executor.advance(playback.POLL_S)
    assert core_fakes.of_type(events, p.PlaybackEnded) == [p.PlaybackEnded("s0")]
    assert service.playing == set()
    c.store.flush()  # a successful play also queues a debounced plays-counter save
    assert c.executor.pending_timers() == 0, "polling stops when nothing plays"
    print("the end of a playback is noticed by polling: OK")


def test_an_undecoded_sound_is_decoded_then_played():
    c, events, service = setup(loaded=False)
    c.send(p.Play("s1", 0.8))
    assert c.engine.preloads == ["s1"]
    assert c.engine.plays == [("s1", 0.8)]
    print("an undecoded sound is decoded on a worker and then played: OK")


def test_a_missing_file_is_reported_and_not_retried():
    c, events, service = setup(loaded=False, files=False)
    c.send(p.Play("s0"))
    assert core_fakes.of_type(events, p.SoundMissing) == [p.SoundMissing("s0")]
    hint = playback.MISSING.format(name="Sound 0")
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [hint]
    assert service.missing == {"s0"}
    c.send(p.Play("s0"))
    assert c.engine.preloads == ["s0"], "a known-missing sound is not decoded again"
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [hint, hint]
    assert c.state()["playback"]["missing"] == ["s0"]
    print("a missing file is reported once per try and not decoded again: OK")


def test_a_device_failure_becomes_a_hint():
    c, events, service = setup()
    c.engine.fail_play = True
    c.send(p.Play("s0"))
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [playback.PLAY_FAILED]
    assert service.playing == set()
    print("a failing device becomes a hint, nothing is marked playing: OK")


def test_a_play_that_waited_too_long_is_dropped():
    c, events, service = setup()
    service._play_on_device("s0", 1.0, 0.0, time.monotonic() - playback.MAX_PLAY_WAIT_S - 1,
                            (0, 0))
    assert c.engine.plays == []
    assert [n.text for n in core_fakes.of_type(events, p.Notice)] == [playback.PLAY_DROPPED]
    print("a play queued behind a long device rebuild is dropped with a hint: OK")


def test_stop_and_stop_all():
    c, events, service = setup()
    c.send(p.Play("s0"))
    c.send(p.Play("s1"))
    c.send(p.Stop("s0"))
    assert c.engine.stop_calls == ["s0"]
    assert core_fakes.of_type(events, p.PlaybackEnded) == [p.PlaybackEnded("s0")]
    c.send(p.StopAll())
    assert c.engine.stopped == 1
    assert core_fakes.of_type(events, p.PlaybackEnded) == [p.PlaybackEnded("s0"),
                                                           p.PlaybackEnded("s1")]
    c.executor.advance(1.0)
    assert len(core_fakes.of_type(events, p.PlaybackEnded)) == 2, "no stale poll afterwards"
    print("stop and stop-all end playback and cancel polling: OK")


def test_results_for_deleted_sounds_are_dropped():
    c, events, service = setup()
    service._decoded("gone", True)
    assert c.engine.forgotten == ["gone"]
    service.forget("s0")
    assert "s0" in c.engine.forgotten
    print("a decode result for a deleted sound is forgotten: OK")


def test_start_preloads_every_sound():
    c, events, service = setup(loaded=False)
    c.start()
    assert sorted(c.engine.preloads) == ["s0", "s1"]
    print("start preloads every sound: OK")


class FlakyPollEngine(core_fakes.FakeEngine):
    """playing_ids() raises once, like a device that hiccups mid-poll."""

    def __init__(self):
        super().__init__()
        self.playing_ids_failures = 0

    def playing_ids(self):
        if self.playing_ids_failures == 0:
            self.playing_ids_failures += 1
            self._record("playing_ids")
            raise RuntimeError("device gone")
        return super().playing_ids()


def test_a_failed_poll_does_not_get_stuck():
    c, events, service = setup(engine=FlakyPollEngine())
    c.send(p.Play("s0"))
    assert service._polling is True
    c.executor.advance(playback.POLL_S)
    # the poll failed: the sound is reported ended and polling stopped instead of
    # staying stuck with the sound marked playing forever
    assert core_fakes.of_type(events, p.PlaybackEnded) == [p.PlaybackEnded("s0")]
    assert service.playing == set()
    assert service._polling is False
    c.store.flush()  # a successful play also queues a debounced plays-counter save
    assert c.executor.pending_timers() == 0
    # a later Play starts polling again
    c.send(p.Play("s0"))
    assert service._polling is True
    print("a failed poll answers the core instead of getting stuck: OK")


def test_device_calls_run_on_the_device_executor():
    c, events, service = setup()
    calls = []
    original = c.devices.submit

    def spy(fn, *args):
        calls.append(getattr(fn, "__name__", repr(fn)))
        original(fn, *args)

    c.devices.submit = spy
    c.send(p.Play("s0"))
    c.executor.advance(playback.POLL_S)
    c.send(p.StopAll())
    assert calls == ["_play_on_device", "_poll_on_device", "stop_all"], calls
    print("play, poll and stop-all go through the device thread: OK")


def test_shutdown_stops_the_engines_own_streams():
    c, events = core_fakes.make_core()
    assert c.engine.stopped == 0
    c.shutdown()
    assert c.engine.stopped > 0, "shutdown must stop the engine's own streams"
    print("shutdown stops the engine's own streams: OK")


def test_started_ignores_a_play_that_was_stopped_before_the_device_answered():
    c, events, service = setup()
    c.devices = core_fakes.QueuedDevices()
    c.send(p.Play("s0"))
    c.send(p.StopAll())
    c.devices.run_all()
    assert core_fakes.of_type(events, p.PlaybackStarted) == [], "stale start must be dropped"
    assert service.playing == set()
    print("StopAll before the device answered drops the stale PlaybackStarted: OK")


def test_started_ignores_a_play_stopped_by_a_single_stop():
    c, events, service = setup()
    c.devices = core_fakes.QueuedDevices()
    c.send(p.Play("s0"))
    c.send(p.Stop("s0"))
    c.devices.run_all()
    assert core_fakes.of_type(events, p.PlaybackStarted) == [], "stale start must be dropped"
    assert service.playing == set()
    print("Stop(sound) before the device answered drops the stale PlaybackStarted: OK")


def test_preview_goes_to_the_headphones_only():
    c, events, service = setup(loaded=False)
    c.send(p.Play("s0", 0.5, preview=True))  # decoded on demand first
    assert c.engine.monitor_only == ["s0"], c.engine.monitor_only
    c.send(p.Play("s1", 0.5))
    assert c.engine.monitor_only == ["s0"], "a normal play reaches every output"
    print("a preview plays on the headphones only, even after a decode: OK")


def test_long_sounds_are_decoded_only_when_played():
    original = playback.LAZY_DECODE_BYTES
    playback.LAZY_DECODE_BYTES = 0  # every 1-byte test file counts as "long"
    try:
        c, events, service = setup(loaded=False)
        service.preload(c.store.data["sounds"])
        assert c.engine.preloads == [], "long sounds are not decoded up front"
        assert service.missing == set(), "skipped is not missing"
        c.send(p.Play("s1"))
        assert c.engine.preloads == ["s1"] and c.engine.plays[-1][0] == "s1"
    finally:
        playback.LAZY_DECODE_BYTES = original
    print("long sounds are decoded on the first play, not at start: OK")


def test_preload_orders_hotkey_then_plays_then_index():
    c, events, service = setup(n=4, loaded=False)
    sounds = c.store.data["sounds"]
    # s0: no hotkey, 0 plays; s1: no hotkey, 5 plays; s2: hotkey, 1 play; s3: no hotkey, 5 plays
    sounds[1]["plays"] = 5
    sounds[2]["hotkey"] = "f1"
    sounds[2]["plays"] = 1
    sounds[3]["plays"] = 5
    service.preload(sounds)
    # hotkeyed first, then by plays descending, ties broken by original list index
    assert c.engine.preloads == ["s2", "s1", "s3", "s0"], c.engine.preloads
    print("preload orders hotkeyed sounds first, then most-played, then list order: OK")


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_a_successful_play_increments_plays_and_a_preview_does_not():
    c, events, service = setup()
    clock = FakeClock()
    service._clock = clock
    sound = c.store.data["sounds"][0]
    assert sound.get("plays", 0) == 0
    c.send(p.Play("s0"))
    assert sound["plays"] == 1, sound
    clock.t += playback.USE_WINDOW_S + 0.5
    c.send(p.Play("s0"))
    assert sound["plays"] == 2
    c.send(p.Play("s1", preview=True))
    assert c.store.data["sounds"][1].get("plays", 0) == 0, "a preview must not count"
    c.store.flush()
    print("a successful play increments plays; a preview does not: OK")


def test_spamming_within_the_use_window_counts_once():
    assert playback.USE_WINDOW_S == 2.0
    c, events, service = setup()
    clock = FakeClock()
    service._clock = clock
    sound = c.store.data["sounds"][0]
    for dt in (0.0, 0.4, 0.5):  # three starts within one second
        clock.t += dt
        c.send(p.Play("s0"))
    assert sound["plays"] == 1, sound
    assert len(core_fakes.of_type(events, p.PlaybackStarted)) == 3, "every click still plays"
    clock.t += 2.5  # a start 2.5 s after the last counted one counts again
    c.send(p.Play("s0"))
    assert sound["plays"] == 2, sound
    c.store.flush()
    print("spamming the same sound within 2 s is one use: OK")


def test_the_window_is_per_sound_and_previews_do_not_open_it():
    c, events, service = setup()
    clock = FakeClock()
    service._clock = clock
    c.send(p.Play("s0", preview=True))  # a preview neither counts nor starts a window
    c.send(p.Play("s0"))
    c.send(p.Play("s1"))
    assert c.store.data["sounds"][0]["plays"] == 1
    assert c.store.data["sounds"][1]["plays"] == 1
    c.store.flush()
    print("the use window is per sound; previews do not open it: OK")


def test_the_plays_counter_is_persisted_debounced_not_on_every_play():
    c, events, service = setup()
    c.store.save_now()  # baseline: config.json exists on disk before any play
    c.send(p.Play("s0"))
    on_disk_before = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert on_disk_before["sounds"][0].get("plays", 0) == 0, \
        "a single play must not write config.json synchronously"
    c.executor.advance(store.SAVE_DEBOUNCE_S)
    on_disk_after = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert on_disk_after["sounds"][0]["plays"] == 1
    print("the plays counter is saved debounced, not synchronously on every play: OK")


def main():
    test_play_uses_the_normalized_gain_and_reports_start()
    test_preview_goes_to_the_headphones_only()
    test_long_sounds_are_decoded_only_when_played()
    test_end_of_playback_is_polled_on_the_device()
    test_an_undecoded_sound_is_decoded_then_played()
    test_a_missing_file_is_reported_and_not_retried()
    test_a_device_failure_becomes_a_hint()
    test_a_play_that_waited_too_long_is_dropped()
    test_stop_and_stop_all()
    test_results_for_deleted_sounds_are_dropped()
    test_start_preloads_every_sound()
    test_a_failed_poll_does_not_get_stuck()
    test_device_calls_run_on_the_device_executor()
    test_shutdown_stops_the_engines_own_streams()
    test_started_ignores_a_play_that_was_stopped_before_the_device_answered()
    test_started_ignores_a_play_stopped_by_a_single_stop()
    test_preload_orders_hotkey_then_plays_then_index()
    test_a_successful_play_increments_plays_and_a_preview_does_not()
    test_spamming_within_the_use_window_counts_once()
    test_the_window_is_per_sound_and_previews_do_not_open_it()
    test_the_plays_counter_is_persisted_debounced_not_on_every_play()
    print("\nALL PLAYBACK LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
