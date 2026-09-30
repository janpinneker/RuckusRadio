"""App-weite Einstellungen, die keinem Fachbereich gehoeren: Assistent erledigt,
Autostart, Hinweis auf eine beim Laden zurueckgesetzte Config."""

from __future__ import annotations

import logging

from . import autostart as autostart_module
from .protocol import CompleteOnboarding, RegenerateViewToken, SetAutostart, SetSidebarPins

log = logging.getLogger(__name__)

AUTOSTART_FAILED = "Autostart konnte nicht geändert werden."
CONFIG_RESET = ("Einstellungen waren beschädigt und wurden zurückgesetzt. "
                "Sicherung: config.json.bak")
VIEW_TOKEN_RENEWED = "Der Localhost-Link ist neu. Der alte Link funktioniert nicht mehr."

# The sidebar holds what the user pinned (spec spotify-bereich-seitenleiste D3).
PIN_LIMIT = 20
PIN_KINDS = ("spotify-playlist", "spotify-album", "spotify-liked", "folder", "sound-playlist")
PINS_FULL = f"Seitenleiste ist voll ({PIN_LIMIT}). Löse zuerst einen Eintrag."


def clean_pins(pins) -> list[dict] | None:
    """The pins as stored, or None when anything is malformed or doubled. Images are
    only https (Spotify covers) - never a local path the page could be tricked into."""
    out, seen = [], set()
    for pin in pins:
        if not isinstance(pin, dict):
            return None
        kind, pin_id, name, image = pin.get("kind"), pin.get("id"), pin.get("name"), pin.get("image")
        if kind not in PIN_KINDS or not isinstance(pin_id, str) or not pin_id or len(pin_id) > 200:
            return None
        if not isinstance(name, str) or not name or len(name) > 200:
            return None
        if image is not None and not (isinstance(image, str) and image.startswith("https://") and len(image) < 2000):
            return None
        if (kind, pin_id) in seen:
            return None
        seen.add((kind, pin_id))
        out.append({"kind": kind, "id": pin_id, "name": name, "image": image})
    return out


class AppSettingsService:
    def __init__(self, core, autostart=autostart_module):
        self._core = core
        self._autostart = autostart
        core.handle(CompleteOnboarding, self.complete_onboarding)
        core.handle(SetAutostart, self.set_autostart)
        core.handle(RegenerateViewToken, self.regenerate_view_token)
        core.handle(SetSidebarPins, self.set_sidebar_pins)
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
            "sidebar_pins": list(cfg.get("sidebar_pins") or []),
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

    def regenerate_view_token(self, _cmd: RegenerateViewToken) -> None:
        self._core.access.rotate_view_token()
        self._core.notice(VIEW_TOKEN_RENEWED)
        self._core.state_changed()

    def set_sidebar_pins(self, cmd: SetSidebarPins) -> None:
        if len(cmd.pins) > PIN_LIMIT:
            self._core.notice(PINS_FULL)
            return
        pins = clean_pins(cmd.pins)
        if pins is None:
            log.warning("sidebar pins refused: %r", cmd.pins)
            return
        self._cfg()["sidebar_pins"] = pins
        self._core.store.save_now()
        self._core.state_changed()
