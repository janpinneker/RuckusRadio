"""Pure board-layout helpers (no Tk)."""

from __future__ import annotations

import math
from typing import Any

from soundboard import config as _config
from soundboard import devices as _devices
from soundboard.theme import COLUMNS

MIN_SLOTS = COLUMNS * 3

MONITOR_LABEL = "Kopfhörer"
UNKNOWN_MONITOR = "Standardgerät"


def slots_total(n: int) -> int:
    """Tiles + placeholders: fill the current row, add one full row, at least 3 rows."""
    return max(MIN_SLOTS, math.ceil(n / COLUMNS) * COLUMNS + COLUMNS)


def ellipsize(text: str, limit: int = 14) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def keycap_text(hotkey: str | None) -> str | None:
    # "ß".upper() is "SS" - keep the German sharp s as it is printed on the key.
    return "".join(ch if ch == "ß" else ch.upper() for ch in hotkey) if hotkey else None


def needle_for_index(i: int, n: int) -> float:
    return (i + 0.5) / max(n, 1) if n else 0.5


def virtual_mic_status(resolved: dict[str, Any] | None, signal: bool | None = None,
                       mic_active: bool = False) -> tuple[str, str]:
    """Dock text + tone ("ok" | "warn" | "off") for the virtual mic.

    `signal` is the last miccheck.verify_path result: None = not measured yet,
    False = the device exists but nothing came through (the failure the old
    "VoiceMeeter verbunden" label used to hide)."""
    virtual = (resolved or {}).get("virtual_mic") or {}
    if not virtual.get("connected"):
        return "Kein virtuelles Mikrofon — nur Mithören aktiv", "off"
    label = virtual.get("label") or "Virtuelles Mikrofon"
    if signal is False:
        return f"{label} gefunden, aber kein Signal — prüfen", "warn"
    suffix = " · Mikro live" if mic_active else ""
    if signal is None:
        return f"{label} verbunden{suffix}", "ok"
    return f"{label} aktiv — Signal geprüft{suffix}", "ok"


def signal_check_summary(results: list[dict[str, Any]]) -> tuple[str, str]:
    """Dock text + tone ("ok" | "warn" | "off") for a "Prüfen" run across every
    destination the mixer actually opened - one miccheck.verify_path result per
    virtual cable, plus that cable's `key`/`label` (see devices.list_virtual_mics).

    Green only when EVERY destination passed; otherwise names the one(s) that did
    not, never the whole app - the "Prüfen only ever checks one cable and reports its
    verdict for everything" bug this replaces let a dead Discord cable hide behind a
    working CS one."""
    if not results:
        return "Kein virtuelles Mikrofon gefunden — Assistent öffnen.", "off"
    failing = [r for r in results if not r.get("ok")]
    if not failing:
        if len(results) == 1:
            return f"{results[0]['label']}: Signal kommt an.", "ok"
        names = ", ".join(r["label"] for r in results)
        return f"Signal kommt an bei allen Zielen ({names}).", "ok"
    names = ", ".join(r["label"] for r in failing)
    if len(failing) == len(results):
        return f"Kein Signal bei: {names}.", "warn"
    return f"Kein Signal bei: {names} — Rest OK.", "warn"


def filter_sounds(sounds: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().casefold()
    if not q:
        return sounds
    return [s for s in sounds if q in s["name"].casefold()]


def output_rows(resolved: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: the rows the Einstellungen page shows, cables first, monitor last.

    `subtitle` names the device the user has to pick in the voice chat - that is the
    whole point of the row, so it is never abbreviated."""
    rows = []
    for pair in resolved.get("virtual_mics") or []:
        rows.append({
            "key": pair["key"],
            "label": pair["label"],
            "subtitle": pair["in_name"],
            "is_monitor": False,
            "settings": _config.output_settings(
                cfg, pair["key"], is_voicemeeter=_config.is_voicemeeter_key(pair["key"])),
        })
    if resolved.get("monitor") is not None:
        rows.append({
            "key": _config.MONITOR_KEY,
            "label": MONITOR_LABEL,
            "subtitle": resolved.get("monitor_name") or UNKNOWN_MONITOR,
            "is_monitor": True,
            "settings": _config.output_settings(cfg, _config.MONITOR_KEY, is_monitor=True),
        })
    return rows


def microphone_hint(names: list[str], current: str | None) -> str:
    """One line under the microphone choice on Einstellungen."""
    if _devices.is_broadcast_name(current):
        return "NVIDIA Broadcast aktiv: Tastatur und Rauschen werden nur aus deiner Stimme entfernt."
    if any(_devices.is_broadcast_name(n) for n in names):
        return ('NVIDIA Broadcast gefunden – wähle "Mikrofon (NVIDIA Broadcast)", '
                "dann filtert es nur deine Stimme, nicht die Sounds.")
    return ("Tipp: NVIDIA Broadcast (kostenlos, RTX-Grafikkarte) entfernt "
            "Tastaturgeräusche nur aus deiner Stimme.")
