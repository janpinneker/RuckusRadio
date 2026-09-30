"""Geraete und Mixer im Testmodus gegen ein Attrappen-Backend."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-routing-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from soundboard import config, playback, protocol as p, routing, store  # noqa: E402
import core_fakes  # noqa: E402

CABLE = core_fakes.CABLE_KEY


def setup(start=True):
    c, events = core_fakes.bare_core()
    c.playback = playback.PlaybackService(c)
    backend = core_fakes.FakeBackend()
    c.routing = routing.RoutingService(c, backend)
    if start:
        c.start()
    return c, events, backend


def notices(events):
    return [n.text for n in core_fakes.of_type(events, p.Notice)]


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_start_builds_the_mixer_and_describes_it():
    c, events, backend = setup()
    assert backend.resolves == 1 and len(backend.sinks) == 1
    sink = backend.sinks[0]
    assert c.engine.sink is sink
    assert (c.engine.voicemeeter_device, c.engine.monitor_device) == (29, 10)
    assert c.engine.monitor_volume == 0.5
    devices = c.state()["devices"]
    assert devices["status"] == {"text": "VB-CABLE verbunden · Mikro live", "tone": "ok"}, devices
    assert [r["key"] for r in devices["output_rows"]] == [CABLE, config.MONITOR_KEY]
    assert devices["mixer"] == {"running": True, "mic_active": True, "mic_muted": False}
    assert devices["discord_device_name"] == CABLE
    json.dumps(p.to_json(p.StateChanged(c.state())))
    assert routing.NO_OUTPUT not in notices(events)
    c2, events2, backend2 = setup(start=False)
    backend2.resolved["voicemeeter"] = None
    backend2.resolved["monitor"] = None
    c2.start()
    assert routing.NO_OUTPUT in notices(events2)
    print("start resolves devices, builds the mixer and describes it: OK")


def test_outputs_and_levels_reach_the_mixer():
    c, events, backend = setup()
    sink = backend.sinks[0]
    c.send(p.SetOutput(CABLE, {"mic": False}))
    assert sink.applied[-1] == (CABLE, {"mic": False, "mic_gain": 1.0,
                                        "sounds": True, "sounds_gain": 1.0, "music": True})
    assert on_disk()["outputs"][CABLE]["mic"] is False, "a switch is saved at once"
    c.send(p.SetOutput(CABLE, {"sounds_gain": 0.5}))
    assert on_disk()["outputs"][CABLE]["sounds_gain"] == 1.0, "a slider waits for the debounce"
    c.executor.advance(store.SAVE_DEBOUNCE_S)
    assert on_disk()["outputs"][CABLE]["sounds_gain"] == 0.5
    c.send(p.SetLevels({"sounds_offset_db": -9.0}))
    assert sink.levels[-1] == (-9.0, True, -6.0)
    c.send(p.SetLevels({"ducking_enabled": False}))
    assert sink.levels[-1] == (-9.0, False, -6.0)
    assert on_disk()["ducking_enabled"] is False
    print("output switches, sliders and mixer levels reach the mixer and the disk: OK")


def test_toggle_mic_mute():
    c, events, backend = setup()
    c.send(p.ToggleMicMute())
    assert backend.sinks[0].mic_muted is True
    assert c.state()["devices"]["mixer"]["mic_muted"] is True
    c.send(p.ToggleMicMute())
    assert backend.sinks[0].mic_muted is False
    assert notices(events)[-2:] == [routing.MIC_MUTED, routing.MIC_OPEN]
    print("the global mic mute toggles on the mixer: OK")


def test_rescan_rebuilds_only_when_idle_and_keeps_the_mute():
    c, events, backend = setup()
    c.send(p.ToggleMicMute())
    c.send(p.Rescan())
    assert backend.rescans == [True]
    assert backend.sinks[0].stopped and len(backend.sinks) == 2
    assert backend.sinks[1].mic_muted is True, "the dock's mute survives a rescan"
    assert c.engine.sink is backend.sinks[1]
    c.engine.playing = {"busy"}
    c.send(p.Rescan())
    assert backend.rescans == [True, False]
    assert len(backend.sinks) == 2 and not backend.sinks[1].stopped
    print("rescan restarts PortAudio only when idle and keeps the mute: OK")


def test_set_microphone_success_failure_and_busy():
    c, events, backend = setup()
    broadcast = "Mikrofon (NVIDIA Broadcast)"
    backend.resolved["mic_name"] = broadcast
    c.send(p.SetMicrophone(broadcast))
    assert c.store.data["microphone_name"] == broadcast
    assert on_disk()["microphone_name"] == broadcast
    assert c.engine.stopped == 1 and backend.rescans == [True]
    assert notices(events)[-1] == routing.MIC_OK.format(name=broadcast)
    backend.resolved["mic_name"] = "Mikrofon (Endorfy Solum Voice S Mic)"
    c.send(p.SetMicrophone("Ghost Mic"))
    assert notices(events)[-1] == routing.MIC_FAILED.format(name="Ghost Mic")
    c.send(p.SetOnboardingActive(True))
    assert c.state()["devices"]["busy"] is True
    c.send(p.SetMicrophone(broadcast))
    assert notices(events)[-1] == routing.MIC_BUSY
    assert c.store.data["microphone_name"] == "Ghost Mic", "busy: nothing changes"
    assert backend.rescans == [True, True]
    print("microphone switch: success, failure hint and busy guard: OK")


def test_set_microphone_apply_false_only_stores_the_choice():
    c, events, backend = setup()
    broadcast = "Mikrofon (NVIDIA Broadcast)"
    before_rescans = len(backend.rescans)
    c.send(p.SetMicrophone(broadcast, apply=False))
    assert c.store.data["microphone_name"] == broadcast
    assert on_disk()["microphone_name"] == broadcast
    assert len(backend.rescans) == before_rescans, "apply=False must not rescan"
    assert c.engine.stopped == 0, "apply=False must not stop_all"
    assert notices(events) == [], "apply=False must not notice"
    # works even while the assistant holds the devices busy
    c.send(p.SetOnboardingActive(True))
    c.send(p.SetMicrophone("Mikrofon (NVIDIA Broadcast) #2", apply=False))
    assert c.store.data["microphone_name"] == "Mikrofon (NVIDIA Broadcast) #2"
    assert on_disk()["microphone_name"] == "Mikrofon (NVIDIA Broadcast) #2"
    assert len(backend.rescans) == before_rescans
    assert notices(events) == [], "still no busy notice with apply=False"
    print("SetMicrophone(apply=False) only stores the choice, even while busy: OK")


def test_set_microphone_apply_true_keeps_todays_behavior():
    c, events, backend = setup()
    broadcast = "Mikrofon (NVIDIA Broadcast)"
    backend.resolved["mic_name"] = broadcast
    c.send(p.SetMicrophone(broadcast))  # apply defaults to True
    assert c.store.data["microphone_name"] == broadcast
    assert c.engine.stopped == 1 and backend.rescans == [True]
    assert notices(events)[-1] == routing.MIC_OK.format(name=broadcast)
    c.send(p.SetOnboardingActive(True))
    before = dict(c.store.data)
    c.send(p.SetMicrophone("Ghost", apply=True))
    assert notices(events)[-1] == routing.MIC_BUSY
    assert c.store.data["microphone_name"] == before["microphone_name"], "busy: nothing changes"
    print("SetMicrophone(apply=True) keeps the busy guard, stop_all and rescan: OK")


def test_rescan_emits_exactly_one_devices_changed_with_the_expected_summary():
    c, events, backend = setup()
    before = len(core_fakes.of_type(events, p.DevicesChanged))
    c.send(p.Rescan())
    changed = core_fakes.of_type(events, p.DevicesChanged)[before:]
    assert len(changed) == 1, changed
    virtual = core_fakes.RESOLVED["virtual_mic"]
    assert changed[0].summary == {
        "found": True,
        "name": virtual["out_name"],
        "label": virtual["label"],
        "discord_device_name": virtual["discord_device_name"],
        "mic_name": core_fakes.RESOLVED["mic_name"],
        "monitor_name": core_fakes.RESOLVED["monitor_name"],
        "mixer_running": True,
    }, changed[0].summary
    devices = c.state()["devices"]
    assert devices["found"] is True
    assert devices["name"] == virtual["out_name"]
    print("Rescan emits exactly one DevicesChanged with the expected summary: OK")


def test_signal_check():
    c, events, backend = setup()
    c.send(p.RunSignalCheck())
    assert backend.checks == [(29, 39)]
    assert backend.sinks[0].stopped and len(backend.sinks) == 2
    done = core_fakes.of_type(events, p.SignalCheckDone)
    assert done == [p.SignalCheckDone(({"key": CABLE, "label": "CABLE", "ok": True,
                                        "rms": 0.1, "reason": "Signal kommt an."},))], done
    devices = c.state()["devices"]
    assert devices["signal"] == {"ok": True, "running": False}
    assert devices["status"]["text"] == "VB-CABLE aktiv — Signal geprüft · Mikro live"

    def broken(_out, _in):
        raise RuntimeError("device vanished")

    backend.verify_path = broken
    c.send(p.RunSignalCheck())
    assert c.routing.signal_running is False
    assert core_fakes.of_type(events, p.SignalCheckDone)[-1] == p.SignalCheckDone(())
    assert notices(events)[-1] == routing.CHECK_FAILED
    assert c.routing.mixer_running is True and c.engine.sink is backend.sinks[-1], \
        "a failed check still rebuilds the mixer - the voice chat keeps the microphone"

    c2, events2, backend2 = setup(start=False)
    backend2.resolved["virtual_mics"] = []
    c2.start()
    c2.send(p.RunSignalCheck())
    assert notices(events2)[-1] == routing.NO_VIRTUAL_MIC
    assert core_fakes.of_type(events2, p.SignalCheckDone) == [p.SignalCheckDone(())]
    print("signal check measures every cable, reports, rebuilds, survives failures: OK")


def test_headphones_follow_the_windows_default():
    c, events, backend = setup()
    # _start_on_device now reads the default device at start (backend.default_id was
    # "{A}" then) and the watcher is created right away with that baseline, so a
    # switch made before the very first tick is no longer lost - one tick is enough
    # to notice it (previously the first tick only captured the baseline and a switch
    # inside that window needed a second tick to be seen).
    backend.default_id = "{B}"
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [True], "the first tick already sees the switch made at start"
    assert core_fakes.of_type(events, p.HeadphonesSwitched) == [
        p.HeadphonesSwitched("Kopfhörer (KT USB Audio)")]
    backend.default_id = "{C}"
    c.playback.playing = {"x"}
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [True], "a playing sound makes the switch wait"
    c.playback.playing = set()
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [True, True]
    backend.default_id = "{D}"
    c.send(p.SetOnboardingActive(True))
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [True, True], "the assistant makes the switch wait"
    c.send(p.SetOnboardingActive(False))
    c.executor.advance(routing.WATCH_S)
    assert len(backend.rescans) == 3, "the switch that waited for the assistant happens now"
    c.store.data["monitor_device"] = "Kopfhörer (KT USB Audio)"
    backend.default_id = "{E}"
    c.executor.advance(routing.WATCH_S)
    assert len(backend.rescans) == 3, "a fixed headphone device does not follow Windows"
    print("headphones follow the Windows default and wait while busy: OK")


def test_stale_results_and_shutdown():
    c, events, backend = setup()
    c.send(p.Rescan())
    before = dict(c.routing.resolved)
    c.routing._applied(1, {"virtual_mic": {}}, {"running": False, "mic_active": False,
                                                "mic_muted": False}, None)
    assert c.routing.resolved == before, "an older device result is dropped"
    current = backend.sinks[-1]
    c.shutdown()
    assert current.stopped, "shutdown hands the microphone back"
    assert c.executor.pending_timers() == 0
    print("stale device results are dropped, shutdown stops the mixer: OK")


def test_mute_survives_a_toggle_during_a_rescan():
    c, events, backend = setup(start=False)
    c.devices = core_fakes.QueuedDevices()
    c.start()
    c.devices.run_all()
    c.send(p.ToggleMicMute())  # muted
    c.devices.run_all()
    c.send(p.Rescan())
    c.send(p.ToggleMicMute())  # unmuted, queued behind the rescan job above
    c.devices.run_all()
    assert c.state()["devices"]["mixer"]["mic_muted"] is False
    assert backend.sinks[-1].mic_muted is False
    c.send(p.Rescan())
    c.devices.run_all()
    assert backend.sinks[-1].mic_muted is False
    print("the mic mute survives a toggle that races a rescan: OK")


def test_a_headphone_switch_that_found_a_playing_sound_is_retried():
    c, events, backend = setup()
    c.executor.advance(routing.WATCH_S)
    backend.default_id = "{B}"
    c.engine.playing = {"x"}  # the device side is busy; core-side playback stays empty
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [False], "a busy device thread does not restart PortAudio"
    assert core_fakes.of_type(events, p.HeadphonesSwitched) == []
    c.engine.playing = set()
    c.executor.advance(routing.WATCH_S)
    assert backend.rescans == [False, True]
    assert core_fakes.of_type(events, p.HeadphonesSwitched) == [
        p.HeadphonesSwitched("Kopfhörer (KT USB Audio)")]
    print("a headphone switch that found a playing sound is retried: OK")


def test_no_rescan_after_shutdown():
    c, events, backend = setup()
    c.shutdown()
    before = list(backend.rescans)
    c.routing.rescan()
    c.routing._default_read("{Z}")
    assert backend.rescans == before, "shutdown must not queue another rescan"
    print("no rescan happens after shutdown: OK")


def test_assistant_mic_choices_come_from_the_device_thread():
    c, events, backend = setup(start=False)
    backend.mic_choice_list = ["Mikro A", "Mikro B"]
    backend.default_mic = "Mikro B"
    c.start()
    devices_state = c.state()["devices"]
    assert devices_state["mic_choices"] == ["Mikro A", "Mikro B"]
    assert devices_state["default_mic"] == "Mikro B"
    backend.mic_choice_list = ["Mikro C"]
    backend.default_mic = None
    c.send(p.Rescan())
    assert c.state()["devices"]["mic_choices"] == ["Mikro C"]
    assert any(call == "mic_choices" for call, _ in backend.threads), "asked through the backend"
    print("assistant mic choices come from the device thread: OK")


def test_headphone_volume_follows_the_migrated_gain():
    c, events, backend = setup(start=False)
    config.set_output_settings(c.store.data, config.MONITOR_KEY, sounds_gain=0.139)
    c.start()
    assert abs(c.engine.monitor_volume - 0.139) < 1e-9, c.engine.monitor_volume
    print("headphone volume follows the migrated Kopfhörer gain: OK")


def test_the_music_bus_hook_follows_every_sink_rebuild():
    """Spec \"musik-bus-kern\" §4/§6.1: der on_block-Haken hängt an der SinkGroup und
    muss bei JEDEM Neuaufbau (Rescan, Prüfung, Gerätewechsel) erneut gesetzt werden -
    sonst verstummt die Musik still."""
    backend = core_fakes.FakeBackend()
    c, _events = core_fakes.make_core(backend=backend)
    bus = c.musicbus.bus
    c.start()
    assert len(backend.sinks) == 1
    assert bus.on_block == backend.sinks[0].distribute_music
    c.send(p.Rescan())
    assert len(backend.sinks) == 2
    assert bus.on_block == backend.sinks[1].distribute_music, "der Haken hängt am NEUEN Sink"
    c.send(p.RunSignalCheck())
    assert len(backend.sinks) == 3
    assert bus.on_block == backend.sinks[2].distribute_music, "auch nach der Prüfung wieder"
    c.shutdown()
    assert bus.on_block is None, "beim Beenden hängt der Haken ab"
    print("der Musik-Bus-Haken folgt jedem Sink-Neuaufbau: OK")


HIFI = "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)"


def test_the_user_names_the_cable_discord_records_from():
    """With two cables Ruckus cannot see which one Discord records from: the primary
    cable was only a guess (Jan's handcheck 2026-09-27: Discord on Hi-Fi Cable, the dock
    button muted VB-CABLE). SetDiscordOutput names it; "" goes back to the primary."""
    c, events, backend = setup(start=False)
    backend.resolved["virtual_mics"].append(
        {"key": HIFI, "label": "Hi-Fi Cable", "out_index": 31, "in_index": 41,
         "out_name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)", "in_name": HIFI})
    c.start()
    assert c.state()["devices"]["discord_device_name"] == CABLE, "default: the primary cable"

    c.send(p.SetDiscordOutput(HIFI))
    assert c.state()["devices"]["discord_device_name"] == HIFI
    assert on_disk()["discord_output"] == HIFI, "the choice is saved at once"
    changed = core_fakes.of_type(events, p.DevicesChanged)
    c.send(p.Rescan())
    assert core_fakes.of_type(events, p.DevicesChanged)[-1].summary["discord_device_name"] == HIFI
    assert len(core_fakes.of_type(events, p.DevicesChanged)) == len(changed) + 1

    # A cable that is gone (uninstalled, renamed) falls back to the primary one.
    backend.resolved["virtual_mics"].pop()
    c.send(p.Rescan())
    assert c.state()["devices"]["discord_device_name"] == CABLE
    assert on_disk()["discord_output"] == HIFI, "the choice survives until the cable is back"

    c.send(p.SetDiscordOutput(""))
    assert on_disk()["discord_output"] is None
    assert c.state()["devices"]["discord_device_name"] == CABLE
    print("the user names the cable Discord records from: OK")


def test_discord_output_ignores_the_headphones():
    c, _events, _backend = setup()
    c.send(p.SetDiscordOutput(config.MONITOR_KEY))
    assert c.state()["devices"]["discord_device_name"] == CABLE, "headphones are no Discord cable"
    print("the headphones are never Discord's cable: OK")


def test_discord_sounds_switch_the_cable_discord_records_from():
    """The browser view may mute Discord (Jan, 2026-09-27) but not touch levels or
    devices: SetDiscordSounds switches only the sounds of Discord's own cable."""
    c, _events, backend = setup(start=False)
    backend.resolved["virtual_mics"].append(
        {"key": HIFI, "label": "Hi-Fi Cable", "out_index": 31, "in_index": 41,
         "out_name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)", "in_name": HIFI})
    c.start()
    sink = backend.sinks[0]
    c.send(p.SetDiscordSounds(False))
    assert sink.applied[-1][0] == CABLE and sink.applied[-1][1]["sounds"] is False
    assert on_disk()["outputs"][CABLE]["sounds"] is False, "saved at once like the switch"
    c.send(p.SetDiscordOutput(HIFI))
    c.send(p.SetDiscordSounds(False))
    assert sink.applied[-1][0] == HIFI and sink.applied[-1][1]["sounds"] is False
    c.send(p.SetDiscordSounds(True))
    assert on_disk()["outputs"][HIFI]["sounds"] is True
    assert on_disk()["outputs"][CABLE]["sounds"] is False, "only Discord's cable moves"
    print("Discord: Sounds switches exactly Discord's cable: OK")


def test_discord_music_switches_only_the_music_of_discords_cable():
    """Spec audio-routing D1: Discord per Knopf ohne Musik, Sounds bleiben unberuehrt."""
    c, _events, backend = setup(start=False)
    backend.resolved["virtual_mics"].append(
        {"key": HIFI, "label": "Hi-Fi Cable", "out_index": 31, "in_index": 41,
         "out_name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)", "in_name": HIFI})
    c.start()
    sink = backend.sinks[0]
    c.send(p.SetDiscordOutput(HIFI))
    c.send(p.SetDiscordMusic(False))
    assert sink.applied[-1][0] == HIFI
    assert sink.applied[-1][1]["music"] is False and sink.applied[-1][1]["sounds"] is True
    assert on_disk()["outputs"][HIFI]["music"] is False, "sofort gespeichert wie der Schalter"
    assert "music" not in (on_disk()["outputs"].get(CABLE) or {}) or \
        on_disk()["outputs"][CABLE]["music"] is True, "nur Discords Kabel"
    c.send(p.SetDiscordMusic(True))
    assert on_disk()["outputs"][HIFI]["music"] is True
    print("Discord: Musik switches exactly the music of Discord's cable: OK")


def test_klangbild_reaches_the_mixer_the_state_and_the_disk():
    """Klangbild K3-K5, Muster SetLevels: Mischpult sofort, Speichern entprellt, Zustand
    fuer die Seite; Unsinn wird geklemmt oder ignoriert."""
    c, events, backend = setup()
    sink = backend.sinks[0]
    c.send(p.SetKlangbild({"music": -12.0, "music_offset_db": -9.0}))
    assert sink.klangbild[-1] == (-9.0, 2.0), sink.klangbild
    assert sink.levels[-1] == (-6.0, True, -6.0), "die bestehenden Pegel reisen mit"
    lv = c.state()["devices"]["levels"]
    assert lv["targets"] == {"effect": -20.0, "music": -12.0}, lv
    assert lv["music_offset_db"] == -9.0
    c.executor.advance(store.SAVE_DEBOUNCE_S)
    assert on_disk()["klangbild_targets"] == {"effect": -20.0, "music": -12.0}
    assert on_disk()["music_offset_db"] == -9.0
    c.send(p.SetKlangbild({"effect": -99, "voice": -5, "music_offset_db": "laut"}))
    lv = c.state()["devices"]["levels"]
    assert lv["targets"] == {"effect": -24.0, "music": -12.0}, lv
    assert lv["music_offset_db"] == -3.0, "Unsinn = Standard"
    assert "voice" not in c.store.data["klangbild_targets"]
    c.send(p.SetLevels({"sounds_offset_db": -9.0}))
    assert sink.klangbild[-1] == (-3.0, 2.0), "SetLevels schickt den Musik-Abstand mit"
    print("Klangbild reaches the mixer, the state and the disk: OK")


def main():
    test_discord_sounds_switch_the_cable_discord_records_from()
    test_discord_music_switches_only_the_music_of_discords_cable()
    test_start_builds_the_mixer_and_describes_it()
    test_outputs_and_levels_reach_the_mixer()
    test_klangbild_reaches_the_mixer_the_state_and_the_disk()
    test_toggle_mic_mute()
    test_rescan_rebuilds_only_when_idle_and_keeps_the_mute()
    test_set_microphone_success_failure_and_busy()
    test_set_microphone_apply_false_only_stores_the_choice()
    test_set_microphone_apply_true_keeps_todays_behavior()
    test_rescan_emits_exactly_one_devices_changed_with_the_expected_summary()
    test_signal_check()
    test_headphones_follow_the_windows_default()
    test_stale_results_and_shutdown()
    test_mute_survives_a_toggle_during_a_rescan()
    test_a_headphone_switch_that_found_a_playing_sound_is_retried()
    test_no_rescan_after_shutdown()
    test_assistant_mic_choices_come_from_the_device_thread()
    test_headphone_volume_follows_the_migrated_gain()
    test_the_music_bus_hook_follows_every_sink_rebuild()
    test_the_user_names_the_cable_discord_records_from()
    test_discord_output_ignores_the_headphones()
    print("\nALL ROUTING LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
