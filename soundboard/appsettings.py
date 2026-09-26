"""App-weite Einstellungen, die keinem Fachbereich gehoeren: Assistent erledigt,
Autostart, Hinweis auf eine beim Laden zurueckgesetzte Config."""

from __future__ import annotations

from . import autostart as autostart_module
from .protocol import CompleteOnboarding, SetAutostart

AUTOSTART_FAILED = "Autostart konnte nicht geändert werden."
CONFIG_RESET = ("Einstellungen waren beschädigt und wurden zurückgesetzt. "
                "Sicherung: config.json.bak")


class AppSettingsService:
    def __init__(self, core, autostart=autostart_module):
        self._core = core
        self._autostart = autostart
        core.handle(CompleteOnboarding, self.complete_onboarding)
        core.handle(SetAutostart, self.set_autostart)
        core.add_state("settings", self.snapshot)
        core.on_start(self._report_reset)

    def _cfg(self) -> dict:
        return self._core.store.data

    def snapshot(self) -> dict:
        cfg = self._cfg()
        return {
            "onboarding_completed": bool(cfg.get("onboarding_completed")),
            "autostart": bool(cfg.get("autostart")),
            "stop_all_hotkey": cfg.get("stop_all_hotkey"),
            "microphone_name": cfg.get("microphone_name"),
            "config_was_reset": bool(self._core.store.was_reset),
        }

    def _report_reset(self) -> None:
        if self._core.store.was_reset:
            self._core.notice(CONFIG_RESET)

    def complete_onboarding(self, _cmd: CompleteOnboarding) -> None:
        self._cfg()["onboarding_completed"] = True
        self._core.store.save_now()
        self._core.state_changed()

    def set_autostart(self, cmd: SetAutostart) -> None:
        if not self._autostart.apply(cmd.enabled):
            self._core.notice(AUTOSTART_FAILED)
            self._core.state_changed()  # the interface puts its checkbox back
            return
        self._cfg()["autostart"] = bool(cmd.enabled)
        self._core.store.save_now()
        self._core.state_changed()
