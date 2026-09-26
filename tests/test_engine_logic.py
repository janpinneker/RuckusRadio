"""Device resolution (pure), AudioEngine playing-id tracking, stop_all, clip clamp,
partial-open cleanup. sd.OutputStream is replaced by a fake — no real audio."""

import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ["RUCKUS_DATA_DIR"] = tempfile.mkdtemp(prefix="ruckus-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from soundboard import audio, config  # noqa: E402
from soundboard.audio import AudioEngine, DecodedSound  # noqa: E402
from soundboard.devices import resolve_devices  # noqa: E402
from soundboard.virtualmic import MixSource, mix_blocks  # noqa: E402

VM_NAME = "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)"
DEVICES = [
    {"index": 3, "name": "Lautsprecher (Realtek(R) Audio)", "channels": 2},
    {"index": 5, "name": "VoiceMeeter Input (VB-Audio Voi", "channels": 8},  # MME truncates to 31 chars
    {"index": 7, "name": "Kopfhörer (USB Headset)", "channels": 2},
    {"index": 9, "name": VM_NAME, "channels": 8},
]


def cfg(**kw):
    base = {"voicemeeter_device_name": VM_NAME, "monitor_device": "default"}
    base.update(kw)
    return base


def test_resolution():
    r = resolve_devices(DEVICES, cfg(), default_index=3)
    assert r == {"voicemeeter": 9, "monitor": 3,
                "monitor_name": "Lautsprecher (Realtek(R) Audio)", "connected": True}, r

    # configured name not present -> fallback substring "VoiceMeeter Input" (first hit)
    r = resolve_devices(DEVICES, cfg(voicemeeter_device_name="VoiceMeeter Aux Input"), default_index=3)
    assert r["voicemeeter"] == 5 and r["connected"], r

    # case-insensitive
    r = resolve_devices(DEVICES, cfg(voicemeeter_device_name="voicemeeter input (vb-audio voicemeeter vaio)"), 3)
    assert r["voicemeeter"] == 9, r

    # VoiceMeeter missing -> monitor-only
    plain = [d for d in DEVICES if "VoiceMeeter" not in d["name"]]
    r = resolve_devices(plain, cfg(), default_index=3)
    assert r == {"voicemeeter": None, "monitor": 3,
                "monitor_name": "Lautsprecher (Realtek(R) Audio)", "connected": False}, r

    # monitor by name, and unknown name falls back to default
    assert resolve_devices(plain, cfg(monitor_device="Kopfhörer"), 3)["monitor"] == 7
    assert resolve_devices(plain, cfg(monitor_device="Gibt es nicht"), 3)["monitor"] == 3

    # no default device (-1 / None) and nothing configured -> no monitor, no crash
    assert resolve_devices([], cfg(), default_index=-1) == {
        "voicemeeter": None, "monitor": None, "monitor_name": None, "connected": False}
    assert resolve_devices([], cfg(), default_index=None)["monitor"] is None
    # empty / missing name in config -> still falls back
    assert resolve_devices(DEVICES, {"monitor_device": "default"}, 3)["voicemeeter"] == 5
    print("device resolution: OK")


def test_monitor_dropped_when_same_as_voicemeeter():
    # Windows default playback IS the (MME-truncated) VoiceMeeter entry: same physical
    # device as VoiceMeeter, different index than the full-name config match -> drop monitor.
    r = resolve_devices(DEVICES, cfg(), default_index=5)
    assert r == {"voicemeeter": 9, "monitor": None, "monitor_name": None, "connected": True}, r

    # normal headphones default -> monitor kept
    r = resolve_devices(DEVICES, cfg(), default_index=7)
    assert r == {"voicemeeter": 9, "monitor": 7,
                "monitor_name": "Kopfhörer (USB Headset)", "connected": True}, r

    # monitor explicitly configured by name to a VoiceMeeter alias -> also dropped
    r = resolve_devices(DEVICES, cfg(monitor_device="VoiceMeeter Input (VB-Audio Voi"), default_index=7)
    assert r["monitor"] is None, r

    # no VoiceMeeter input resolved at all -> a VoiceMeeter-named default (e.g. the AUX
    # input) is the only output left; keep it instead of ending up with no device
    aux_only = [{"index": 4, "name": "VoiceMeeter Aux Input (VB-Audio", "channels": 8}]
    r = resolve_devices(aux_only, cfg(), default_index=4)
    assert r == {"voicemeeter": None, "monitor": 4,
                "monitor_name": "VoiceMeeter Aux Input (VB-Audio", "connected": False}, r
    print("monitor dropped when it is VoiceMeeter: OK")


def test_voicemeeter_prefers_monitor_hostapi():
    # several devices match the VoiceMeeter name at different host APIs -> prefer the
    # one on the same host API as the (already-resolved) monitor device.
    devices_multi = [
        {"index": 2, "name": "Kopfhörer (USB Headset)", "channels": 2, "hostapi": 0},
        {"index": 5, "name": "VoiceMeeter Input (VB-Audio Voi", "channels": 8, "hostapi": 0},
        {"index": 6, "name": "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", "channels": 8, "hostapi": 3},
    ]
    r = resolve_devices(devices_multi, cfg(voicemeeter_device_name="VoiceMeeter", monitor_device="Kopfhörer"), 2)
    assert r["voicemeeter"] == 5, "must prefer the VoiceMeeter entry on the monitor's host API"

    # no same-host-API VoiceMeeter match -> fall back to the first match
    devices_single_host = [
        {"index": 2, "name": "Kopfhörer (USB Headset)", "channels": 2, "hostapi": 0},
        {"index": 6, "name": "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", "channels": 8, "hostapi": 3},
    ]
    r2 = resolve_devices(devices_single_host, cfg(voicemeeter_device_name="VoiceMeeter", monitor_device="Kopfhörer"), 2)
    assert r2["voicemeeter"] == 6
    print("host-api preference for VoiceMeeter match: OK")


class FakeStream:
    created: list["FakeStream"] = []
    fail_on_device: int | None = None

    def __init__(self, samplerate, device, channels, dtype, callback, finished_callback):
        if device == FakeStream.fail_on_device:
            raise sd.PortAudioError("fake open failure")
        self.device = device
        self.callback = callback
        self.finished_callback = finished_callback
        self.started = self.aborted = self.closed = False
        FakeStream.created.append(self)

    def start(self):
        self.started = True

    def abort(self, ignore_errors=True):
        self.aborted = True
        self.finished_callback()

    def close(self, ignore_errors=True):
        self.closed = True

    def run_to_end(self, frames=256):
        """Drive the callback like PortAudio would until CallbackStop, then finish."""
        out = []
        while True:
            buf = np.full((frames, 2), 9.0, dtype=np.float32)
            try:
                self.callback(buf, frames, None, None)
                out.append(buf)
            except sd.CallbackStop:
                out.append(buf)
                break
        self.finished_callback()
        return np.concatenate(out)


def make_engine(vm=1, mon=2, monitor_volume=0.5):
    engine = AudioEngine(vm, mon, monitor_volume=monitor_volume)
    samples = np.full((1000, 2), 0.8, dtype=np.float32)
    engine._cache["a"] = DecodedSound(samples, 48000)
    engine._cache["b"] = DecodedSound(samples.copy(), 48000)
    return engine


def test_tracking():
    FakeStream.created.clear()
    engine = make_engine()
    assert engine.playing_ids() == set()
    assert engine.is_loaded("a") and not engine.is_loaded("zzz")

    p1 = engine.play("a")
    p2 = engine.play("a")  # overlap, same sound
    p3 = engine.play("b")
    assert len(FakeStream.created) == 6, "two streams (VM + monitor) per play"
    assert all(s.started for s in FakeStream.created)
    assert engine.playing_ids() == {"a", "b"}
    assert engine.is_playing("a") and engine.is_playing("b")

    # finishing one of two "a" playbacks keeps "a" playing
    for s in p1._streams:
        s.stream.run_to_end()
    assert p1.is_finished()
    assert engine.playing_ids() == {"a", "b"}
    assert all(s.stream.closed for s in p1._streams), "finished streams get closed when reaped"

    for s in p3._streams:
        s.stream.run_to_end()
    assert engine.playing_ids() == {"a"}

    for s in p2._streams:
        s.stream.run_to_end()
    assert engine.playing_ids() == set() and not engine.is_playing("a")
    print("playing-id tracking: OK")


def test_stop_all():
    FakeStream.created.clear()
    engine = make_engine()
    engine.play("a")
    engine.play("b")
    engine.stop_all()
    assert engine.playing_ids() == set()
    assert all(s.aborted and s.closed for s in FakeStream.created)
    print("stop_all clears tracking: OK")


def test_gain_and_clip():
    FakeStream.created.clear()
    engine = make_engine(monitor_volume=0.5)
    p = engine.play("a", volume=1.5)  # 0.8 * 1.5 = 1.2 -> clipped to 1.0 on VM
    vm, mon = (s.stream for s in p._streams)
    assert (vm.device, mon.device) == (1, 2)
    vm_out = vm.run_to_end()
    mon_out = mon.run_to_end()
    assert np.isclose(vm_out[:1000].max(), 1.0) and vm_out.max() <= 1.0, vm_out.max()
    assert np.isclose(mon_out[:1000], 0.6).all(), "monitor = 0.8 * 1.5 * 0.5"
    assert (vm_out[1000:] == 0).all(), "tail after the sound is silence"

    neg = AudioEngine(None, 2, monitor_volume=1.0)
    neg._cache["n"] = DecodedSound(np.full((10, 2), -0.9, dtype=np.float32), 48000)
    out = neg.play("n", volume=1.5)._streams[0].stream.run_to_end()
    assert np.isclose(out[:10], -1.0).all(), "negative peaks clamp to -1"
    assert np.isclose(neg._cache["n"].samples, -0.9).all(), "shared buffer must not be modified"
    print("gain + clip clamp: OK")


def test_monitor_only_and_live_volume():
    FakeStream.created.clear()
    engine = make_engine(vm=None, mon=2, monitor_volume=0.5)
    p = engine.play("a")
    assert len(p._streams) == 1 and p._streams[0].stream.device == 2
    engine.monitor_volume = 0.25
    p2 = engine.play("a")
    out = p2._streams[0].stream.run_to_end()
    assert np.isclose(out[:1000], 0.2).all(), "new monitor_volume applies to new plays"
    print("monitor-only + live monitor volume: OK")


def test_partial_open_failure():
    FakeStream.created.clear()
    FakeStream.fail_on_device = 2
    engine = make_engine()
    try:
        engine.play("a")
    except sd.PortAudioError:
        pass
    else:
        raise AssertionError("open failure must propagate to the caller")
    finally:
        FakeStream.fail_on_device = None
    assert len(FakeStream.created) == 1 and FakeStream.created[0].closed, "VM stream must not leak"
    assert engine.playing_ids() == set()
    print("partial open failure cleans up: OK")


def test_stop_single_sound():
    FakeStream.created.clear()
    engine = make_engine()
    engine.play("a")
    engine.play("a")  # overlap
    engine.play("b")
    engine.stop("a")
    assert engine.playing_ids() == {"b"}, "stop(sound_id) must stop only that sound's playbacks"

    a_streams = FakeStream.created[:4]  # 2 overlapping "a" plays x 2 devices
    b_streams = FakeStream.created[4:]
    assert all(s.aborted and s.closed for s in a_streams), "all of a's playbacks must be stopped and closed"
    assert not any(s.aborted or s.closed for s in b_streams), "b must be untouched"

    engine.stop("does-not-exist")  # no matching playback -> no-op, no crash
    assert engine.playing_ids() == {"b"}
    print("stop(sound_id): OK")


def test_unloaded_and_forget():
    engine = make_engine()
    try:
        engine.play("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unloaded sound must raise KeyError")
    engine.forget("a")
    assert not engine.is_loaded("a")
    engine.forget("does-not-exist")
    print("unloaded/forget: OK")


class _MixingSink:
    """Minimal double for whatever `engine.sink` is at runtime (a `sinkgroup.SinkGroup`,
    which owns one Target per destination). This test only needs a single mixed
    destination and does not touch a real device, so a tiny stand-in built from the
    same `MixSource`/`mix_blocks` core the real targets use is enough - and keeps this
    test about AudioEngine's routing, not about SinkGroup/Target (covered on their own
    in test_sinkgroup_logic.py)."""

    def __init__(self):
        self.running = True
        self._sources: list[MixSource] = []

    def add_source(self, samples, gain):
        source = MixSource(samples, gain)
        self._sources.append(source)
        return source

    def remove(self, source):
        source.stop()
        if source in self._sources:
            self._sources.remove(source)
        source.finished.set()

    def _output_callback(self, outdata, frames, time_info, status):
        done = []
        blocks = []
        for source in self._sources:
            chunk = source.pull(frames)
            if chunk is None:
                done.append(source)
            else:
                blocks.append(chunk)
        self._sources = [s for s in self._sources if s not in done]
        outdata[:] = mix_blocks(blocks, frames)


def test_sink_replaces_the_virtual_mic_stream():
    """Cable mode: the sound goes into the mixer, not into an own device stream, so the
    mic can be mixed on top of it. The monitor is just another target inside the sink
    now (Task 5), so it does not get a separate stream either while the sink runs."""
    FakeStream.created.clear()
    sink = _MixingSink()  # already "running"; no real device is opened here
    engine = make_engine(vm=1, mon=2)
    engine.sink = sink

    playback = engine.play("a", volume=1.5)
    assert len(FakeStream.created) == 0, "sink owns every output while running, headphones included"
    assert len(sink._sources) == 1
    source = sink._sources[0]
    assert source.gain == 1.5, "per-sound volume reaches the mixer unscaled"

    out = np.zeros((256, 2), dtype=np.float32)
    sink._output_callback(out, 256, None, None)
    assert np.allclose(out, 1.0), "0.8 * 1.5 clips at 1.0 instead of wrapping"

    engine.stop_all()
    assert source.finished.is_set() and sink._sources == []
    assert playback.is_finished()

    # an idle sink (stopped mixer) must not swallow the sound: fall back to the device
    FakeStream.created.clear()
    sink.running = False
    engine.play("b")
    assert {s.device for s in FakeStream.created} == {1, 2}
    print("sink replaces the virtual mic stream: OK")


class _CountingSink:
    """Stands in for a SinkGroup: counts what the engine hands it."""

    def __init__(self, running=True):
        self.running = running
        self.added = []

    def add_source(self, samples, gain, only=None):
        handle = type("H", (), {"finished": threading.Event()})()
        self.added.append((gain, only))
        return handle

    def remove(self, handle):
        pass


def test_play_leaves_the_monitor_to_the_sink():
    """The headphones are a target inside the group now. If play still opened its own
    monitor stream, every sound would be audible twice and the matrix switch for the
    headphones would do nothing."""
    engine = AudioEngine(voicemeeter_device=None, monitor_device=10,
                         monitor_volume=0.5, sink=_CountingSink())
    engine._cache["s"] = DecodedSound(np.zeros((480, 2), dtype=np.float32), 48000)
    opened = []
    original = audio._StreamPlayback
    audio._StreamPlayback = lambda *a, **kw: opened.append(a) or original(*a, **kw)
    try:
        playback = engine.play("s", volume=1.0)
        playback.stop()
    finally:
        audio._StreamPlayback = original
    assert opened == [], "no direct stream while a sink is running"
    print("play leaves the monitor to the sink: OK")


def test_play_still_uses_the_monitor_without_a_sink():
    """No cable installed at all: the headphones are the only way to hear anything."""
    engine = AudioEngine(voicemeeter_device=None, monitor_device=10,
                         monitor_volume=0.5, sink=None)
    targets = engine._targets_for(volume=1.0)
    assert targets == [(10, 0.5)], targets
    print("play still uses the monitor without a sink: OK")


def test_preview_plays_only_on_the_headphones():
    sink = _CountingSink()
    engine = AudioEngine(voicemeeter_device=1, monitor_device=10, monitor_volume=0.5, sink=sink)
    engine._cache["s"] = DecodedSound(np.zeros((480, 2), dtype=np.float32), 48000)
    engine.play("s", volume=0.7, monitor_only=True).stop()
    engine.play("s", volume=0.7).stop()
    assert sink.added == [(0.7, config.MONITOR_KEY), (0.7, None)], sink.added
    no_sink = AudioEngine(voicemeeter_device=1, monitor_device=10, monitor_volume=0.5, sink=None)
    assert no_sink._targets_for(1.0, monitor_only=True) == [(10, 0.5)]
    assert no_sink._targets_for(1.0) == [(1, 1.0), (10, 0.5)]
    print("a preview plays only on the headphones: OK")


if __name__ == "__main__":
    audio.sd.OutputStream = FakeStream  # never open real devices here
    test_resolution()
    test_monitor_dropped_when_same_as_voicemeeter()
    test_voicemeeter_prefers_monitor_hostapi()
    test_tracking()
    test_stop_all()
    test_stop_single_sound()
    test_gain_and_clip()
    test_monitor_only_and_live_volume()
    test_partial_open_failure()
    test_unloaded_and_forget()
    test_sink_replaces_the_virtual_mic_stream()
    test_play_leaves_the_monitor_to_the_sink()
    test_play_still_uses_the_monitor_without_a_sink()
    test_preview_plays_only_on_the_headphones()
    print("\nALL ENGINE LOGIC CHECKS PASSED")
