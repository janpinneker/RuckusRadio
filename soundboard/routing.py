"""Geraete und Mixer: aufloesen, neu scannen, Ausgaenge, Mischpult, Mikrofon, Pruefen,
Kopfhoerer folgen dem Windows-Standardgeraet.

Kern-Thread: Config aendern, entscheiden, Zustand. Geraete-Thread: alles, was PortAudio
oder COM anfasst - ueber DeviceBackend - und die SinkGroup. self._sink wird nur auf dem
Geraete-Thread gelesen und geschrieben; der Kern kennt gespiegelte Werte
(mixer_running, mic_active, mic_muted). Jeder Geraete-Auftrag bekommt eine Kopie der
Config, nie die Live-Config. Jedes Ergebnis traegt eine Generationsnummer; ein aelteres
als das zuletzt uebernommene wird verworfen.
"""

from __future__ import annotations

import copy
import logging
from typing import Callable

from . import config, defaultdevice, devices, levels, miccheck, sinkgroup
from .layout import output_rows, virtual_mic_status
from .protocol import (DevicesChanged, HeadphonesSwitched, Rescan, RunSignalCheck,
                       SetDiscordOutput, SetLevels, SetMicrophone, SetOnboardingActive,
                       SetOutput, SignalCheckDone, ToggleMicMute)

log = logging.getLogger(__name__)

WATCH_S = 2.0  # how often the headphones check the Windows default device

NO_OUTPUT = "Kein Ausgabegerät gefunden. Schließ Lautsprecher oder Kopfhörer an und starte neu."
NO_VIRTUAL_MIC = "Kein virtuelles Mikrofon gefunden — Assistent öffnen."
MIC_BUSY = "Prüfung läuft – Mikrofon gleich noch einmal wählen."
MIC_OK = "Mikrofon: {name}"
MIC_FAILED = "„{name}“ lässt sich nicht öffnen – Gerät angeschlossen?"
MIC_MUTED = "Mikrofon stumm — die anderen hören nur noch Sounds."
MIC_OPEN = "Mikrofon wieder offen."
CHECK_FAILED = "Prüfung fehlgeschlagen – Details stehen in ruckus.log."
NO_DEVICE = "kein Gerät"


class DeviceBackend:
    """Everything routing needs from PortAudio and Windows, in one replaceable place.
    Device thread only."""

    def resolve(self, cfg: dict) -> dict:
        return devices.resolve_system_devices(cfg)

    def rescan(self, cfg: dict, reinit: bool) -> dict:
        return devices.rescan_system_devices(cfg, reinit=reinit)

    def build_sink(self, cfg: dict, resolved: dict):
        return sinkgroup.build(cfg, resolved)

    def verify_path(self, out_index, in_index) -> dict:
        return miccheck.verify_path(out_index, in_index)

    def mic_choices(self) -> tuple[list[str], str | None]:
        return devices.system_mic_choices()

    def default_render_id(self) -> str | None:
        return defaultdevice.default_render_id()


def _sink_info(sink) -> dict:
    return {"running": sink is not None,
            "mic_active": bool(sink is not None and sink.mic_active),
            "mic_muted": bool(sink is not None and sink.mic_muted)}


class RoutingService:
    def __init__(self, core, backend):
        self._core = core
        self._backend = backend
        self.resolved: dict = {}
        self.mixer_running = False
        self.mic_active = False
        self.mic_muted = False
        self.signal_ok: bool | None = None
        self.signal_running = False
        self.onboarding_active = False
        self._generation = 0
        self._applied_generation = 0
        self._watcher: defaultdevice.DefaultDeviceWatcher | None = None
        self._watch_timer = None
        self._stopped = False
        self._sink = None  # device thread only
        self._dev_muted = False  # device thread only: the mute the sink was built/kept with
        self._mute_seq = 0  # core thread: bumped on every toggle, carried through device jobs
        core.handle(SetOutput, self.set_output)
        core.handle(SetLevels, self.set_levels)
        core.handle(SetMicrophone, self.set_microphone)
        core.handle(SetDiscordOutput, self.set_discord_output)
        core.handle(ToggleMicMute, self.toggle_mic)
        core.handle(RunSignalCheck, self.run_signal_check)
        core.handle(Rescan, lambda _cmd: self.rescan())
        core.handle(SetOnboardingActive, self.set_onboarding_active)
        core.add_state("devices", self.snapshot)
        core.on_start(self.start)
        core.on_shutdown(self.shutdown)

    @property
    def cfg(self) -> dict:
        return self._core.store.data

    @property
    def busy(self) -> bool:
        """Devices are held by a signal check or by the assistant: no restarts now."""
        return self.signal_running or self.onboarding_active

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _config_copy(self) -> dict:
        return copy.deepcopy(self.cfg)

    # ---- start / rescan ----

    def start(self) -> None:
        gen = self._next_generation()
        mute_seq = self._mute_seq
        self._core.devices.submit(self._start_on_device, gen, self._config_copy(), mute_seq)
        self._schedule_watch()

    def _start_on_device(self, gen: int, cfg: dict, mute_seq: int) -> None:  # device
        resolved = self._with_mic_choices(self._backend.resolve(cfg))
        log.info("selected microphone: index=%s name=%r configured=%r "
                 "filter=MicChain(highpass=80Hz, leveler, limiter)",
                 resolved.get("mic"), resolved.get("mic_name"), cfg.get("microphone_name"))
        sink = self._install_sink(cfg, resolved)
        try:
            baseline = self._backend.default_render_id()
        except Exception:
            log.exception("reading the default output device at start failed")
            baseline = None
        self._core.executor.submit(self._start_applied, gen, resolved, _sink_info(sink),
                                   mute_seq, baseline)

    def _start_applied(self, gen: int, resolved: dict, info: dict, mute_seq: int,
                       baseline: str | None) -> None:  # core
        self._applied(gen, resolved, info, self._after_start, mute_seq)
        # A baseline read right at start means a switch inside the very first WATCH_S
        # window is not lost - without this the watcher would only start remembering
        # at the first tick, silently swallowing an earlier switch.
        if self._watcher is None and baseline is not None:
            self._watcher = defaultdevice.DefaultDeviceWatcher(initial=baseline)

    def _after_start(self, resolved: dict, _idle: bool) -> None:
        if resolved.get("voicemeeter") is None and resolved.get("monitor") is None:
            self._core.notice(NO_OUTPUT)

    def rescan(self, followup: Callable[[dict, bool], None] | None = None) -> None:
        if self._stopped:
            return
        gen = self._next_generation()
        mute_seq = self._mute_seq
        self._core.devices.submit(self._rescan_on_device, gen, self._config_copy(),
                                  mute_seq, followup)

    def _rescan_on_device(self, gen: int, cfg: dict, mute_seq: int, followup) -> None:  # device
        idle = not self._core.engine.playing_ids()
        if idle:
            self._drop_sink()  # PortAudio may not be restarted with a stream open
        resolved = self._with_mic_choices(self._backend.rescan(cfg, idle))
        sink = self._install_sink(cfg, resolved) if idle else self._sink
        self._core.executor.submit(self._rescanned, gen, resolved, _sink_info(sink), idle,
                                   mute_seq, followup)

    def _rescanned(self, gen: int, resolved: dict, info: dict, idle: bool, mute_seq: int,
                   followup) -> None:
        if idle:
            self.signal_ok = None
        self._applied(gen, resolved, info, followup, mute_seq, idle)

    def _with_mic_choices(self, resolved: dict) -> dict:  # device
        """The assistant's microphone list travels with every device result, read on
        the device thread - the interface never asks PortAudio itself."""
        try:
            choices, default = self._backend.mic_choices()
        except Exception:
            log.exception("reading the microphone list failed")
            choices, default = [], None
        resolved = dict(resolved)
        resolved["mic_choices"] = list(choices)
        resolved["default_mic"] = default
        return resolved

    def _drop_sink(self) -> None:  # device
        sink, self._sink = self._sink, None
        if sink is not None:
            sink.stop()
        self._core.engine.sink = None
        if self._core.musicbus is not None:
            self._core.musicbus.attach_sink(None)

    def _install_sink(self, cfg: dict, resolved: dict):  # device
        sink = self._backend.build_sink(cfg, resolved)
        if sink is not None:
            sink.set_mic_muted(self._dev_muted)
        # Musik-Bus (Spec "musik-bus-kern" §4/§6.1): der on_block-Haken muss bei
        # JEDEM Neuaufbau erneut gesetzt werden, sonst verstummt die Musik still.
        if self._core.musicbus is not None:
            self._core.musicbus.attach_sink(sink)
        engine = self._core.engine
        engine.voicemeeter_device = resolved.get("voicemeeter")
        engine.monitor_device = resolved.get("monitor")
        engine.monitor_volume = config.output_settings(
            cfg, config.MONITOR_KEY, is_monitor=True)["sounds_gain"]
        engine.sink = sink
        self._sink = sink
        return sink

    def _applied(self, gen: int, resolved: dict, info: dict, followup, mute_seq: int | None = None,
                idle: bool = True) -> None:  # core
        if gen < self._applied_generation:
            log.info("dropping device result %s, %s is newer", gen, self._applied_generation)
            return
        self._applied_generation = gen
        self.resolved = resolved
        self.mixer_running = info["running"]
        self.mic_active = info["mic_active"]
        if mute_seq is None or mute_seq == self._mute_seq:
            self.mic_muted = info["mic_muted"]
        self._core.state_changed()
        self._core.emit(DevicesChanged(self._devices_changed_summary()))
        if followup is not None:
            followup(resolved, idle)

    def _devices_changed_summary(self) -> dict:
        """JSON-safe summary for DevicesChanged: what an interface needs to redraw
        the virtual mic status without pulling the whole snapshot apart."""
        virtual = self.resolved.get("virtual_mic") or {}
        return {
            "found": bool(virtual.get("connected")),
            "name": virtual.get("out_name"),
            "label": virtual.get("label"),
            "discord_device_name": self._discord_device_name(),
            "mic_name": self.resolved.get("mic_name"),
            "monitor_name": self.resolved.get("monitor_name"),
            "mixer_running": self.mixer_running,
        }

    # ---- settings that reach the mixer ----

    def _with_sink(self, fn: Callable) -> None:
        self._core.devices.submit(self._call_sink, fn)

    def _call_sink(self, fn: Callable) -> None:  # device
        if self._sink is not None:
            fn(self._sink)

    def set_output(self, cmd: SetOutput) -> None:
        settings = config.set_output_settings(self.cfg, cmd.key, **cmd.changes)
        self._with_sink(lambda sink: sink.apply(cmd.key, settings))
        if any(name.endswith("_gain") for name in cmd.changes):
            self._core.store.save_soon()
        else:
            self._core.store.save_now()
        self._core.state_changed()

    def set_levels(self, cmd: SetLevels) -> None:
        for name, value in cmd.changes.items():
            if name == "ducking_enabled":
                self.cfg[name] = bool(value)
            elif name in ("sounds_offset_db", "ducking_db"):
                self.cfg[name] = float(value)
        offset = levels.sounds_offset_db(self.cfg)
        enabled, depth = levels.ducking(self.cfg)
        self._with_sink(lambda sink: sink.apply_levels(offset, enabled, depth))
        if "ducking_enabled" in cmd.changes:
            self._core.store.save_now()
        else:
            self._core.store.save_soon()
        self._core.state_changed()

    def toggle_mic(self, _cmd: ToggleMicMute) -> None:
        if not self.mixer_running:
            return
        muted = not self.mic_muted
        self.mic_muted = muted
        self._mute_seq += 1
        self._core.devices.submit(self._set_dev_mute, muted)
        self._core.notice(MIC_MUTED if muted else MIC_OPEN)
        self._core.state_changed()

    def _set_dev_mute(self, muted: bool) -> None:  # device
        self._dev_muted = muted
        if self._sink is not None:
            self._sink.set_mic_muted(muted)

    def set_microphone(self, cmd: SetMicrophone) -> None:
        name = cmd.name
        if not cmd.apply:
            # the assistant stores the choice while it holds the devices busy; no busy
            # check, no stop_all, no rescan, no notice - applied later with apply=True.
            self.cfg["microphone_name"] = name
            self._core.store.save_now()
            self._core.state_changed()
            return
        if self.busy:
            self._core.notice(MIC_BUSY)
            self._core.state_changed()  # the interface shows the real microphone again
            return
        self.cfg["microphone_name"] = name
        self._core.store.save_now()
        self._core.playback.stop_all()  # PortAudio restarts only without open streams

        def report(resolved: dict, _idle: bool) -> None:
            ok = resolved.get("mic_name") == name
            self._core.notice(MIC_OK.format(name=name) if ok else MIC_FAILED.format(name=name))

        self.rescan(followup=report)

    def set_discord_output(self, cmd: SetDiscordOutput) -> None:
        self.cfg["discord_output"] = cmd.key or None
        self._core.store.save_now()
        self._core.state_changed()

    def _discord_device_name(self) -> str | None:
        """The cable Discord records from: the user's choice while that cable exists,
        otherwise the primary cable (the old guess). Headphones never count."""
        chosen = self.cfg.get("discord_output")
        cables = {pair["key"] for pair in self.resolved.get("virtual_mics") or []}
        if chosen and chosen in cables:
            return chosen
        return (self.resolved.get("virtual_mic") or {}).get("discord_device_name")

    def set_onboarding_active(self, cmd: SetOnboardingActive) -> None:
        self.onboarding_active = bool(cmd.active)
        self._core.state_changed()

    # ---- signal check ----

    def run_signal_check(self, _cmd: RunSignalCheck | None = None) -> None:
        pairs = copy.deepcopy(self.resolved.get("virtual_mics") or [])
        if not pairs:
            self._core.notice(NO_VIRTUAL_MIC)
            self._core.emit(SignalCheckDone(()))
            return
        if self.signal_running:
            return
        self.signal_running = True
        self._core.state_changed()
        gen = self._next_generation()
        mute_seq = self._mute_seq
        self._core.devices.submit(self._check_on_device, gen, self._config_copy(),
                                  copy.deepcopy(self.resolved), pairs, mute_seq)

    def _check_on_device(self, gen: int, cfg: dict, resolved: dict, pairs: list[dict],
                         mute_seq: int) -> None:  # device
        had_sink = self._sink is not None
        results = None
        try:
            self._drop_sink()  # the measurement needs the devices for itself
            results = [{"key": pair["key"], "label": pair["label"],
                        **self._backend.verify_path(pair.get("out_index"), pair.get("in_index"))}
                       for pair in pairs]
        except Exception:
            log.exception("signal check failed")
        # Rebuild the mixer even when the measurement failed - otherwise the voice
        # chat would lose the microphone until the next rescan.
        sink = None
        if had_sink:
            try:
                sink = self._install_sink(cfg, resolved)
            except Exception:
                log.exception("rebuilding the mixer after the signal check failed")
        info = _sink_info(sink)
        if results is None:
            self._core.executor.submit(self._check_failed, gen, resolved, info, mute_seq)
            return
        self._core.executor.submit(self._checked, gen, resolved, info, mute_seq, results)

    def _checked(self, gen: int, resolved: dict, info: dict, mute_seq: int,
                results: list[dict]) -> None:
        self.signal_running = False
        self.signal_ok = all(r["ok"] for r in results) if results else None
        self._applied(gen, resolved, info, None, mute_seq)
        self._core.emit(SignalCheckDone(tuple(results)))

    def _check_failed(self, gen: int, resolved: dict, info: dict, mute_seq: int) -> None:
        self.signal_running = False
        self._applied(gen, resolved, info, None, mute_seq)
        self._core.notice(CHECK_FAILED)
        self._core.emit(SignalCheckDone(()))

    # ---- headphones follow the Windows default device ----

    def _schedule_watch(self) -> None:
        if self._stopped:
            return
        self._watch_timer = self._core.executor.call_later(WATCH_S, self._watch_tick)

    def _watch_tick(self) -> None:  # core timer
        self._watch_timer = None
        self._core.devices.submit(self._read_default)

    def _read_default(self) -> None:  # device
        try:
            current = self._backend.default_render_id()
        except Exception:
            log.exception("reading the default output device failed")
            current = None
        self._core.executor.submit(self._default_read, current)

    def _default_read(self, current: str | None) -> None:  # core
        if self._stopped:
            return
        try:
            if self._watcher is None:
                if current is not None:
                    self._watcher = defaultdevice.DefaultDeviceWatcher(initial=current)
                return
            follows = self.cfg.get("monitor_device", "default") == "default"
            if not follows or not self._watcher.observe(current):
                return
            if self.busy or self._core.playback.playing:
                return  # stays pending; taken over at a later tick
            self._watcher.done()

            def followup(resolved: dict, idle: bool) -> None:
                if not idle:
                    # a sound was playing on the device thread - PortAudio was not
                    # restarted, so the switch did not happen; try again later.
                    self._watcher.pending = True
                    return
                self._core.emit(HeadphonesSwitched(resolved.get("monitor_name") or NO_DEVICE))

            self.rescan(followup=followup)
        finally:
            self._schedule_watch()

    # ---- shutdown / state ----

    def shutdown(self) -> None:
        self._stopped = True
        if self._watch_timer is not None:
            self._watch_timer.cancel()
            self._watch_timer = None
        self._core.devices.submit(self._drop_sink)  # hands the microphone back

    def snapshot(self) -> dict:
        text, tone = virtual_mic_status(self.resolved, self.signal_ok, self.mic_active)
        enabled, depth = levels.ducking(self.cfg)
        virtual = self.resolved.get("virtual_mic") or {}
        return {
            "status": {"text": text, "tone": tone},
            "mixer": {"running": self.mixer_running, "mic_active": self.mic_active,
                      "mic_muted": self.mic_muted},
            "signal": {"ok": self.signal_ok, "running": self.signal_running},
            "busy": self.busy,
            "found": bool(virtual.get("connected")),
            "name": virtual.get("out_name"),
            "discord_device_name": self._discord_device_name(),
            "virtual_mic_label": virtual.get("label"),
            "output_rows": output_rows(self.resolved, self.cfg),
            "microphones": list(self.resolved.get("microphones") or []),
            "mic_name": self.resolved.get("mic_name"),
            "mic_choices": list(self.resolved.get("mic_choices") or []),
            "default_mic": self.resolved.get("default_mic"),
            "monitor_name": self.resolved.get("monitor_name"),
            "levels": {"sounds_offset_db": levels.sounds_offset_db(self.cfg),
                       "ducking_enabled": enabled, "ducking_db": depth},
        }
