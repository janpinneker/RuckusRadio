"""Globale Hotkeys am Kern.

Die Callbacks laufen auf dem Hook-Thread der keyboard-Bibliothek. Sie legen nur einen
Befehl in die Kern-Warteschlange (core.send ist thread-sicher) und schlucken jede
Ausnahme - eine, die in den Hook-Thread entkommt, wuerde den Hook zerstoeren. Die
Oberflaeche ist nicht beteiligt: friert sie ein, feuern Hotkeys trotzdem.
"""

from __future__ import annotations

import logging

from . import config
from .hotkeys import (
    RESERVED_HOTKEYS,
    find_hotkey_conflict,
    has_altgr,
    hotkey_error,
    is_modifier_only,
    normalize_hotkey,
)
from .protocol import (
    Play,
    RemoveHotkey,
    ResumeHotkeys,
    SetHotkey,
    SetStopAllHotkey,
    StopAll,
    SuspendHotkeys,
)

log = logging.getLogger(__name__)

INVALID = ("Hotkey von „{label}“ ist ungültig und wurde übersprungen. "
           "Vergib ihn per Rechtsklick neu.")
STOP_ALL_RESERVED = "Strg+Alt+Entf gehört Windows. Wähl für „Alle stoppen“ eine andere Kombination."
STOP_ALL_TAKEN = "Diese Kombination hat schon „{name}“. Wähl für „Alle stoppen“ eine andere."
STOP_ALL_MODIFIER_ONLY = ("Nur Strg, Alt oder Shift reicht für „Alle stoppen“ nicht. "
                          "Nimm eine Taste dazu.")
STOP_ALL_ALTGR = "AltGr tippt Zeichen und fällt als Stopp-Taste weg. Wähl für „Alle stoppen“ eine andere."
STOP_ALL_UNUSABLE = ("Diese Kombination kann Ruckus nicht als Hotkey nutzen. "
                     "Wähl für „Alle stoppen“ eine andere.")
STOP_ALL_INVALID = ("Hotkey für „Alle stoppen“ ist ungültig und wurde übersprungen. "
                    "Vergib ihn in den Einstellungen neu.")


class HotkeyService:
    def __init__(self, core, manager):
        self._core = core
        self._manager = manager
        core.handle(SetHotkey, self.set_hotkey)
        core.handle(RemoveHotkey, self.remove_hotkey)
        core.handle(SetStopAllHotkey, self.set_stop_all_hotkey)
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

    def _register(self, hotkey: str, callback, label: str, message: str | None = None) -> None:
        try:
            self._manager.register(hotkey, callback)
        except Exception as exc:
            log.error("could not register hotkey %r for %s: %r", hotkey, label, exc)
            self._core.notice(message or INVALID.format(label=label))

    def register_all(self) -> None:
        """(Re-)wire stop-all plus every sound's hotkey from the config."""
        if self._manager is None:
            return
        self._manager.unregister_all()
        # A non-string value (hand-edited config.json) counts as no hotkey.
        stop_hotkey = self._cfg().get("stop_all_hotkey")
        if stop_hotkey and isinstance(stop_hotkey, str):
            self._register(stop_hotkey, lambda: self._fire(StopAll()), "Alle stoppen",
                           STOP_ALL_INVALID)
        for sound in self._cfg()["sounds"]:
            if sound.get("hotkey") and isinstance(sound["hotkey"], str):
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
        error = hotkey_error(cmd.hotkey, self._cfg()["sounds"], cmd.sound_id,
                              self._cfg().get("stop_all_hotkey"))
        if error:
            self._core.notice(error)
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

    def set_stop_all_hotkey(self, cmd: SetStopAllHotkey) -> None:
        hotkey = normalize_hotkey(cmd.hotkey)
        # Checked in this order, all before saving: normalizing can turn bad input
        # into a live combo ("ctrl++" -> "ctrl" would fire on every Ctrl press).
        if has_altgr(hotkey):
            self._core.notice(STOP_ALL_ALTGR)
            return
        if is_modifier_only(hotkey):
            self._core.notice(STOP_ALL_MODIFIER_ONLY)
            return
        if hotkey in RESERVED_HOTKEYS:
            self._core.notice(STOP_ALL_RESERVED)
            return
        taken = find_hotkey_conflict(hotkey, self._cfg()["sounds"], None, None)
        if taken:
            self._core.notice(STOP_ALL_TAKEN.format(name=taken))
            return
        validate = getattr(self._manager, "validate", None)
        if validate is not None:
            try:
                validate(hotkey)
            except Exception as exc:
                log.error("stop-all combo %r is not usable: %r", hotkey, exc)
                self._core.notice(STOP_ALL_UNUSABLE)
                return
        self._cfg()["stop_all_hotkey"] = hotkey
        self._core.store.save_now()
        self._core.state_changed()
        self.register_all()

    def forget(self, sound: dict) -> None:
        """A sound is being deleted: its hotkey must stop firing."""
        if self._manager is not None and sound.get("hotkey"):
            try:
                self._manager.unregister(sound["hotkey"])
            except Exception:
                log.exception("unregistering %r failed", sound["hotkey"])
