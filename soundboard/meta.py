"""Per-sound metadata stored under <data_dir>/meta/<sound_id>.json.

Keeps optional per-sound overrides that live outside the main config.json so the
config stays small and sound metadata survives config-only migrations::

    {
        "gain": 1.0,            # 0..2 (percent UI: 0..200 %)
        "name_override": null  # optional display name used by the tile
    }

The meta directory is created on demand, never assumed to exist, and is only
touched on the Tk thread (RuckusRadioApp calls these methods from UI code).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from soundboard import config

GAMEPAD_MAX_GAIN = 2.0
DEFAULT_GAIN = 1.0
META_FILENAME = ".ruckus-sound-meta.json"


class MetaError(Exception):
    """Raised when meta read/write failed in a way we cannot silently paper over."""


def meta_dir(data_dir: Path) -> Path:
    """Meta storage under <data_dir>/meta. Created on demand."""
    directory = data_dir / "meta"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def meta_path(data_dir: Path, sound_id: str) -> Path:
    return meta_dir(data_dir) / f"{sound_id}.json"


def load_meta(data_dir: Path, sound_id: str) -> dict[str, Any]:
    """Return the meta dict for `sound_id`, falling back to defaults when missing."""
    path = meta_path(data_dir, sound_id)
    if not path.exists():
        return _defaults()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetaError(f"can't read meta for {sound_id!r}: {exc}") from exc
    return _merge_defaults(payload)


def save_meta(data_dir: Path, sound_id: str, payload: dict[str, Any]) -> None:
    """Atomically write meta for `sound_id`.

    Payload is merged with defaults so a partial dict (e.g. only ``{"gain": 0.8}``)
    still yields a complete file.
    """
    merged = _merge_defaults(payload)
    path = meta_path(data_dir, sound_id)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # Best-effort: log in the app layer, never leak a half-written file.
        raise MetaError(f"can't write meta for {sound_id!r}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def gain(data_dir: Path, sound_id: str) -> float:
    """Per-sound gain 0..GAMEPAD_MAX_GAIN, default DEFAULT_GAIN."""
    return float(load_meta(data_dir, sound_id).get("gain", DEFAULT_GAIN))


def set_gain(data_dir: Path, sound_id: str, value: float) -> dict[str, Any]:
    """Persist a new per-sound gain and return the full meta dict."""
    clamped = _clamp_gain(value)
    meta = load_meta(data_dir, sound_id)
    meta["gain"] = clamped
    save_meta(data_dir, sound_id, meta)
    return meta


def set_name_override(data_dir: Path, sound_id: str, name: str | None) -> dict[str, Any]:
    """Persist an optional display name for the tile."""
    meta = load_meta(data_dir, sound_id)
    meta["name_override"] = (name.strip() if name and name.strip() else None)
    save_meta(data_dir, sound_id, meta)
    return meta


def delete_meta(data_dir: Path, sound_id: str) -> None:
    """Drop the meta file for a sound. Missing file is not an error."""
    path = meta_path(data_dir, sound_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise MetaError(f"can't delete meta for {sound_id!r}: {exc}") from exc


def _defaults() -> dict[str, Any]:
    return {"gain": DEFAULT_GAIN, "name_override": None}


def _merge_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(_defaults())
    merged.update({k: v for k, v in payload.items() if k in merged or k in _ALLOWED_KEYS})
    return merged


_ALLOWED_KEYS = {"gain", "name_override"}


def _clamp_gain(value: float) -> float:
    return min(max(float(value), 0.0), GAMEPAD_MAX_GAIN)


def effective_gain(data_dir: Path, sound_id: str, base_gain: float) -> float:
    """Per-sound override applied on top of a target/base gain.

    Kept deliberately simple: ``base_gain * per_sound_gain``, clamped back into the
    engine's playable range so an aggressive override can never ask the stream to
    render out-of-range values that numpy clipping would otherwise hide.
    """
    per_sound = gain(data_dir, sound_id)
    raw = base_gain * per_sound
    if raw > 1.0:
        return min(raw, 1.0)
    if raw < 0.0:
        return 0.0
    return raw
