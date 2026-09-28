"""Das einzige Format zwischen App-Kern und Oberflaeche.

Befehle gehen von der Oberflaeche (oder den Hotkeys) an den Kern, Ereignisse vom Kern
an die Oberflaeche. Jede Nachricht ist eine eingefrorene Dataclass, deren Felder nur
JSON-Typen enthalten. Dadurch spricht die heutige Tk-Oberflaeche dieselbe Sprache wie
die spaetere Web-UI: to_json/from_json uebersetzen verlustfrei.

Konvention: Sequenz-Felder sind Tupel (from_json macht aus einer Liste auf oberster
Ebene wieder ein Tupel); innerhalb von dict-Feldern stehen Listen.

Neue Nachricht: Klasse mit @message dekorieren und von Command oder Event erben. Eine
inkompatible Aenderung an einer bestehenden Nachricht erhoeht PROTOCOL_VERSION.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, ClassVar

PROTOCOL_VERSION = 1
NOTICE_LEVELS = ("hint", "info", "error")

_REGISTRY: dict[str, type] = {}


class ProtocolError(ValueError):
    """A message that cannot be encoded or decoded."""


class Command:
    kind: ClassVar[str] = "command"


class Event:
    kind: ClassVar[str] = "event"
    # Deklariertes Zusammenfassen: sag selbst, ob ein neueres Ereignis desselben Typs
    # ein älteres im selben Bündel ersetzt, und wodurch es sich unterscheidet. Die
    # Brücke pflegt damit keine Liste von Ereignisnamen.
    coalesce: ClassVar[bool] = False
    coalesce_by: ClassVar[tuple[str, ...]] = ()


def message(cls: type) -> type:
    """Freeze `cls` as a dataclass and register it under its class name."""
    if cls.__name__ in _REGISTRY:
        raise ProtocolError(f"duplicate message name {cls.__name__}")
    frozen = dataclasses.dataclass(frozen=True)(cls)
    _REGISTRY[cls.__name__] = frozen
    return frozen


# ---- commands: interface -> core ----

@message
class AddSound(Command):
    path: str
    name: str
    icon_path: str | None = None


@message
class DeleteSound(Command):
    sound_id: str


@message
class RenameSound(Command):
    sound_id: str
    name: str


@message
class SetSoundIcon(Command):
    sound_id: str
    icon_path: str


@message
class SetSoundVolume(Command):
    sound_id: str
    volume: float


@message
class SetHotkey(Command):
    sound_id: str
    hotkey: str


@message
class RemoveHotkey(Command):
    sound_id: str


@message
class SetStopAllHotkey(Command):
    hotkey: str


@message
class SuspendHotkeys(Command):
    pass


@message
class ResumeHotkeys(Command):
    pass


@message
class ExportSounds(Command):
    target_path: str
    sound_ids: tuple[str, ...] | None = None


@message
class ImportPack(Command):
    path: str


@message
class Play(Command):
    sound_id: str
    volume: float | None = None
    preview: bool = False  # "Probehören": only the headphones, never the voice chat


@message
class Stop(Command):
    sound_id: str


@message
class StopAll(Command):
    pass


@message
class SetOutput(Command):
    key: str
    changes: dict


@message
class SetLevels(Command):
    changes: dict


@message
class SetMicrophone(Command):
    name: str
    apply: bool = True  # False: only remember the choice (e.g. while onboarding_active)


@message
class SetDiscordOutput(Command):
    """Which cable Discord records from (an output row key); "" = the primary cable.
    Ruckus cannot read Discord's input device, so with two cables the user names it."""
    key: str


@message
class SetDiscordSounds(Command):
    """Sounds on/off for the cable Discord records from - the dock button. Its own
    command so the browser view may send it without SetOutput (levels, devices)."""
    on: bool


@message
class ToggleMicMute(Command):
    pass


@message
class RunSignalCheck(Command):
    pass


@message
class Rescan(Command):
    pass


@message
class SetOnboardingActive(Command):
    active: bool


@message
class CompleteOnboarding(Command):
    pass


@message
class SetAutostart(Command):
    enabled: bool


@message
class CheckForUpdates(Command):
    pass


@message
class InstallUpdate(Command):
    pass


@message
class RegenerateViewToken(Command):
    """Neuer Ansichtsschlüssel (Spec §7). Alte Links und offene Ansichten hören auf."""
    pass


# ---- requests: the page asks, Python opens the dialog (spec §9) ----
# The interface never sends a file path; it asks for a file and the core chooses it.

@message
class RequestAddSound(Command):
    pass


@message
class RequestImportPack(Command):
    pass


@message
class RequestExportSounds(Command):
    sound_id: str | None = None


@message
class RequestSetSoundIcon(Command):
    sound_id: str


@message
class SetMusicBus(Command):
    """Musik-Bus an/aus: der Ton anderer Apps wird mitgeschnitten und klingt fuer
    andere wie ein Sound (Spec "musik-bus-kern"). Transport wie Play/Stop."""

    enabled: bool


@message
class SetMusicBusGain(Command):
    gain: float


@message
class SpotifyLogin(Command):
    pass


@message
class SpotifyLogout(Command):
    pass


@message
class SpotifySearch(Command):
    query: str
    search_type: str = "track"  # track | album | playlist | artist
    offset: int = 0


@message
class SpotifySearchMore(Command):
    """The next page of the same search ("mehr laden")."""

    query: str
    search_type: str
    offset: int


@message
class SpotifyLoadLibrary(Command):
    pass


@message
class SpotifyLoadPlaylist(Command):
    playlist_id: str
    offset: int = 0


# ---- Spotify F2: Wiedergabe (Spec §13.2). Alles PLAYBACK in access.py. ----

@message
class SpotifyPlay(Command):
    uris: tuple[str, ...] = ()   # einzelne Titel
    context_uri: str = ""        # Playlist/Album
    offset_uri: str = ""         # Starttitel im Kontext


@message
class SpotifyPause(Command):
    pass


@message
class SpotifyResume(Command):
    pass


@message
class SpotifyNext(Command):
    pass


@message
class SpotifyPrevious(Command):
    pass


@message
class SpotifySeek(Command):
    position_ms: int


@message
class SpotifySetVolume(Command):
    volume_percent: int          # auf 0..100 geklemmt


@message
class SpotifySetShuffle(Command):
    on: bool


@message
class SpotifySetRepeat(Command):
    mode: str                    # off | context | track


@message
class SpotifyAddToQueue(Command):
    uri: str


@message
class SpotifyTransfer(Command):
    device_id: str
    play: bool = True


@message
class SpotifyLoadDevices(Command):
    pass


# ---- events: core -> interface ----

@message
class StateChanged(Event):
    state: dict
    coalesce: ClassVar[bool] = True


@message
class PartChanged(Event):
    """Eine Zustandsgruppe, die sich selten ändert (die Bibliothek wächst, der
    flüchtige Teil nicht). Getrennt von StateChanged, damit die Sekundenlast nicht
    mit der Bibliothek wächst. Ersetzt sich je Gruppe, nicht global."""
    group: str
    part: dict
    coalesce: ClassVar[bool] = True
    coalesce_by: ClassVar[tuple[str, ...]] = ("group",)


@message
class JobChanged(Event):
    """One job's state after a change. Jobs replace themselves per id, so a fast job
    does not flood the batch. Lives in the volatile group (they come and go)."""
    job: dict
    coalesce: ClassVar[bool] = True
    coalesce_by: ClassVar[tuple[str, ...]] = ("id",)


@message
class PlaybackStarted(Event):
    sound_id: str
    needle: float


@message
class PlaybackEnded(Event):
    sound_id: str


@message
class SoundMissing(Event):
    sound_id: str


@message
class SoundAdded(Event):
    sound_id: str


@message
class SignalCheckDone(Event):
    results: tuple


@message
class DevicesChanged(Event):
    summary: dict


@message
class HeadphonesSwitched(Event):
    name: str


@message
class UpdateAvailable(Event):
    version: str
    notes: str = ""


@message
class UpdateReady(Event):
    installer_path: str


@message
class SpotifyAuthChanged(Event):
    connected: bool
    user_name: str = ""


@message
class SpotifyError(Event):
    message: str
    level: str = "error"


@message
class Notice(Event):
    """hint: one line in the status bar; info/error: a dialog the user confirms."""

    text: str
    level: str = "hint"

    def __post_init__(self) -> None:
        if self.level not in NOTICE_LEVELS:
            raise ProtocolError(f"notice level must be one of {NOTICE_LEVELS}, got {self.level!r}")


# ---- JSON ----

def coalesce_key(message: dict) -> str | None:
    """Der Schlüssel, unter dem ein eingereihtes Ereignis ein älteres ersetzt - oder
    None, wenn es nicht zusammengefasst wird. Rein und damit leicht zu testen."""
    cls = _REGISTRY.get(message.get("type"))
    if cls is None or not getattr(cls, "coalesce", False):
        return None
    by = getattr(cls, "coalesce_by", ())
    if not by:
        return message["type"]
    data = message.get("data") or {}
    # A field may sit at the top level (PartChanged.group) or inside a payload the event
    # carries (JobChanged.job.id) - both are declared with the same key.
    return message["type"] + ":" + "|".join(
        str(data.get(field) if field in data else (data.get("job") or {}).get(field))
        for field in by)


def _check(value: Any, path: str) -> Any:
    """Validate a JSON value; tuples become lists on the way out."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{path}: {value!r} is not a finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_check(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(f"{path}: dict keys must be strings, got {key!r}")
            out[key] = _check(item, f"{path}.{key}")
        return out
    raise ProtocolError(f"{path}: {type(value).__name__} is not a JSON value")


def to_json(msg: Any) -> dict:
    name = type(msg).__name__
    if _REGISTRY.get(name) is not type(msg):
        raise ProtocolError(f"{name} is not a registered message")
    data = {f.name: _check(getattr(msg, f.name), f"{name}.{f.name}")
            for f in dataclasses.fields(msg)}
    return {"type": name, "kind": msg.kind, "v": PROTOCOL_VERSION, "data": data}


def from_json(data: Any) -> Any:
    if not isinstance(data, dict):
        raise ProtocolError("a message must be a JSON object")
    name = data.get("type")
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProtocolError(f"unknown message type {name!r}")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {data.get('v')!r}, expected {PROTOCOL_VERSION}")
    fields = data.get("data")
    if not isinstance(fields, dict):
        raise ProtocolError(f"{name}: 'data' must be an object")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise ProtocolError(f"{name}: unknown field(s) {unknown}")
    values = {key: tuple(value) if isinstance(value, list) else value
              for key, value in fields.items()}
    for key, value in values.items():
        _check(value, f"{name}.{key}")
    try:
        return cls(**values)
    except TypeError as exc:
        raise ProtocolError(f"{name}: {exc}") from exc
