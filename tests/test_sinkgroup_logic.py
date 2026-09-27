"""SinkGroup und Target rein logisch: kein echtes Geraet, keine PortAudio-Streams.

Target._open_stream wird durch einen Attrappen-Stream ersetzt, der den Callback
merkt, statt ihn von einem Audio-Thread rufen zu lassen. Die Tests rufen den
Callback dann selbst auf, damit die Mischung deterministisch pruefbar ist."""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

_TMP = tempfile.mkdtemp(prefix="ruckus-sinkgroup-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, dynamics, sinkgroup, virtualmic  # noqa: E402

FRAMES = virtualmic.BLOCKSIZE


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def abort(self, ignore_errors=True):
        pass

    def close(self, ignore_errors=True):
        self.closed = True


def fake_target(key, settings, fail=False):
    target = sinkgroup.Target(key, key, device=0, settings=settings)
    if fail:
        def boom():
            raise RuntimeError("device busy")
        target._open_stream = boom
    else:
        target._open_stream = lambda: FakeStream(target._output_callback)
    return target


def start_with_mic(group):
    """start() plus: tu so, als laufe ein Mikrofonstream.

    Die Tests bauen die Gruppe ohne echtes Geraet (`mic_device=None`), also schaltet
    `start()` den Mikrofonzweig der Ziele ab. Wer Mikrobloecke prueft, muss ihn wieder
    anschalten - sonst liest der Callback die Queue nie und der Test wuerde aus dem
    falschen Grund gruen oder rot."""
    group.start()
    group.mic_active = True
    for target in group.targets:
        target._mic_wanted = True
    return group


def pull(target):
    """Run one output callback and return what the target wrote."""
    out = np.zeros((FRAMES, virtualmic.TARGET_CHANNELS), dtype=np.float32)
    target._output_callback(out, FRAMES, None, None)
    return out


def settings(**kw):
    base = {"mic": True, "mic_gain": 1.0, "sounds": True, "sounds_gain": 1.0}
    base.update(kw)
    return base


def tone(value=0.5, frames=FRAMES):
    return np.full((frames, virtualmic.TARGET_CHANNELS), value, dtype=np.float32)


def test_mic_block_reaches_every_target():
    a = fake_target("a", settings())
    b = fake_target("b", settings())
    group = sinkgroup.SinkGroup([a, b], mic_device=None)
    start_with_mic(group)
    group._distribute_mic(tone(0.25))
    assert abs(pull(a)[0, 0] - 0.25) < 1e-6
    assert abs(pull(b)[0, 0] - 0.25) < 1e-6, "one target must not eat another's block"
    group.stop()
    print("mic block reaches every target: OK")


def test_each_target_mixes_its_own_way():
    both = fake_target("both", settings(mic_gain=0.5, sounds_gain=1.0))
    voice = fake_target("voice", settings(sounds=False))
    quiet = fake_target("quiet", settings(mic=False, sounds_gain=0.5))
    group = sinkgroup.SinkGroup([both, voice, quiet], mic_device=None)
    start_with_mic(group)
    group.add_source(tone(0.4), gain=1.0)
    group._distribute_mic(tone(0.2))

    assert abs(pull(both)[0, 0] - (0.2 * 0.5 + 0.4)) < 1e-6, "mic at half plus the sound"
    assert abs(pull(voice)[0, 0] - 0.2) < 1e-6, "sounds off: voice only"
    assert abs(pull(quiet)[0, 0] - 0.4 * 0.5) < 1e-6, "mic off, sound at half"
    group.stop()
    print("each target mixes its own way: OK")


def test_preview_reaches_only_the_headphones_and_volume_reaches_the_cable():
    cable = fake_target("cable", settings(mic=False))
    phones = fake_target(config.MONITOR_KEY, settings(mic=False))
    group = sinkgroup.SinkGroup([cable, phones], mic_device=None)
    group.start()
    group.add_source(tone(0.4), gain=0.5, only=config.MONITOR_KEY)
    assert abs(pull(cable)[0, 0]) < 1e-9, "the voice chat never hears a preview"
    assert abs(pull(phones)[0, 0] - 0.2) < 1e-6, "the headphones hear it at the preview volume"
    group.add_source(tone(0.4), gain=0.25)
    assert abs(pull(cable)[0, 0] - 0.1) < 1e-6, "a normal play's volume reaches the cable too"
    group.stop()
    print("preview only in the headphones, play volume reaches the cable: OK")


def test_group_source_finishes_when_all_targets_finish():
    a = fake_target("a", settings())
    b = fake_target("b", settings())
    group = sinkgroup.SinkGroup([a, b], mic_device=None)
    group.start()
    short = tone(0.3, frames=FRAMES // 2)
    handle = group.add_source(short, gain=1.0)
    assert not handle.finished.is_set()
    pull(a)
    assert not handle.finished.is_set(), "one target done is not all targets done"
    pull(b)
    assert handle.finished.is_set(), "both done means the playback is over"
    # No poll() anywhere: finished asks the sources, so AudioEngine._reap sees it.
    group.stop()
    print("group source finishes when all targets finish: OK")


def test_full_queue_only_hurts_its_own_target():
    slow = fake_target("slow", settings())
    fast = fake_target("fast", settings())
    group = sinkgroup.SinkGroup([slow, fast], mic_device=None)
    start_with_mic(group)
    for _ in range(virtualmic.MIC_QUEUE_BLOCKS + 3):
        group._distribute_mic(tone(0.1))
    assert slow.dropped_blocks > 0, "the full queue drops its oldest blocks"
    assert slow.dropped_blocks == fast.dropped_blocks, "both are full, both drop"
    # Draining one must not change the other.
    pull(fast)
    assert slow._mic_queue.qsize() == virtualmic.MIC_QUEUE_BLOCKS
    group.stop()
    print("full queue only hurts its own target: OK")


def test_starved_target_writes_silence_not_garbage():
    a = fake_target("a", settings())
    group = sinkgroup.SinkGroup([a], mic_device=None)
    start_with_mic(group)
    out = pull(a)
    assert a.starved_blocks == 1, a.starved_blocks
    assert float(np.abs(out).max()) == 0.0, "no mic block means silence"
    group.stop()
    print("starved target writes silence: OK")


def test_failing_target_does_not_stop_the_group():
    broken = fake_target("broken", settings(), fail=True)
    fine = fake_target("fine", settings())
    group = sinkgroup.SinkGroup([broken, fine], mic_device=None)
    start_with_mic(group)
    assert group.running is True
    assert broken.open_failed is True
    assert fine.open_failed is False
    group._distribute_mic(tone(0.3))
    assert abs(pull(fine)[0, 0] - 0.3) < 1e-6
    group.stop()
    print("failing target does not stop the group: OK")


def test_running_stays_true_when_every_target_is_off():
    """AudioEngine.play falls back to its direct path when running is False, which
    would write into the cable behind the user's back. running describes the group."""
    a = fake_target("a", settings(mic=False, sounds=False))
    group = sinkgroup.SinkGroup([a], mic_device=None)
    group.start()
    assert a.wants_stream is False, "nothing to send means no open stream"
    assert group.running is True, "the group is alive even with every target switched off"
    group.stop()
    assert group.running is False
    print("running stays true when every target is off: OK")


def test_apply_reopens_a_switched_on_target():
    a = fake_target("a", settings(mic=False, sounds=False))
    group = sinkgroup.SinkGroup([a], mic_device=None)
    group.start()
    assert a._stream is None
    group.apply("a", settings(mic=True, sounds=True))
    assert a._stream is not None, "switching something on opens the stream again"
    group.apply("a", settings(mic=False, sounds=False))
    assert a._stream is None, "switching everything off closes it"
    group.stop()
    print("apply reopens a switched-on target: OK")


def test_missing_mic_leaves_the_sounds_running():
    """Spec-Fehlerfall: Mikrofon fehlt oder ist belegt. Die Sounds muessen trotzdem
    durchgehen - sonst nimmt ein belegtes Mikro dem ganzen Soundboard die Stimme weg."""
    a = fake_target("a", settings())
    group = sinkgroup.SinkGroup([a], mic_device=None)
    group.start()  # kein Mikro: bewusst NICHT start_with_mic
    assert group.mic_active is False
    assert group.running is True
    group.add_source(tone(0.6), gain=1.0)
    assert abs(pull(a)[0, 0] - 0.6) < 1e-6, "sounds keep flowing without a microphone"
    assert a.starved_blocks == 0, "a mic that was never opened must not count as starved"
    group.stop()
    print("missing mic leaves the sounds running: OK")


def test_muted_target_writes_silence_without_inflating_starved_blocks():
    """Regression guard for the Critical finding: SinkGroup._distribute_mic() only
    guards the INPUT side (`if self.mic_muted: return`), so without a matching guard
    on the OUTPUT side, Target._output_callback keeps calling get_nowait() on a queue
    nothing refills and starved_blocks climbs forever while muted - deliberate silence
    would look identical to a mic/clock drift fault. Mirrors
    test_starved_target_writes_silence_not_garbage's pattern for asserting on the
    starved counter."""
    a = fake_target("a", settings())
    group = sinkgroup.SinkGroup([a], mic_device=None)
    start_with_mic(group)
    pull(a)  # drain any startup state so the counter below starts clean
    group.set_mic_muted(True)

    before = a.starved_blocks
    for _ in range(5):
        out = pull(a)
        assert float(np.abs(out).max()) == 0.0, "muted target must write silence"
    assert a.starved_blocks == before, (
        "starved_blocks must not climb while muted: it means drift, not deliberate mute"
    )
    group.stop()
    print("muted target writes silence without inflating starved_blocks: OK")


def test_unmuting_restores_the_mic_to_every_mic_enabled_target():
    a = fake_target("a", settings())
    b = fake_target("b", settings())
    group = sinkgroup.SinkGroup([a, b], mic_device=None)
    start_with_mic(group)
    group.set_mic_muted(True)
    group._distribute_mic(tone(0.4))  # muted: nothing reaches either target
    assert float(np.abs(pull(a)).max()) == 0.0
    assert float(np.abs(pull(b)).max()) == 0.0

    group.set_mic_muted(False)
    group._distribute_mic(tone(0.4))
    assert abs(pull(a)[0, 0] - 0.4) < 1e-6, "unmuting must restore the mic to a"
    assert abs(pull(b)[0, 0] - 0.4) < 1e-6, "unmuting must restore the mic to b"
    group.stop()
    print("unmuting restores the mic to every mic-enabled target: OK")


def test_global_mute_does_not_touch_a_targets_own_mic_switch():
    """A target with its own 'mic' switch off must stay silent regardless of the
    global mute, and set_mic_muted() must never flip that target's own setting."""
    off = fake_target("off", settings(mic=False))
    on = fake_target("on", settings())
    group = sinkgroup.SinkGroup([off, on], mic_device=None)
    start_with_mic(group)

    group._distribute_mic(tone(0.5))
    assert float(np.abs(pull(off)).max()) == 0.0, "mic switch off: silent even unmuted"
    assert off.mic is False

    group.set_mic_muted(True)
    group._distribute_mic(tone(0.5))
    assert float(np.abs(pull(off)).max()) == 0.0
    assert off.mic is False, "the global mute must not flip a target's own mic switch"

    group.set_mic_muted(False)
    group._distribute_mic(tone(0.5))
    assert float(np.abs(pull(off)).max()) == 0.0, "still off after unmuting"
    assert off.mic is False
    assert abs(pull(on)[0, 0] - 0.5) < 1e-6, "the other target is unaffected"
    group.stop()
    print("global mute does not touch a target's own mic switch: OK")


def test_stop_all_sources_clears_every_target():
    a = fake_target("a", settings())
    b = fake_target("b", settings())
    group = sinkgroup.SinkGroup([a, b], mic_device=None)
    group.start()
    handle = group.add_source(tone(0.5), gain=1.0)
    group.stop_all_sources()
    assert handle.finished.is_set()
    assert float(np.abs(pull(a)).max()) == 0.0
    assert float(np.abs(pull(b)).max()) == 0.0
    group.stop()
    print("stop_all_sources clears every target: OK")


def test_remove_matches_sources_by_identity_not_position():
    """SinkGroup.remove() must not zip(self.targets, handle.per_target()): a target
    with its 'sounds' switch off never gets a MixSource, so the two lists differ in
    length. b below is that target - it sits BETWEEN a and c, so a positional zip()
    stops one pair short and never reaches c at all, leaving c's source alive but
    stopped in a half-broken state. Matching by identity is the only way both a and c
    end up correct. This is the exact regression the code review flagged: once
    SinkGroup is wired into AudioEngine, this runs on every ordinary stop-a-sound."""
    a = fake_target("a", settings(mic=False))
    b = fake_target("b", settings(mic=False, sounds=False))  # off: contributes no source
    c = fake_target("c", settings(mic=False))
    group = sinkgroup.SinkGroup([a, b, c], mic_device=None)
    group.start()

    first = group.add_source(tone(0.1), gain=1.0)
    second = group.add_source(tone(0.2), gain=1.0)
    # b contributed nothing, so each handle only carries two sources (a's and c's).
    source_a1, source_c1 = first.per_target()
    source_a2, source_c2 = second.per_target()

    group.remove(first)

    # The removed playback's own sources must be gone from exactly the targets that
    # held them - checked directly on each target's own list, not just via output,
    # because a stale-but-stopped source would still read as silence on the next pull.
    assert source_a1 not in a._sources, "a still holds a source that should be gone"
    assert source_c1 not in c._sources, (
        "c still holds the removed source - this is what positional zip() gets "
        "wrong: it runs out of pairs before reaching c"
    )
    assert b._sources == [], "b never had a source to begin with"

    # The still-running second playback must be untouched in every target.
    assert source_a2 in a._sources, "second playback must survive in a"
    assert source_c2 in c._sources, "second playback must survive in c"
    assert abs(pull(a)[0, 0] - 0.2) < 1e-6, "a must still mix the second playback"
    assert abs(pull(c)[0, 0] - 0.2) < 1e-6, "c must still mix the second playback"

    assert first.finished.is_set(), "the removed handle must report finished"
    assert not second.finished.is_set(), "the surviving handle must not report finished"

    group.stop()
    print("remove matches sources by identity not position: OK")


def test_remove_twice_is_a_noop():
    """Removing the same handle a second time (e.g. a double stop request) must not
    raise and must not touch any other playback still running in the group."""
    a = fake_target("a", settings(mic=False))
    c = fake_target("c", settings(mic=False))
    group = sinkgroup.SinkGroup([a, c], mic_device=None)
    group.start()

    first = group.add_source(tone(0.1), gain=1.0)
    second = group.add_source(tone(0.2), gain=1.0)
    source_a2, source_c2 = second.per_target()

    group.remove(first)
    group.remove(first)  # must be a harmless no-op the second time

    assert first.finished.is_set()
    assert source_a2 in a._sources, "second playback must survive a double remove"
    assert source_c2 in c._sources, "second playback must survive a double remove"
    assert abs(pull(a)[0, 0] - 0.2) < 1e-6
    assert abs(pull(c)[0, 0] - 0.2) < 1e-6

    group.stop()
    print("remove twice is a noop: OK")


def test_build_makes_one_target_per_cable_plus_monitor():
    cfg = config._default_config()
    cfg["default_mic"] = True
    config.set_output_settings(cfg, "CABLE Output (VB-Audio Virtual Cable)", sounds=True)
    config.set_output_settings(cfg, "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)", sounds=False)
    resolved = {
        "monitor": 10,
        "mic": 31,
        "virtual_mics": [
            {"key": "CABLE Output (VB-Audio Virtual Cable)", "label": "CABLE",
             "out_index": 29, "in_index": 39,
             "out_name": "CABLE Input (VB-Audio Virtual Cable)",
             "in_name": "CABLE Output (VB-Audio Virtual Cable)"},
            {"key": "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)", "label": "Hi-Fi Cable",
             "out_index": 28, "in_index": 38,
             "out_name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)",
             "in_name": "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)"},
        ],
    }
    group = sinkgroup.build(cfg, resolved, open_streams=False)
    assert group is not None
    keys = [t.key for t in group.targets]
    assert keys == ["CABLE Output (VB-Audio Virtual Cable)",
                    "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)",
                    config.MONITOR_KEY], keys
    assert group.target("Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)").sounds is False
    monitor = group.target(config.MONITOR_KEY)
    assert monitor.device == 10 and monitor.mic is False
    assert group.mic_device == 31
    print("build makes one target per cable plus monitor: OK")


def test_build_without_any_target_returns_none():
    cfg = config._default_config()
    assert sinkgroup.build(cfg, {"monitor": None, "mic": 31, "virtual_mics": []},
                           open_streams=False) is None
    print("build without any target returns None: OK")


def test_build_keeps_going_without_a_monitor():
    cfg = config._default_config()
    resolved = {
        "monitor": None, "mic": None,
        "virtual_mics": [
            {"key": "CABLE Output", "label": "CABLE", "out_index": 29, "in_index": 39,
             "out_name": "CABLE Input", "in_name": "CABLE Output"},
        ],
    }
    group = sinkgroup.build(cfg, resolved, open_streams=False)
    assert group is not None and [t.key for t in group.targets] == ["CABLE Output"]
    print("build keeps going without a monitor: OK")

def test_settings_survive_a_vanished_device():
    """Spec-Fehlerfall: Kabel deinstalliert. Der Eintrag in outputs bleibt erhalten,
    damit die Einstellung nach einer Neuinstallation wieder greift."""
    cfg = config._default_config()
    config.set_output_settings(cfg, "Hi-Fi Cable Output", sounds=False, mic_gain=0.4)
    gone = {"monitor": 10, "mic": None, "virtual_mics": []}
    group = sinkgroup.build(cfg, gone, open_streams=False)
    assert group is not None and [t.key for t in group.targets] == [config.MONITOR_KEY]
    assert "Hi-Fi Cable Output" in cfg["outputs"], "the entry must not be dropped"

    back = {"monitor": 10, "mic": None, "virtual_mics": [
        {"key": "Hi-Fi Cable Output", "label": "Hi-Fi Cable", "out_index": 28,
         "in_index": 38, "out_name": "Hi-Fi Cable Input", "in_name": "Hi-Fi Cable Output"},
    ]}
    again = sinkgroup.build(cfg, back, open_streams=False)
    target = again.target("Hi-Fi Cable Output")
    assert target.sounds is False, "the old setting comes back with the device"
    assert abs(target.mic_gain - 0.4) < 1e-9, target.mic_gain
    print("settings survive a vanished device: OK")


class PassChain:
    """Stands in for MicChain: passes the block through, speaking is set by the test."""

    def __init__(self, speaking=False):
        self.speaking = speaking
        self.seen = []

    def process(self, mono):
        self.seen.append(np.asarray(mono).shape)
        return np.asarray(mono, dtype=np.float32).reshape(-1, 1)


def test_mic_chain_runs_once_before_the_fan_out():
    a = fake_target("a", settings(sounds=False))
    b = fake_target("b", settings(sounds=False))
    group = start_with_mic(sinkgroup.SinkGroup([a, b], mic_device=None))
    chain = PassChain()
    group.mic_chain = chain
    group._input_callback(np.full((FRAMES, 1), 0.25, np.float32), FRAMES, None, None)
    assert chain.seen == [(FRAMES, 1)], "the chain sees the mono block exactly once"
    assert np.allclose(pull(a), 0.25) and np.allclose(pull(b), 0.25), "stereo to every target"
    print("mic chain runs once before the fan-out: OK")


def test_sounds_bypass_the_mic_chain():
    target = fake_target("CABLE Output", settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([target], mic_device=None))
    group.add_source(tone(0.4), gain=1.0)
    assert np.array_equal(pull(target), tone(0.4)), "a sound reaches the cable as decoded"
    print("sounds bypass the mic chain: OK")


def test_offset_applies_to_cables_not_to_the_monitor():
    cable = fake_target("CABLE Output", settings(mic=False))
    monitor = fake_target(config.MONITOR_KEY, settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([cable, monitor], mic_device=None))
    group.apply_levels(-6.0, False, -6.0)
    group.add_source(tone(0.4), gain=1.0)
    assert np.allclose(pull(cable), 0.4 * dynamics.db_to_gain(-6.0), atol=1e-6)
    assert np.allclose(pull(monitor), 0.4), "the headphones keep their own level"
    assert monitor.ducker is None
    print("offset applies to cables, not to the monitor: OK")


def test_ducking_lowers_sounds_while_speaking():
    cable = fake_target("CABLE Output", settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([cable], mic_device=None))
    group.apply_levels(0.0, True, -6.0)
    group.add_source(tone(0.4, frames=FRAMES * 1000), gain=1.0)
    group.voice.speaking = True
    for _ in range(60):
        out = pull(cable)
    assert np.allclose(out, 0.4 * dynamics.db_to_gain(-6.0), atol=1e-3), float(out[0, 0])
    group.voice.speaking = False
    for _ in range(400):
        out = pull(cable)
    assert np.allclose(out, 0.4, atol=1e-3), float(out[0, 0])
    print("ducking lowers sounds while speaking and recovers: OK")


def test_muted_mic_never_ducks():
    cable = fake_target("CABLE Output", settings())
    group = start_with_mic(sinkgroup.SinkGroup([cable], mic_device=None))
    group.mic_chain = PassChain(speaking=True)
    group.set_mic_muted(True)
    group._input_callback(np.full((FRAMES, 1), 0.3, np.float32), FRAMES, None, None)
    assert group.voice.speaking is False
    group.set_mic_muted(False)
    group._input_callback(np.full((FRAMES, 1), 0.3, np.float32), FRAMES, None, None)
    assert group.voice.speaking is True
    print("a muted mic never ducks the sounds: OK")


def test_sound_limiter_catches_overlaps():
    cable = fake_target("CABLE Output", settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([cable], mic_device=None))
    group.add_source(tone(0.6), gain=1.0)
    group.add_source(tone(0.6), gain=1.0)
    out = pull(cable)
    assert float(np.max(np.abs(out))) <= dynamics.db_to_gain(-1.0) + 1e-6, float(np.max(out))
    print("two overlapping sounds stay below -1 dBFS: OK")


def test_music_reaches_every_sounds_target_and_nobody_steals_the_block():
    """Musik-Bus (Spec §4): Muster _distribute_mic - eine Queue je Ziel, kein Ziel
    darf einem anderen Bloecke wegnehmen."""
    a = fake_target("a", settings(mic=False))
    b = fake_target("b", settings(mic=False))
    off = fake_target("off", settings(mic=False, sounds=False))
    group = sinkgroup.SinkGroup([a, b, off], mic_device=None)
    group.start()
    group.distribute_music(tone(0.3))
    assert np.allclose(pull(a), 0.3) and np.allclose(pull(b), 0.3), \
        "beide Ziele erhalten denselben Block"
    assert not pull(off).any(), "sounds aus: keine Musik"
    group.stop()
    print("Musik erreicht jedes sounds-Ziel, niemand stiehlt Bloecke: OK")


def test_music_runs_through_the_sound_branch():
    """Musik liegt in chunks, nicht in blocks: Offset und Ducking wirken darauf."""
    cable = fake_target("CABLE Output", settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([cable], mic_device=None))
    group.apply_levels(-6.0, True, -6.0)
    group.voice.speaking = True
    for _ in range(60):
        group.distribute_music(tone(0.4))
        out = pull(cable)
    ducked = 0.4 * dynamics.db_to_gain(-6.0) * dynamics.db_to_gain(-6.0)
    assert np.allclose(out, ducked, atol=1e-3), f"{float(out[0, 0]):.4f} statt {ducked:.4f}"
    group.voice.speaking = False
    for _ in range(400):
        group.distribute_music(tone(0.4))
        out = pull(cable)
    assert np.allclose(out, 0.4 * dynamics.db_to_gain(-6.0), atol=1e-3), float(out[0, 0])
    group.stop()
    print("Musik laeuft durch den Sound-Zweig (Offset + Ducking): OK")


def test_the_monitor_hears_the_music_unregulated():
    """Kopfhörer: sounds_offset 1.0, kein Ducker - was andere hoeren wird geregelt,
    das eigene Mithoeren nicht (Spec §4)."""
    monitor = fake_target(config.MONITOR_KEY, settings(mic=False))
    group = start_with_mic(sinkgroup.SinkGroup([monitor], mic_device=None))
    group.apply_levels(-6.0, True, -6.0)
    group.voice.speaking = True
    group.distribute_music(tone(0.4))
    assert np.allclose(pull(monitor), 0.4), "kein Offset, kein Duck"
    assert monitor.ducker is None and monitor.sounds_offset == 1.0
    group.stop()
    print("Kopfhoerer hoert die Musik unreguliert: OK")


def test_build_applies_the_configured_levels():
    cfg = config._default_config()
    cfg["sounds_offset_db"] = -10.0
    cfg["ducking_enabled"] = False
    resolved = {
        "virtual_mics": [{"key": "CABLE Output (VB-Audio Virtual Cable)", "label": "CABLE",
                          "out_index": 29, "in_index": 39}],
        "monitor": 5,
        "mic": None,
    }
    group = sinkgroup.build(cfg, resolved, open_streams=False)
    cable, monitor = group.targets
    assert abs(cable.sounds_offset - dynamics.db_to_gain(-10.0)) < 1e-12
    assert cable.ducker is not None and cable.ducker.enabled is False
    assert monitor.sounds_offset == 1.0 and monitor.ducker is None
    print("build applies the configured levels: OK")


def test_a_level_change_reaches_a_sound_that_is_already_playing():
    """Jan's hand check 2026-09-27: moving the headphone level during a sound changed
    nothing - the level was baked into the source when it started."""
    phones = fake_target(config.MONITOR_KEY, settings(mic=False, sounds_gain=1.0))
    group = sinkgroup.SinkGroup([phones], mic_device=None)
    group.start()
    group.add_source(tone(0.4, frames=FRAMES * 4), gain=0.5)
    assert abs(pull(phones)[0, 0] - 0.2) < 1e-6
    group.apply(config.MONITOR_KEY, settings(mic=False, sounds_gain=0.25))
    assert abs(pull(phones)[0, 0] - 0.4 * 0.5 * 0.25) < 1e-6, "the playing sound follows the slider"
    group.add_source(tone(0.4, frames=FRAMES * 4), gain=0.5)
    assert abs(pull(phones)[0, 0] - 2 * 0.4 * 0.5 * 0.25) < 1e-6, "a new sound starts at the new level"
    group.stop()
    print("a level change reaches a sound that is already playing: OK")


def test_switching_sounds_off_silences_a_playing_sound_on_that_target_only():
    cable = fake_target("cable", settings(mic=False))
    phones = fake_target(config.MONITOR_KEY, settings(mic=False))
    group = sinkgroup.SinkGroup([cable, phones], mic_device=None)
    group.start()
    group.add_source(tone(0.4, frames=FRAMES * 4), gain=1.0)
    group.apply("cable", settings(mic=False, sounds=False))
    assert abs(pull(cable)[0, 0]) < 1e-9, "Discord stops hearing the sound at once"
    assert abs(pull(phones)[0, 0] - 0.4) < 1e-6, "the headphones keep playing it"
    group.stop()
    print("switching sounds off silences a playing sound on that target only: OK")


def main():
    test_mic_block_reaches_every_target()
    test_each_target_mixes_its_own_way()
    test_preview_reaches_only_the_headphones_and_volume_reaches_the_cable()
    test_group_source_finishes_when_all_targets_finish()
    test_full_queue_only_hurts_its_own_target()
    test_starved_target_writes_silence_not_garbage()
    test_muted_target_writes_silence_without_inflating_starved_blocks()
    test_unmuting_restores_the_mic_to_every_mic_enabled_target()
    test_global_mute_does_not_touch_a_targets_own_mic_switch()
    test_failing_target_does_not_stop_the_group()
    test_running_stays_true_when_every_target_is_off()
    test_apply_reopens_a_switched_on_target()
    test_missing_mic_leaves_the_sounds_running()
    test_stop_all_sources_clears_every_target()
    test_remove_matches_sources_by_identity_not_position()
    test_remove_twice_is_a_noop()
    test_build_makes_one_target_per_cable_plus_monitor()
    test_build_without_any_target_returns_none()
    test_build_keeps_going_without_a_monitor()
    test_settings_survive_a_vanished_device()
    test_mic_chain_runs_once_before_the_fan_out()
    test_sounds_bypass_the_mic_chain()
    test_offset_applies_to_cables_not_to_the_monitor()
    test_ducking_lowers_sounds_while_speaking()
    test_muted_mic_never_ducks()
    test_sound_limiter_catches_overlaps()
    test_music_reaches_every_sounds_target_and_nobody_steals_the_block()
    test_music_runs_through_the_sound_branch()
    test_the_monitor_hears_the_music_unregulated()
    test_build_applies_the_configured_levels()
    test_a_level_change_reaches_a_sound_that_is_already_playing()
    test_switching_sounds_off_silences_a_playing_sound_on_that_target_only()
    print("\nALL SINKGROUP LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
