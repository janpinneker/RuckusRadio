"""Global system-wide hotkeys via the `keyboard` low-level Windows hook.
Works even when a game window has focus. If hotkeys don't fire while a
game/anti-cheat process has focus, try running Ruckus Radio as Administrator."""

from __future__ import annotations

import threading
from typing import Callable

import keyboard


class HotkeyManager:
    def __init__(self):
        self._handlers: dict[str, object] = {}  # hotkey_str -> keyboard handler ref

    def register(self, hotkey_str: str, callback: Callable[[], None]) -> None:
        if hotkey_str in self._handlers:
            self.unregister(hotkey_str)
        handler = keyboard.add_hotkey(hotkey_str, callback)
        self._handlers[hotkey_str] = handler

    def unregister(self, hotkey_str: str) -> None:
        handler = self._handlers.pop(hotkey_str, None)
        if handler is not None:
            keyboard.remove_hotkey(handler)

    def unregister_all(self) -> None:
        for hotkey_str in list(self._handlers):
            self.unregister(hotkey_str)

    def rebind(self, old_hotkey: str | None, new_hotkey: str, callback: Callable[[], None]) -> None:
        if old_hotkey:
            self.unregister(old_hotkey)
        self.register(new_hotkey, callback)

    def validate(self, hotkey_str: str) -> None:
        """Raises (ValueError) if the keyboard library cannot parse the combo -
        checked before a combo is saved, so a bad one never replaces a working one."""
        keyboard.parse_hotkey(hotkey_str)


MODIFIER_KEYS = {"ctrl", "alt", "shift", "win", "windows"}

# Fixed canonical priority order — matches what keyboard.get_hotkey_name() produces
# (see keyboard/__init__.py: modifiers = ['ctrl', 'alt', 'shift', 'windows']), not an
# alphabetical sort. Any modifier spelling not in this list (e.g. an alias normalize_hotkey
# doesn't map) sorts after the known ones rather than crashing.
MODIFIER_PRIORITY = ["ctrl", "alt", "shift", "windows"]

# Warnings, not refusals (user decision 2026-09-25): every combination may be used,
# the dialog only says what it will also do while typing.
PLAIN_KEY_MESSAGE = (
    "Achtung: Diese Taste feuert auch beim Tippen im Chat. Geht trotzdem – "
    "sicherer mit Strg, Alt oder Shift dazu."
)
MODIFIER_ONLY_MESSAGE = (
    "Achtung: Nur Strg/Alt/Shift feuert bei jedem Tastenkürzel mit dieser Taste. Geht trotzdem."
)
ALTGR_MESSAGE = (
    "Achtung: AltGr (Strg+Alt) tippt dabei auch ein Zeichen, z. B. AltGr+2 = ², "
    "AltGr+7 = {. Geht trotzdem."
)

# AltGr is a typing key (it produces @ € { } ...), never a safe hotkey modifier.
ALTGR_KEYS = {"alt gr", "altgr"}
# F1-F24 type nothing, so they are fine without a modifier. Numpad keys are NOT
# exempt: the `keyboard` lib names them like the main row ("5", "num 5" -> both
# scan codes), so a bare numpad hotkey would also fire while typing digits.
FUNCTION_KEYS = {f"f{n}" for n in range(1, 25)}

# Strg+Alt+Entf belongs to Windows (secure attention sequence); never assign it.
RESERVED_HOTKEYS = {"ctrl+alt+delete"}
RESERVED_MESSAGE = "Strg+Alt+Entf gehört Windows. Wähl eine andere Kombination."


def normalize_hotkey(hotkey: str) -> str:
    """Lowercase, strip spaces, order-independent: modifiers sorted into the fixed
    canonical priority (ctrl, alt, shift, windows) then the non-modifier key(s), so
    'alt+ctrl+1' and 'ctrl+alt+1' both normalize to 'ctrl+alt+1'. A non-string value
    (hand-edited config.json) counts as no hotkey: ''."""
    if not isinstance(hotkey, str):
        return ""
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    modifiers = sorted(
        (p for p in parts if p in MODIFIER_KEYS),
        key=lambda m: MODIFIER_PRIORITY.index(m) if m in MODIFIER_PRIORITY else len(MODIFIER_PRIORITY),
    )
    rest = [p for p in parts if p not in MODIFIER_KEYS]
    return "+".join(modifiers + rest)


def is_modifier_only(hotkey: str) -> bool:
    """True if nothing but Strg/Alt/Shift/Win is left (or nothing at all) - such a
    combo fires on every shortcut that uses the modifier."""
    return not [p for p in normalize_hotkey(hotkey).split("+") if p and p not in MODIFIER_KEYS]


def has_altgr(hotkey: str) -> bool:
    return any(p in ALTGR_KEYS for p in normalize_hotkey(hotkey).split("+"))


def is_plain_key(hotkey: str) -> bool:
    """True for a single letter/digit pressed with no modifier — would fire while
    typing in chat, so these must be rejected."""
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    if len(parts) != 1:
        return False
    key = parts[0]
    return key not in MODIFIER_KEYS and len(key) == 1 and key.isalnum()


def find_hotkey_conflict(
    hotkey: str,
    sounds: list[dict],
    exclude_sound_id: str | None,
    stop_all_hotkey: str | None,
) -> str | None:
    """Returns the display name of whatever already owns this combo (another
    sound's name, or 'Alle stoppen' for the stop-all hotkey), or None if free."""
    normalized = normalize_hotkey(hotkey)
    if not normalized:
        return None
    if stop_all_hotkey and normalize_hotkey(stop_all_hotkey) == normalized:
        return "Alle stoppen"
    for s in sounds:
        if not isinstance(s, dict) or s.get("id") == exclude_sound_id:
            continue
        existing = s.get("hotkey")
        if existing and normalize_hotkey(existing) == normalized:
            return s["name"]
    return None


def typing_warning(hotkey: str) -> str | None:
    """German warning if the combo would fire (or be typed) during normal chat typing:
    modifier-only combos, a non-F-key without Strg/Alt/Shift/Win, and Strg+Alt+<char>
    without Shift (that is AltGr on German layouts: @, €, {, [, ~, |)."""
    parts = normalize_hotkey(hotkey).split("+") if hotkey.strip() else []
    real_modifiers = {p for p in parts if p in MODIFIER_KEYS}
    keys = [p for p in parts if p not in MODIFIER_KEYS and p not in ALTGR_KEYS]
    if not keys:
        return MODIFIER_ONLY_MESSAGE
    # `keyboard.read_hotkey()` may spell the right Alt key as `alt gr`, while
    # hand-edited/configured combinations can contain it alongside `ctrl`.
    # Both forms are the same German typing key: AltGr+<key> types a character.
    if "alt gr" in parts or "altgr" in parts:
        return ALTGR_MESSAGE
    if not real_modifiers:
        return None if all(k in FUNCTION_KEYS for k in keys) else PLAIN_KEY_MESSAGE
    if real_modifiers == {"ctrl", "alt"} and any(len(k) == 1 or k == "plus" for k in keys):
        return ALTGR_MESSAGE
    return None


def hotkey_error(
    hotkey: str,
    sounds: list[dict],
    exclude_sound_id: str | None,
    stop_all_hotkey: str | None,
) -> str | None:
    """The one thing that refuses a combo: it already belongs to another sound or to
    stop-all. German message, or None if the combo is free. Typing side effects are
    only warnings - see typing_warning."""
    normalized = normalize_hotkey(hotkey)
    if normalized in RESERVED_HOTKEYS:
        return RESERVED_MESSAGE
    conflict = find_hotkey_conflict(normalized, sounds, exclude_sound_id, stop_all_hotkey)
    if conflict:
        return f"„{normalized.upper()}“ ist schon „{conflict}“ zugewiesen. Wähl eine andere Kombination."
    return None


def capture_hotkey_async(on_captured: Callable[[str], None]) -> None:
    """Blocks on a background thread until the user presses a key combo,
    then invokes on_captured(hotkey_str) on that thread — caller is responsible
    for marshalling back onto the GUI thread (app.call_in_ui)."""

    def _worker():
        combo = keyboard.read_hotkey(suppress=False)
        on_captured(combo)

    threading.Thread(target=_worker, daemon=True).start()
