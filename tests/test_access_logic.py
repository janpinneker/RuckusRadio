"""Zwei Rollen, Fähigkeiten statt Befehlsklassen: der Browser darf nur abspielen."""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-access-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import access  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def all_commands():
    return [cls for name, cls in vars(p).items()
            if inspect.isclass(cls) and issubclass(cls, p.Command) and cls is not p.Command]


def build():
    c, _ = core_fakes.bare_core()
    return access.Access(c.store)


def test_every_command_has_a_capability():
    """Der eigentliche Schutz: ein neuer Befehl ist verboten, bis er eingeordnet wird."""
    commands = all_commands()
    assert len(commands) >= 30, len(commands)
    missing = sorted(cls.__name__ for cls in commands if cls not in access.COMMAND_CAPABILITY)
    assert missing == [], f"ohne Faehigkeit: {missing}"
    print(f"{len(commands)} Befehle, jeder hat eine Faehigkeit: OK")


def test_unknown_capability_names_are_refused():
    known = set(access.COMMAND_CAPABILITY.values())
    assert known <= access.ALL_CAPABILITIES, known - access.ALL_CAPABILITIES
    assert access.ALL_CAPABILITIES == set(access.ROLE_CAPABILITIES[access.ROLE_WINDOW])
    print("Faehigkeitsnamen sind geschlossen: OK")


def test_tokens_resolve_to_roles():
    a = build()
    assert a.role_for(a.window_token) == access.ROLE_WINDOW
    assert a.role_for(a.view_token) == access.ROLE_VIEW
    # "ä…": a query can carry any character; compare_digest raises on non-ASCII str,
    # and a key check must refuse, not throw
    for bad in ("", None, 42, "falsch", a.window_token + "x", "ä" + a.window_token[1:]):
        assert a.role_for(bad) is None, bad
    print("Schluessel loesen zu Rollen auf: OK")


def test_the_view_may_play_and_will_be_able_to_drive_music():
    a = build()
    for cmd in (p.Play("s"), p.Stop("s"), p.StopAll(),
                p.SetMusicBus(True), p.SetMusicBusGain(0.5)):
        assert a.may_send(access.ROLE_VIEW, cmd), cmd
    # Musik-Transport gehoert zu PLAYBACK, ohne die Rollen anzufassen:
    assert access.PLAYBACK in access.ROLE_CAPABILITIES[access.ROLE_VIEW]
    for cls in (p.SetMusicBus, p.SetMusicBusGain):
        assert access.COMMAND_CAPABILITY[cls] == access.PLAYBACK, cls
    print("die Ansicht darf abspielen: OK")


def test_the_view_may_mute_discord_but_not_touch_outputs():
    a = build()
    assert a.may_send(access.ROLE_VIEW, p.SetDiscordSounds(False)), "Jan: mute Discord from the browser"
    assert not a.may_send(access.ROLE_VIEW, p.SetOutput("CABLE", {"sounds": False}))
    assert not a.may_send(access.ROLE_VIEW, p.ToggleMicMute())
    print("die Ansicht darf Discord stummschalten, aber keine Ausgaenge aendern: OK")


def test_the_view_may_not_manage_anything():
    a = build()
    for cmd in (p.AddSound("x", "X"), p.ImportPack("x"), p.ExportSounds("x"),
                p.SetSoundIcon("s", "x"), p.DeleteSound("s"), p.RenameSound("s", "n"),
                p.SetSoundVolume("s", 0.5), p.SetHotkey("s", "k"), p.RemoveHotkey("s"),
                p.SetStopAllHotkey("k"),
                p.SetLevels({"sounds_offset_db": -6.0}), p.SetMicrophone("m"),
                p.SetKlangbild({"music": -14.0}), p.SetSoundCategory("s", "music"),
                p.SetAutostart(True),
                p.Rescan(), p.RunSignalCheck(), p.CheckForUpdates(), p.InstallUpdate(),
                p.RegenerateViewToken(),
                p.CompleteOnboarding(), p.SetOnboardingActive(False),
                p.SuspendHotkeys(), p.ResumeHotkeys(),
                p.RequestAddSound(), p.RequestImportPack(),
                p.RequestExportSounds(None), p.RequestSetSoundIcon("s"),
                p.LoadWaveform("s"), p.PreviewTrim("s", 0.0, 1.0), p.SetSoundTrim("s", 0.0, 1.0),
                p.ClearSoundTrim("s"), p.RequestExportTrimmed("s"),
                p.CreateFolder("f-1", "Memes"), p.CreateFolder("f-2", "Unter", "f-1", "sounds"),
                p.RenameFolder("f-1", "Neu"), p.DeleteFolder("f-1"), p.MoveToFolder(("s1", "s2"), "f-1"),
                p.MoveToFolder(("s1",), None), p.CreatePlaylist("p-1", "Lieblinge"),
                p.RenamePlaylist("p-1", "Neu"), p.DeletePlaylist("p-1"), p.AddToPlaylist("p-1", ("s1",)),
                p.RemoveFromPlaylist("p-1", ("s1",)), p.SetTags("s1", ("lustig", "kurz")),
                p.SetFavorite("s1", True),):
        # Die Spotify-Lesebefehle stehen bewusst NICHT hier: die Ansicht darf sie.
        assert not a.may_send(access.ROLE_VIEW, cmd), cmd
    assert not a.may_send("quatsch", p.Play("s"))
    print("die Ansicht darf nichts verwalten: OK")


def test_the_view_may_browse_music():
    """Suchen und Ansehen sind Lesen: im Steam-Overlay sollen die Sounds und Titel
    sichtbar sein, ohne das Spiel zu verlassen. Deshalb hat die Ansicht MUSIC."""
    a = build()
    for cmd in (p.SpotifySearch("neon"), p.SpotifySearchMore("neon", "track", 20),
                p.SpotifyLoadLibrary(), p.SpotifyLoadPlaylist("pl1")):
        assert a.may_send(access.ROLE_VIEW, cmd), cmd
    for cls in (p.SpotifySearch, p.SpotifySearchMore, p.SpotifyLoadLibrary,
                p.SpotifyLoadPlaylist):
        assert access.COMMAND_CAPABILITY[cls] == access.MUSIC, cls
    assert access.MUSIC in access.ROLE_CAPABILITIES[access.ROLE_VIEW]
    assert access.MUSIC in access.ROLE_CAPABILITIES[access.ROLE_WINDOW]
    print("die Ansicht darf Musik durchsuchen und ansehen: OK")


PLAYER_COMMANDS = (
    p.SpotifyPlay(uris=("spotify:track:t1",)), p.SpotifyPlay(context_uri="spotify:playlist:x"),
    p.SpotifyPause(), p.SpotifyResume(), p.SpotifyNext(), p.SpotifyPrevious(),
    p.SpotifySeek(1000), p.SpotifySetVolume(50), p.SpotifySetShuffle(True),
    p.SpotifySetRepeat("track"), p.SpotifyAddToQueue("spotify:track:t1"),
    p.SpotifyTransfer("dev1"), p.SpotifyLoadDevices(),
)


def test_the_view_may_drive_spotify_playback():
    """Transport ist Abspielen (Spec §13.1): die Ansicht im Steam-Overlay darf steuern."""
    a = build()
    for cmd in PLAYER_COMMANDS:
        assert access.COMMAND_CAPABILITY[type(cmd)] == access.PLAYBACK, cmd
        assert a.may_send(access.ROLE_VIEW, cmd), cmd
        assert a.may_send(access.ROLE_WINDOW, cmd), cmd
    again = p.from_json(p.to_json(p.SpotifyPlay(uris=("a", "b"))))
    assert again.uris == ("a", "b"), again
    print("die Ansicht darf Spotify steuern: OK")


def test_the_view_may_not_touch_the_music_account():
    """Anmelden und abmelden bleibt Fenster-Sache: der Ansichtsschluessel ist dauerhaft
    und liegt in einem Lesezeichen - damit darf niemand ein Konto verbinden."""
    a = build()
    for cmd in (p.SpotifyLogin(), p.SpotifyLogout()):
        assert not a.may_send(access.ROLE_VIEW, cmd), cmd
    for cls in (p.SpotifyLogin, p.SpotifyLogout):
        assert access.COMMAND_CAPABILITY[cls] == access.MUSIC_ACCOUNT, cls
    assert access.MUSIC_ACCOUNT in access.ROLE_CAPABILITIES[access.ROLE_WINDOW]
    assert access.MUSIC_ACCOUNT not in access.ROLE_CAPABILITIES[access.ROLE_VIEW]
    print("nur das Fenster darf das Musik-Konto verbinden: OK")


def test_the_window_may_send_everything():
    a = build()
    for cls in all_commands():
        instance = cls(**{f.name: ("s" if f.name.endswith("id") else None)
                          for f in __import__("dataclasses").fields(cls)})
        assert a.may_send(access.ROLE_WINDOW, instance), cls
    print("das Fenster darf alles: OK")


def test_the_view_token_survives_a_restart_and_can_be_rotated():
    c, _ = core_fakes.bare_core()
    first = access.Access(c.store)
    second = access.Access(c.store)
    assert second.view_token == first.view_token, "der Ansichtsschluessel liegt in der Config"
    assert second.window_token != first.window_token, "der Vollschluessel ist pro Start"
    old = first.view_token
    new = first.rotate_view_token()
    assert first.role_for(old) is None, "der alte Schluessel muss ungueltig sein"
    assert first.role_for(new) == access.ROLE_VIEW
    assert access.Access(c.store).view_token == new
    print("Ansichtsschluessel dauerhaft und erneuerbar: OK")


def test_the_view_token_survives_a_write_and_read_of_the_config():
    """Der Schluessel liegt in der Config: ein echter Neustart liest ihn aus config.json
    zurueck. Nur die Ablage zu merken reicht nicht - hier geht es durch die Datei."""
    from soundboard import config
    first = build()
    written = config.config_path().read_text(encoding="utf-8")
    assert first.view_token in written, "der Ansichtsschluessel muss in config.json stehen"
    loaded = config.load_config()
    assert loaded.get(access.VIEW_TOKEN_KEY) == first.view_token
    print("Ansichtsschluessel ueberlebt config.json: OK")


def test_the_view_may_load_more_library_and_recent_but_not_save_tracks():
    """F3 Task 1: mehr Bibliothek und Zuletzt gespielt sind Lesen (MUSIC, wie Suchen und
    Ansehen); ein Titel als gespeichert markieren ist Konto-Sache (MUSIC_ACCOUNT, nur
    das Fenster) - ein Lesezeichen im Overlay soll die Bibliothek des Kontos nicht aendern."""
    a = build()
    for cmd in (p.SpotifyLoadLibraryMore("playlists", 20), p.SpotifyLoadRecent()):
        assert a.may_send(access.ROLE_VIEW, cmd), cmd
    for cls in (p.SpotifyLoadLibraryMore, p.SpotifyLoadRecent):
        assert access.COMMAND_CAPABILITY[cls] == access.MUSIC, cls
    assert not a.may_send(access.ROLE_VIEW, p.SpotifySetSaved("spotify:track:t1", True))
    assert access.COMMAND_CAPABILITY[p.SpotifySetSaved] == access.MUSIC_ACCOUNT
    assert a.may_send(access.ROLE_WINDOW, p.SpotifySetSaved("spotify:track:t1", True))
    print("mehr laden und zuletzt gespielt sind Lesen, gespeichert ist Kontosache: OK")


def test_klangbild_commands_have_their_capabilities():
    """Kategorie umstellen ist Bibliothek (wie die Lautstaerke), Ziele sind Mischpult."""
    assert access.COMMAND_CAPABILITY[p.SetSoundCategory] == access.LIBRARY
    assert access.COMMAND_CAPABILITY[p.SetKlangbild] == access.SETTINGS
    print("Klangbild-Befehle haben ihre Faehigkeiten: OK")


def main():
    test_every_command_has_a_capability()
    test_unknown_capability_names_are_refused()
    test_tokens_resolve_to_roles()
    test_the_view_may_play_and_will_be_able_to_drive_music()
    test_the_view_may_mute_discord_but_not_touch_outputs()
    test_the_view_may_not_manage_anything()
    test_the_view_may_browse_music()
    test_the_view_may_drive_spotify_playback()
    test_the_view_may_not_touch_the_music_account()
    test_the_view_may_load_more_library_and_recent_but_not_save_tracks()
    test_the_window_may_send_everything()
    test_the_view_token_survives_a_restart_and_can_be_rotated()
    test_the_view_token_survives_a_write_and_read_of_the_config()
    test_klangbild_commands_have_their_capabilities()
    print("\nALL ACCESS CHECKS PASSED")


if __name__ == "__main__":
    main()
