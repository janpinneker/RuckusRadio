"""Persistent config in %APPDATA%\\Soundboard — never inside PyInstaller's
onefile temp extraction dir (sys._MEIPASS), which is wiped between runs."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

CONFIG_VERSION = 2
MAX_SOUND_VOLUME = 1.5  # per-sound gain cap (150 %, about +3.5 dB), VolumeDialog max
MAX_OUTPUT_GAIN = 2.0  # about +6 dB, the top of the dB sliders on Einstellungen
MONITOR_KEY = "__monitor__"  # the headphones inside config["outputs"]

# The stop-all default until 1.1.x was Strg+Alt+Backspace - AltGr+Backspace on German
# keyboards, a typing key. It moves to Strg+ß unless the user picked their own (spec C9).
OLD_STOP_ALL_DEFAULT = "ctrl+alt+backspace"

log = logging.getLogger(__name__)

# True when the last load_config() found a corrupt config.json, moved it to
# config.json.bak and started over from defaults (the GUI shows a hint).
last_load_was_reset = False

DEFAULT_OUTPUT: dict[str, Any] = {
    "mic": True, "mic_gain": 1.0, "sounds": True, "sounds_gain": 1.0, "music": True,
}
# The headphones are a target like any other, but hearing your own voice is
# off unless the user asks for it. Music never goes there: Spotify already plays
# on them directly (spec audio-routing-spotify, premise 3).
DEFAULT_MONITOR_OUTPUT: dict[str, Any] = {
    "mic": False, "mic_gain": 0.0, "sounds": True, "sounds_gain": 0.5, "music": False,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "onboarding_completed": False,
    "voicemeeter_device_name": "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)",
    "outputs": {},  # {recording device name (or MONITOR_KEY): DEFAULT_OUTPUT}
    "default_mic": True,  # what a newly appearing cable starts with
    "default_mic_gain": 1.0,
    "discord_output": None,  # output row key Discord records from; None = the primary cable
    "autostart": False,
    "sidebar_pins": [],  # entries the user pinned to the sidebar (spec spotify-bereich D3)
    "monitor_device": "default",
    "stop_all_hotkey": "ctrl+ß",  # Strg+ß: types nothing, no AltGr (spec C9, user 2026-09-26)
    "stop_all_migrated": True,  # new installs never run the one-time migration below
    "sounds_offset_db": -6.0,  # sounds sit this far below the voice on every cable
    "ducking_enabled": True,  # lower the sounds on the cables while the user speaks
    "ducking_db": -6.0,
    # Klangbild (spec 2026-09-28-klangbild, K3/K4): loudness target per category in
    # LUFS, and how far music sits under the voice on the cables.
    "klangbild_targets": {"effect": -20.0, "music": -14.0},
    "music_offset_db": -3.0,
    # Spotify's public client id (PKCE, no secret). Empty = the music tab shows a hint.
    "spotify_client_id": "",
    # F2: last chosen Spotify Connect device (only the id, spec §13.3)
    "spotify_device_id": "",
    # The local server's port. The Spotify redirect callback is the server's own route
    # (127.0.0.1:47800/callback), so a user-changed port must round-trip through here -
    # the dashboard knows exactly one URL, and `spotify._callback_url` checks the port.
    "server_port": 47800,
    # Musik-Bus (WASAPI-Loopback dessen, was andere Apps spielen; Spec
    # "musik-bus-kern"): aus beim Start, der Pegel begrenzt auf 0.0-2.0.
    "musicbus_enabled": False,
    "musicbus_gain": 1.0,
    # Bibliothek 2.0 (spec 2026-10-01-bibliothek-2, A1): folders/playlists of the
    # sounds area; each sound may carry folder_id, tags and favorite.
    "folders": [],  # [{id, name, parent_id, cover}]
    "playlists": [],  # [{id, name, item_ids, cover}]
    "sounds": [],
}

# Bibliothek 2.0 limits (A3, A6, A8) - shared with collections.py.
COLLECTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_COLLECTION_NAME = 80
MAX_SOUND_NAME = 50  # Jan 2026-10-04 (Nachtlauf B5): a sound name holds at most 50 characters
MAX_TAGS = 20
MAX_TAG_LEN = 32


def clean_collection_name(name: Any) -> str | None:
    """Trimmed name, or None when it is no string, empty or longer than allowed."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if 0 < len(name) <= MAX_COLLECTION_NAME else None


def clip_sound_name(name: str) -> str:
    """A sound name from outside (pack, file name): trimmed and cut to MAX_SOUND_NAME."""
    return name.strip()[:MAX_SOUND_NAME].strip()


def normalize_tags(tags: Any) -> list[str]:
    """Like the web page's normalizeTags: trimmed strings, no case-insensitive
    duplicates, at most MAX_TAGS of at most MAX_TAG_LEN characters each."""
    if not isinstance(tags, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()[:MAX_TAG_LEN].strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) == MAX_TAGS:
            break
    return out


def get_app_data_dir() -> Path:
    """%APPDATA%\\Soundboard (or $RUCKUS_DATA_DIR), with sounds\\ and icons\\ subfolders."""
    override = os.environ.get("RUCKUS_DATA_DIR")
    base = Path(override) if override else Path(os.environ["APPDATA"]) / "Soundboard"
    (base / "sounds").mkdir(parents=True, exist_ok=True)
    (base / "icons").mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return get_app_data_dir() / "config.json"


def _default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


def _with_defaults(loaded: dict[str, Any]) -> dict[str, Any]:
    """Keys added in later versions must not make an older config.json crash the app:
    start from the defaults, overlay what was saved, then migrate what moved."""
    merged = _default_config()
    merged.update(loaded)
    _migrate_outputs(merged, loaded)
    _migrate_levels(merged, loaded)
    _migrate_stop_all_hotkey(merged, loaded)
    _clean_library(merged)
    return merged


def cover_path(collection_id: str) -> str:
    """Schritt C: a folder's or playlist's own cover, relative to the data dir."""
    return f"covers/{collection_id}.png"


def _clean_cover(value: Any, collection_id: str) -> str | None:
    """Only the collection's own file counts; anything hand-edited elsewhere is dropped."""
    return value if value == cover_path(collection_id) else None


def _clean_library(merged: dict[str, Any]) -> None:
    """Bibliothek 2.0: drop what a hand-edited file broke instead of crashing later.
    Folders with a missing parent, or caught in a loop, move to the root; sounds pointing at a missing
    folder lose the folder; playlists keep each existing entry once."""
    folders: list[dict] = []
    ids: set[str] = set()
    for f in merged.get("folders") if isinstance(merged.get("folders"), list) else []:
        if not isinstance(f, dict):
            continue
        fid, name = f.get("id"), clean_collection_name(f.get("name"))
        if not isinstance(fid, str) or not COLLECTION_ID_RE.fullmatch(fid) or fid in ids or name is None:
            continue
        ids.add(fid)
        parent = f.get("parent_id")
        folders.append({"id": fid, "name": name, "parent_id": parent if isinstance(parent, str) else None,
                        "cover": _clean_cover(f.get("cover"), fid)})
    for f in folders:
        if f["parent_id"] not in ids or f["parent_id"] == f["id"]:
            f["parent_id"] = None
    parent_of = {f["id"]: f["parent_id"] for f in folders}
    for f in folders:  # a loop (a -> b -> a) would make every walk up circle: cut it here
        cur, seen = parent_of[f["id"]], {f["id"]}
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parent_of[cur]
        if cur is not None:
            f["parent_id"] = parent_of[f["id"]] = None
    merged["folders"] = folders

    playlists: list[dict] = []
    pids: set[str] = set()
    for pl in merged.get("playlists") if isinstance(merged.get("playlists"), list) else []:
        if not isinstance(pl, dict):
            continue
        pid, name = pl.get("id"), clean_collection_name(pl.get("name"))
        if not isinstance(pid, str) or not COLLECTION_ID_RE.fullmatch(pid) or pid in pids or name is None:
            continue
        pids.add(pid)
        playlists.append({"id": pid, "name": name, "item_ids": list(pl.get("item_ids") or [])
                          if isinstance(pl.get("item_ids"), list) else [],
                          "cover": _clean_cover(pl.get("cover"), pid)})

    sounds = merged.get("sounds") if isinstance(merged.get("sounds"), list) else []
    sound_ids = {s.get("id") for s in sounds if isinstance(s, dict)}
    for pl in playlists:
        items: list[str] = []
        for item in pl["item_ids"]:
            if isinstance(item, str) and item in sound_ids and item not in items:
                items.append(item)
        pl["item_ids"] = items
    merged["playlists"] = playlists

    for s in sounds:
        if not isinstance(s, dict):
            continue
        folder = s.get("folder_id")
        s["folder_id"] = folder if isinstance(folder, str) and folder in ids else None
        s["tags"] = normalize_tags(s.get("tags"))
        s["favorite"] = s.get("favorite") is True
        s["cover"] = _clean_cover(s.get("cover"), str(s.get("id")))  # own cover, hand check 2026-10-03b


def _migrate_stop_all_hotkey(merged: dict[str, Any], loaded: dict[str, Any]) -> None:
    """The stop-all default moved from the old AltGr-typing combo to Strg+ß (spec
    C9). Migrate a config that still has the old default - but only once: a user who
    later deliberately picks the old combo again must not be reverted on every load.
    Also skip the migration if a sound already owns ctrl+ß (a rare but real
    conflict), leaving the user's old stop-all combo in place rather than silently
    stealing a sound's hotkey."""
    if loaded.get("stop_all_migrated"):
        return
    if merged.get("stop_all_hotkey") == OLD_STOP_ALL_DEFAULT:
        from .hotkeys import find_hotkey_conflict
        new_default = DEFAULT_CONFIG["stop_all_hotkey"]
        if not find_hotkey_conflict(new_default, merged.get("sounds") or [], None, None):
            merged["stop_all_hotkey"] = new_default
    merged["stop_all_migrated"] = True


def _migrate_outputs(merged: dict[str, Any], loaded: dict[str, Any]) -> None:
    """Move the pre-matrix settings into config["outputs"], once.

    Idempotent: a config that already carries an entry keeps it untouched, so a
    second load never overwrites what the user changed in the meantime."""
    if "mic_gain" in loaded and "default_mic_gain" not in loaded:
        merged["default_mic_gain"] = clamp_gain(loaded["mic_gain"])
    if "mic_passthrough" in loaded and "default_mic" not in loaded:
        merged["default_mic"] = bool(loaded["mic_passthrough"])

    outputs = merged.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}
    merged["outputs"] = outputs

    if MONITOR_KEY not in outputs:
        monitor = dict(DEFAULT_MONITOR_OUTPUT)
        if "monitor_volume" in loaded:
            monitor["sounds_gain"] = clamp_gain(loaded["monitor_volume"])
        outputs[MONITOR_KEY] = monitor

    # output_mode, mic_passthrough, mic_gain and monitor_volume are replaced by the
    # matrix; drop them so the file stops carrying values nothing reads. monitor_volume
    # is only popped here, AFTER the block above has already copied it into the
    # monitor row's sounds_gain - the dock's old "Mithören" slider is gone, the
    # Kopfhörer row on Einstellungen is the only place that volume lives now. That
    # copied value does not stick around: _migrate_levels() then resets sounds_gain
    # to 0.5 for every pre-v2 config, since it was hand-tuned for unnormalized audio.
    for gone in ("output_mode", "mic_passthrough", "mic_gain", "monitor_volume"):
        merged.pop(gone, None)


def _migrate_levels(merged: dict[str, Any], loaded: dict[str, Any]) -> None:
    """Version 2 measures every sound and levels the voice itself. The old per-cable
    gains were hand-tuned to 2-6 % to tame unnormalized songs and would now make the
    sounds nearly inaudible, so they are reset to neutral - once, for configs saved
    before version 2. The same is true of every sound's own `volume` (also hand-tuned
    down, e.g. 0.02-0.5) and of the headphone row's `sounds_gain`: both were tuned to
    compensate for unnormalized audio, not to set a deliberate listening level, so
    stacked with normalization they would leave sounds 20-40 dB under the voice.
    Both are reset too - the headphones to the app default, not to the user's old
    value."""
    try:
        version = int(loaded.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    if version >= 2:
        return
    outputs = merged.get("outputs")
    if isinstance(outputs, dict):
        for key, entry in outputs.items():
            if key == MONITOR_KEY or not isinstance(entry, dict):
                continue
            entry["mic_gain"] = 1.0
            entry["sounds_gain"] = 1.0
        monitor = outputs.get(MONITOR_KEY)
        if isinstance(monitor, dict):
            monitor["sounds_gain"] = DEFAULT_MONITOR_OUTPUT["sounds_gain"]
    sounds = merged.get("sounds")
    if isinstance(sounds, list):
        for sound in sounds:
            if isinstance(sound, dict):
                sound["volume"] = 1.0
    merged["version"] = CONFIG_VERSION


def load_config() -> dict[str, Any]:
    global last_load_was_reset
    last_load_was_reset = False
    path = config_path()
    if not path.exists():
        config = _default_config()
        save_config(config)
        return config
    try:
        with open(path, encoding="utf-8") as f:
            return _with_defaults(json.load(f))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("config.json is corrupt (%s), moved to config.json.bak", exc)
        try:
            os.replace(path, path.with_name("config.json.bak"))
        except OSError:
            # The file may be locked or the data directory may have become
            # read-only. Starting with defaults is safer than aborting startup.
            log.warning("could not preserve the corrupt config.json", exc_info=True)
        last_load_was_reset = True
        config = _default_config()
        try:
            save_config(config)
        except OSError:
            log.warning("could not write replacement config.json", exc_info=True)
        return config
    except OSError as exc:
        # A transient read/permission failure should not make the window
        # unusable. Do not overwrite the file because its contents may be fine.
        log.warning("could not read config.json (%s), using defaults", exc)
        return _default_config()


def save_config(config: dict[str, Any]) -> None:
    """Atomic: write config.json.tmp, then os.replace it over config.json, so a
    crash or full disk mid-write never leaves a half-written config behind."""
    path = config_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def clamp_volume(value: Any) -> float:
    """Per-sound volume in [0, MAX_SOUND_VOLUME]; anything non-numeric or
    non-finite (NaN, inf) becomes the neutral 1.0."""
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(volume):
        return 1.0
    return min(max(volume, 0.0), MAX_SOUND_VOLUME)


def clamp_gain(value: Any) -> float:
    """Gain in [0, MAX_OUTPUT_GAIN]; anything non-numeric or non-finite becomes 1.0."""
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(gain):
        return 1.0
    return min(max(gain, 0.0), MAX_OUTPUT_GAIN)


def output_settings(cfg: dict[str, Any], key: str, is_monitor: bool = False,
                    is_voicemeeter: bool = False) -> dict[str, Any]:
    """The five settings for one target, always complete. Reading never writes.

    VoiceMeeter starts with the microphone OFF: it does its own mixing, and taking the
    microphone from here would take it away from VoiceMeeter. The user can switch it on."""
    base = dict(DEFAULT_MONITOR_OUTPUT if is_monitor else DEFAULT_OUTPUT)
    if not is_monitor:
        base["mic"] = False if is_voicemeeter else bool(cfg.get("default_mic", True))
        base["mic_gain"] = clamp_gain(cfg.get("default_mic_gain", 1.0))
    stored = (cfg.get("outputs") or {}).get(key)
    if isinstance(stored, dict):
        # F1 (Final-Fix, 2026-09-29): ein gespeicherter Eintrag ohne den Schluessel
        # `music` (jede Config vor diesem Feature) soll nicht stumpf auf True landen -
        # er folgt `sounds`, damit ein Kabel, das vorher stumm geschaltet war, nach dem
        # Update nicht ploetzlich Musik ins Gespraech mischt. Ein Eintrag OHNE `sounds`
        # (kein Kabel je gesehen) bleibt beim Default True.
        if not is_monitor and "music" not in stored and "sounds" in stored:
            base["music"] = bool(stored["sounds"])
        base.update({k: stored[k] for k in base if k in stored})
    return {
        "mic": bool(base["mic"]), "mic_gain": clamp_gain(base["mic_gain"]),
        "sounds": bool(base["sounds"]), "sounds_gain": clamp_gain(base["sounds_gain"]),
        "music": False if is_monitor else bool(base["music"]),
    }


def is_voicemeeter_key(key: str) -> bool:
    return "voicemeeter" in key.lower()


def set_output_settings(cfg: dict[str, Any], key: str, **changes: Any) -> dict[str, Any]:
    """Merge `changes` into one target's settings and return the complete result."""
    is_monitor = key == MONITOR_KEY
    current = output_settings(cfg, key, is_monitor=is_monitor,
                              is_voicemeeter=is_voicemeeter_key(key))
    for name, value in changes.items():
        if name in ("mic", "sounds", "music"):
            current[name] = bool(value)
        elif name in ("mic_gain", "sounds_gain"):
            current[name] = clamp_gain(value)
    if is_monitor:
        current["music"] = False
    cfg.setdefault("outputs", {})[key] = current
    return dict(current)


def new_sound_id() -> str:
    return str(uuid.uuid4())


def find_sound(config: dict[str, Any], sound_id: str) -> dict[str, Any] | None:
    for s in config["sounds"]:
        if s["id"] == sound_id:
            return s
    return None


def unique_name(config: dict[str, Any], name: str, exclude_id: str | None = None) -> str:
    """Append ' (2)', ' (3)', ... if name already exists among sounds (other
    than the sound with id `exclude_id`, e.g. the one being renamed)."""
    existing = {s["name"] for s in config["sounds"] if s.get("id") != exclude_id}
    if name not in existing:
        return name
    n = 2
    while f"{name} ({n})" in existing:
        n += 1
    return f"{name} ({n})"
