"""Wer darf was: zwei Rollen mit Fähigkeiten statt einer Liste von Befehlsklassen (Spec §7).

Der Vollschlüssel des Fensters entsteht bei jedem Start neu und wird nie geschrieben.
Der Ansichtsschlüssel liegt in der Config, weil ein Lesezeichen im Steam-Overlay auch
morgen noch funktionieren soll; er gilt nur auf 127.0.0.1 und darf nur abspielen.

Warum Fähigkeiten: die Erlaubnisliste von Befehlsklassen musste für jedes neue Feature
nachgefasset werden. Musik-Transport gehört zu PLAYBACK, also funktioniert ein Musik-Tab
im Browser später ohne Änderung an den Rollen. Und jeder Befehl MUSS einer Fähigkeit
zugeordnet sein - `tests/test_access_logic.py` erzwingt das -, ein neuer Befehl ist damit
verboten, bis er bewusst eingeordnet wird.
"""

from __future__ import annotations

import logging
import secrets

from .protocol import (
    AddToPlaylist, ClearCollectionCover, CreateFolder, CreatePlaylist, DeleteFolder, DeletePlaylist, MoveFolder, MoveToFolder,
    RequestSetCollectionCover, SetCollectionCover,
    RemoveFromPlaylist, RenameFolder, RenamePlaylist, SetFavorite, SetTags,
    AddSound, CheckForUpdates, ClearSoundTrim, CompleteOnboarding, DeleteSound, ExportSounds,
    ImportPack, InstallUpdate, LoadWaveform, Play, PreviewTrim, RegenerateViewToken,
    RemoveHotkey, RenameSound,
    RequestAddSound, RequestExportTrimmed,
    RequestExportSounds, RequestImportPack, RequestSetSoundIcon, Rescan,
    ResumeHotkeys, RunSignalCheck, SetAutostart, SetDiscordMusic, SetDiscordOutput,
    SetDiscordSounds, SetHotkey, SetKlangbild, SetLevels, SetMicrophone,
    SetMusicBus, SetMusicBusGain, SetOutput, SetOnboardingActive, SetSidebarPins, SetSoundCategory,
    SetSoundIcon, SetSoundTrim, SetSoundVolume, SetStopAllHotkey, ToggleMicMute,
    SpotifyLoadAlbum, SpotifyLoadLibrary, SpotifyLoadPlaylist, SpotifyLogin, SpotifyLogout,
    SpotifySearch, SpotifySearchMore, Stop, StopAll, SuspendHotkeys,
    SpotifyPlay, SpotifyPause, SpotifyResume, SpotifyNext, SpotifyPrevious,
    SpotifySeek, SpotifySetVolume, SpotifySetShuffle, SpotifySetRepeat,
    SpotifyAddToQueue, SpotifyTransfer, SpotifyLoadDevices,
    SpotifyLoadLibraryMore, SpotifyLoadRecent, SpotifySetSaved,
)

log = logging.getLogger(__name__)

ROLE_WINDOW = "window"
ROLE_VIEW = "view"
VIEW_TOKEN_KEY = "view_token"

PLAYBACK = "playback"
CAPTURE = "capture"
LIBRARY = "library"
SETTINGS = "settings"
DIAGNOSTICS = "diagnostics"
UPDATES = "updates"
ONBOARDING = "onboarding"
VOICE = "voice"
MUSIC = "music"            # suchen und ansehen - darf auch die Ansicht (Jans Entscheidung)
MUSIC_ACCOUNT = "music_account"  # anmelden und abmelden - nur das Fenster

ALL_CAPABILITIES = frozenset({PLAYBACK, CAPTURE, LIBRARY, SETTINGS, DIAGNOSTICS,
                              UPDATES, ONBOARDING, VOICE, MUSIC, MUSIC_ACCOUNT})

# Every command belongs to exactly one capability. A command missing here is refused
# for every role, and `test_every_command_has_a_capability` fails, so it cannot happen
# silently.
COMMAND_CAPABILITY: dict[type, str] = {
    Play: PLAYBACK, Stop: PLAYBACK, StopAll: PLAYBACK,
    # Musik-Bus (Ton anderer Apps): an/aus und Pegel sind Transport wie Play/Stop -
    # deshalb duerfen Fenster UND Ansicht beides (Spec "musik-bus-kern" §2).
    SetMusicBus: PLAYBACK, SetMusicBusGain: PLAYBACK,
    # muting Discord is part of playing sounds for friends (Jan, 2026-09-27); it moves
    # only the sounds switch of Discord's cable, never a level or a device
    SetDiscordSounds: PLAYBACK, SetDiscordMusic: PLAYBACK,
    SuspendHotkeys: CAPTURE, ResumeHotkeys: CAPTURE,
    AddSound: LIBRARY, DeleteSound: LIBRARY, RenameSound: LIBRARY,
    SetSoundIcon: LIBRARY, SetSoundVolume: LIBRARY, SetSoundCategory: LIBRARY,
    SetHotkey: LIBRARY,
    RemoveHotkey: LIBRARY, SetStopAllHotkey: LIBRARY, SetSidebarPins: LIBRARY, ExportSounds: LIBRARY,
    ImportPack: LIBRARY,
    # Bibliothek 2.0: ordering the sounds is managing the library (the view may not)
    CreateFolder: LIBRARY, RenameFolder: LIBRARY, DeleteFolder: LIBRARY, MoveFolder: LIBRARY, MoveToFolder: LIBRARY,
    CreatePlaylist: LIBRARY, RenamePlaylist: LIBRARY, DeletePlaylist: LIBRARY,
    AddToPlaylist: LIBRARY, RemoveFromPlaylist: LIBRARY, SetTags: LIBRARY, SetFavorite: LIBRARY,
    RequestSetCollectionCover: LIBRARY, SetCollectionCover: LIBRARY, ClearCollectionCover: LIBRARY,
    # Die vier Request*-Befehle (LIBRARY): die Seite bittet, Python oeffnet den Dialog.
    RequestAddSound: LIBRARY, RequestImportPack: LIBRARY,
    RequestExportSounds: LIBRARY, RequestSetSoundIcon: LIBRARY,
    # C8 Kürzen (Z7): ändert die Bibliothek wie Umbenennen - nur das Fenster. Auch das
    # Vorhören und die Wellenform, weil nur der Editor sie braucht.
    LoadWaveform: LIBRARY, PreviewTrim: LIBRARY, SetSoundTrim: LIBRARY,
    ClearSoundTrim: LIBRARY, RequestExportTrimmed: LIBRARY,
    SetOutput: SETTINGS, SetLevels: SETTINGS, SetKlangbild: SETTINGS, SetMicrophone: SETTINGS,
    SetDiscordOutput: SETTINGS,
    ToggleMicMute: SETTINGS, SetAutostart: SETTINGS, RegenerateViewToken: SETTINGS,
    Rescan: DIAGNOSTICS, RunSignalCheck: DIAGNOSTICS,
    CheckForUpdates: UPDATES, InstallUpdate: UPDATES,
    CompleteOnboarding: ONBOARDING, SetOnboardingActive: ONBOARDING,
    # Musik zerfaellt bewusst in zwei Rechte. Suchen und Ansehen darf auch die Ansicht:
    # im Steam-Overlay soll man im Spiel stoebern koennen, ohne das Konto zu wechseln.
    # Ein Konto verbinden oder trennen darf nur das Fenster - ein Lesezeichen im Overlay
    # soll kein Konto anfassen.
    SpotifySearch: MUSIC, SpotifySearchMore: MUSIC,
    SpotifyLoadLibrary: MUSIC, SpotifyLoadPlaylist: MUSIC, SpotifyLoadAlbum: MUSIC,
    SpotifyLogin: MUSIC_ACCOUNT, SpotifyLogout: MUSIC_ACCOUNT,
    # F2-Transport (Spec §13.1): PLAYBACK, damit auch die Ansicht steuern darf.
    SpotifyPlay: PLAYBACK, SpotifyPause: PLAYBACK, SpotifyResume: PLAYBACK,
    SpotifyNext: PLAYBACK, SpotifyPrevious: PLAYBACK, SpotifySeek: PLAYBACK,
    SpotifySetVolume: PLAYBACK, SpotifySetShuffle: PLAYBACK, SpotifySetRepeat: PLAYBACK,
    SpotifyAddToQueue: PLAYBACK, SpotifyTransfer: PLAYBACK, SpotifyLoadDevices: PLAYBACK,
    # F3 (Spec §14): mehr laden und Zuletzt gespielt sind Lesen wie Suchen/Ansehen -
    # die Ansicht darf beides. Einen Titel speichern aendert das Konto, bleibt Fenster-Sache.
    SpotifyLoadLibraryMore: MUSIC, SpotifyLoadRecent: MUSIC,
    SpotifySetSaved: MUSIC_ACCOUNT,
}

ROLE_CAPABILITIES: dict[str, frozenset] = {
    ROLE_WINDOW: ALL_CAPABILITIES,
    # Die Ansicht darf abspielen, Musik durchsuchen und ansehen - und nichts sonst.
    ROLE_VIEW: frozenset({PLAYBACK, MUSIC}),
}


class Access:
    def __init__(self, store, *, token_factory=secrets.token_urlsafe):
        """Runs on the core thread: it reads and writes the store."""
        self._store = store
        self._token_factory = token_factory
        self.window_token = self._new_token()
        self.view_token = self._load_or_create_view_token()

    def _new_token(self) -> str:
        return self._token_factory(32)

    def _load_or_create_view_token(self) -> str:
        token = self._store.data.get(VIEW_TOKEN_KEY)
        if isinstance(token, str) and token:
            return token
        token = self._new_token()
        self._store.data[VIEW_TOKEN_KEY] = token
        self._store.save_now()
        return token

    def role_for(self, token) -> str | None:
        if not isinstance(token, str) or not token:
            return None
        # bytes, not str: compare_digest raises on a non-ASCII str, and the key arrives
        # in a query string that can carry any character
        given = token.encode("utf-8")
        if secrets.compare_digest(given, self.window_token.encode("utf-8")):
            return ROLE_WINDOW
        if secrets.compare_digest(given, self.view_token.encode("utf-8")):
            return ROLE_VIEW
        return None

    def may_send(self, role: str, command) -> bool:
        capability = COMMAND_CAPABILITY.get(type(command))
        if capability is None:
            return False
        return capability in ROLE_CAPABILITIES.get(role, frozenset())

    def rotate_view_token(self) -> str:
        self.view_token = self._new_token()
        self._store.data[VIEW_TOKEN_KEY] = self.view_token
        self._store.save_now()
        log.info("view key rotated; the old link no longer works")
        return self.view_token
