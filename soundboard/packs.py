"""Export/import `.ruckuspack` sound packages — pure logic, no Tk.

Pack layout (zip, deflate):
    manifest.json                 {"format": "ruckuspack", "version": 1, "sounds": [<folder>, ...]}
    <folder>/meta.json            {"name": str, "volume": float, "audio": <filename in folder>}
    <folder>/<audio filename>     the sound's mp3
    <folder>/icon.png             optional

Security: every zip member name is validated before use (no absolute paths,
no ".." segments) and members are never extracted to disk by their own path —
we always read bytes via ZipFile.open() and write to filenames we compute
ourselves (a freshly generated uuid). Total uncompressed size is capped to
guard against zip bombs.

Threading: import_pack takes no live config_data — only a plain
`existing_names` snapshot (read-only; never mutated). It writes files and
returns the new sound dicts without appending them anywhere. The caller
(the GUI's Tk-thread success callback) re-dedupes each name against the
live config and appends there, since config may have changed while this
ran on a worker thread.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from soundboard import config, icons

MANIFEST_NAME = "manifest.json"
PACK_FORMAT = "ruckuspack"
PACK_VERSION = 1
ALLOWED_AUDIO_EXTENSIONS = {".mp3"}
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB, guards against zip bombs

# Errors that can surface while pulling bytes out of a member: a bad/mismatched
# CRC-32 or a truncated deflate stream (zipfile.BadZipFile), a short read
# (OSError) or a broken compressed stream zipfile didn't wrap (zlib.error).
_MEMBER_READ_ERRORS = (zipfile.BadZipFile, OSError, zlib.error)

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


class PackError(Exception):
    """Raised with a German, user-facing message for any invalid pack."""


def _not_a_pack() -> PackError:
    return PackError("Die Datei ist kein Ruckus-Radio-Paket.")


def _corrupt(detail: str) -> PackError:
    return PackError(f"Das Paket ist beschädigt: {detail}.")


def _is_safe_member_name(name: str) -> bool:
    """Reject absolute paths and any ".." path-traversal segment (zip-slip)."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute():
        return False
    return ".." not in pure.parts


def _unique_against(names: set[str], name: str) -> str:
    """Append ' (2)', ' (3)', ... if name already exists in `names`."""
    if name not in names:
        return name
    n = 2
    while f"{name} ({n})" in names:
        n += 1
    return f"{name} ({n})"


def sanitize_filename(name: str) -> str:
    """Make `name` safe to use as a Windows file name (without extension):
    strip the characters Windows forbids (\\ / : * ? " < > |), collapse
    whitespace, strip trailing dots/spaces (Windows also rejects those at the
    end of a name), and fall back to "Sound" if nothing is left."""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(" .")
    return cleaned or "Sound"


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def export_pack(config_data: dict[str, Any], data_dir: Path, sound_ids: list[str],
                 dest_path: Path) -> Path:
    """Write a .ruckuspack zip containing the given sounds. Hotkeys are not exported."""
    dest_path = Path(dest_path)
    sounds = [s for sid in sound_ids if (s := config.find_sound(config_data, sid)) is not None]

    manifest = {"format": PACK_FORMAT, "version": PACK_VERSION, "sounds": []}
    try:
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for sound in sounds:
                folder = sound["id"]
                manifest["sounds"].append(folder)

                audio_src = data_dir / sound["file"]
                if not audio_src.is_file():
                    raise PackError(f"Die Audiodatei von „{sound['name']}“ fehlt.")
                audio_name = "sound" + Path(sound["file"]).suffix.lower()
                zf.write(audio_src, f"{folder}/{audio_name}")

                icon_src = data_dir / sound.get("icon", "")
                if sound.get("icon") and icon_src.exists():
                    zf.write(icon_src, f"{folder}/icon.png")

                meta = {"name": sound["name"], "volume": sound.get("volume", 1.0), "audio": audio_name}
                zf.writestr(f"{folder}/meta.json", json.dumps(meta, ensure_ascii=False))

            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
    except BaseException:
        dest_path.unlink(missing_ok=True)  # never leave a half-written pack behind
        raise

    return dest_path


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def import_pack(pack_path: Path, existing_names: set[str], data_dir: Path) -> list[dict]:
    """Validate and import a .ruckuspack. Writes files under data_dir and
    returns the new sound dicts — does NOT append them to any config and does
    NOT mutate `existing_names` (both are the caller's job; `existing_names`
    is only a read-only snapshot used to dedup names within this call).
    New names are deduped against `existing_names` plus each other; the
    caller must still re-dedupe against its live config before appending,
    since that may have changed while this ran on a worker thread."""
    pack_path = Path(pack_path)

    try:
        zf = zipfile.ZipFile(pack_path)
    except (zipfile.BadZipFile, FileNotFoundError, OSError):
        raise _not_a_pack()

    with zf:
        try:
            infos = zf.infolist()
        except Exception:
            raise _corrupt("das Archiv konnte nicht gelesen werden")

        for info in infos:
            if not _is_safe_member_name(info.filename):
                raise _corrupt("ungültiger Dateipfad im Paket")

        # Summing the central directory's declared file_size is sufficient here:
        # zipfile truncates a read to the size declared for that member and
        # raises BadZipFile on a CRC-32 mismatch, so no member can actually
        # yield more uncompressed bytes than what's summed below.
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise _corrupt("das Paket ist zu groß")

        namelist = set(zf.namelist())
        if MANIFEST_NAME not in namelist:
            raise _not_a_pack()

        try:
            manifest_bytes = zf.read(MANIFEST_NAME)
        except _MEMBER_READ_ERRORS:
            raise _corrupt("manifest.json ist unlesbar")
        try:
            manifest = json.loads(manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _corrupt("manifest.json ist kein gültiges JSON")

        if not isinstance(manifest, dict) or manifest.get("format") != PACK_FORMAT:
            raise _not_a_pack()

        folders = manifest.get("sounds")
        if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
            raise _corrupt("manifest.json listet keine Sounds")

        prepared = []
        for folder in folders:
            if not _is_safe_member_name(folder):
                raise _corrupt("ungültiger Dateipfad im Paket")

            meta_name = f"{folder}/meta.json"
            if meta_name not in namelist:
                raise _corrupt(f"meta.json fehlt für „{folder}“")
            try:
                meta_bytes = zf.read(meta_name)
            except _MEMBER_READ_ERRORS:
                raise _corrupt(f"meta.json von „{folder}“ ist unlesbar")
            try:
                meta = json.loads(meta_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise _corrupt(f"meta.json von „{folder}“ ist kein gültiges JSON")

            name = meta.get("name")
            audio_filename = meta.get("audio")
            if not name or not audio_filename:
                raise _corrupt(f"meta.json von „{folder}“ ist unvollständig")

            audio_member = f"{folder}/{audio_filename}"
            if not _is_safe_member_name(audio_filename) or audio_member not in namelist:
                raise _corrupt(f"Audiodatei fehlt für „{name}“")

            ext = Path(audio_filename).suffix.lower()
            if ext not in ALLOWED_AUDIO_EXTENSIONS:
                raise _corrupt(f"nicht unterstütztes Audioformat „{ext}“ bei „{name}“")

            # Read (and thereby CRC-check) the audio bytes now, during
            # validation — before anything is written — so a corrupted or
            # truncated entry anywhere in the pack fails before any file for
            # any sound in this pack is written to disk.
            try:
                audio_bytes = zf.read(audio_member)
            except _MEMBER_READ_ERRORS:
                raise _corrupt(f"Audiodatei von „{name}“ ist unlesbar")

            volume = config.clamp_volume(meta.get("volume", 1.0))

            icon_member = f"{folder}/icon.png"
            icon_bytes = None
            if icon_member in namelist:
                try:
                    icon_bytes = zf.read(icon_member)
                except _MEMBER_READ_ERRORS:
                    raise _corrupt(f"Icon von „{name}“ ist unlesbar")

            prepared.append({
                "name": str(name),
                "volume": volume,
                "audio_bytes": audio_bytes,
                "audio_ext": ext,
                "icon_bytes": icon_bytes,
            })

        # All validated — now actually write files (only computed destination
        # names are ever used, never paths taken from the zip). If anything
        # goes wrong partway through, delete every file this call wrote and
        # fail the whole import atomically rather than leave a half-written pack.
        used_names = set(existing_names)
        new_sounds: list[dict] = []
        written_paths: list[Path] = []
        try:
            for item in prepared:
                sound_id = config.new_sound_id()
                dest_audio = data_dir / "sounds" / f"{sound_id}{item['audio_ext']}"
                dest_audio.parent.mkdir(parents=True, exist_ok=True)
                dest_audio.write_bytes(item["audio_bytes"])
                written_paths.append(dest_audio)

                dest_icon = data_dir / "icons" / f"{sound_id}.png"
                dest_icon.parent.mkdir(parents=True, exist_ok=True)
                icon_image = None
                if item["icon_bytes"] is not None:
                    try:
                        with Image.open(io.BytesIO(item["icon_bytes"])) as img:
                            img.verify()
                        with Image.open(io.BytesIO(item["icon_bytes"])) as img:
                            icon_image = icons.crop_to_circle(img, icons.ICON_SIZE)
                    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                        icon_image = None
                if icon_image is None:
                    icon_image = icons.placeholder_icon(item["name"])
                icon_image.save(dest_icon, format="PNG")
                written_paths.append(dest_icon)

                name = _unique_against(used_names, item["name"])
                used_names.add(name)
                sound = {
                    "id": sound_id,
                    "name": name,
                    "file": f"sounds/{sound_id}{item['audio_ext']}",
                    "icon": f"icons/{sound_id}.png",
                    "hotkey": None,
                    "volume": item["volume"],
                }
                new_sounds.append(sound)
        except Exception as exc:
            for path in written_paths:
                path.unlink(missing_ok=True)
            if isinstance(exc, PackError):
                raise
            raise _corrupt("Schreiben der Sound-Dateien ist fehlgeschlagen") from exc

        return new_sounds
