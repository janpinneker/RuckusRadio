"""Globale Hotkeys am Kern.

Die Callbacks laufen auf dem Hook-Thread der keyboard-Bibliothek. Sie legen nur einen
Befehl in die Kern-Warteschlange (core.send ist thread-sicher) und schlucken jede
Ausnahme - eine, die in den Hook-Thread entkommt, wuerde den Hook zerstoeren. Die
Oberflaeche ist nicht beteiligt: friert sie ein, feuern Hotkeys trotzdem.
"""

from __future__ import annotations

import logging

from . import config
from .protocol import Play, RemoveHotkey, ResumeHotkeys, SetHotkey, StopAll, SuspendHotkeys

log = logging.getLogger(__name__)

INVALID = ("Hotkey von „{label}“ ist ungültig und wurde übersprungen. "
           "Vergib ihn per Rechtsklick neu.")


class HotkeyService:
    def __init__(self, core, manager):
        self._core = core
        self._manager = manager
        core.handle(SetHotkey, self.set_hotkey)
        core.handle(RemoveHotkey, self.remove_hotkey)
        core.handle(SuspendHotkeys, lambda _cmd: self.unregister_all())
        core.handle(ResumeHotkeys, lambda _cmd: self.register_all())
        core.on_start(self.register_all)
        core.before_shutdown(self.unregister_all)
        # A command that re-registers hotkeys (e.g. ResumeHotkeys) may still be queued
        # ahead of the shutdown steps when before_shutdown runs; unregister once more
        # on the core thread so those late re-registrations do not survive shutdown.
        core.on_shutdown(self.unregister_all)

    def _cfg(self) -> dict:
        return self._core.store.data

    def _fire(self, command) -> None:  # keyboard hook thread
        try:
            self._core.send(command)
        except Exception:
            log.exception("hotkey callback failed")

    def _play_callback(self, sound_id: str):
        return lambda: self._fire(Play(sound_id))

    def _register(self, hotkey: str, callback, label: str) -> None:
        try:
            self._manager.register(hotkey, callback)
        except Exception as exc:
            log.error("could not register hotkey %r for %s: %r", hotkey, label, exc)
            self._core.notice(INVALID.format(label=label))

    def register_all(self) -> None:
        """(Re-)wire stop-all plus every sound's hotkey from the config."""
        if self._manager is None:
            return
        self._manager.unregister_all()
        stop_hotkey = self._cfg().get("stop_all_hotkey")
        if stop_hotkey:
            self._register(stop_hotkey, lambda: self._fire(StopAll()), "Alle stoppen")
        for sound in self._cfg()["sounds"]:
            if sound.get("hotkey"):
                self._register(sound["hotkey"], self._play_callback(sound["id"]), sound["name"])

    def unregister_all(self) -> None:
        if self._manager is None:
            return
        try:
            self._manager.unregister_all()
        except Exception:
            log.exception("unregistering hotkeys failed")

    def set_hotkey(self, cmd: SetHotkey) -> None:
        sound = config.find_sound(self._cfg(), cmd.sound_id)
        if sound is None:
            return
        old_hotkey = sound.get("hotkey")
        sound["hotkey"] = cmd.hotkey
        self._core.store.save_now()
        self._core.state_changed()
        if self._manager is None:
            return
        try:
            self._manager.rebind(old_hotkey, cmd.hotkey, self._play_callback(cmd.sound_id))
        except Exception as exc:
            log.error("could not bind hotkey %r: %r", cmd.hotkey, exc)
            self._core.notice(INVALID.format(label=sound["name"]))

    def remove_hotkey(self, cmd: RemoveHotkey) -> None:
        sound = config.find_sound(self._cfg(), cmd.sound_id)
        if sound is None:
            return
        old_hotkey = sound.get("hotkey")
        sound["hotkey"] = None
        self._core.store.save_now()
        self._core.state_changed()
        if self._manager is not None and old_hotkey:
            self._manager.unregister(old_hotkey)

    def forget(self, sound: dict) -> None:
        """A sound is being deleted: its hotkey must stop firing."""
        if self._manager is not None and sound.get("hotkey"):
            try:
                self._manager.unregister(sound["hotkey"])
            except Exception:
                log.exception("unregistering %r failed", sound["hotkey"])
