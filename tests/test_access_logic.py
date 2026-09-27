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


def test_the_view_may_not_manage_anything():
    a = build()
    for cmd in (p.AddSound("x", "X"), p.ImportPack("x"), p.ExportSounds("x"),
                p.SetSoundIcon("s", "x"), p.DeleteSound("s"), p.RenameSound("s", "n"),
                p.SetSoundVolume("s", 0.5), p.SetHotkey("s", "k"), p.RemoveHotkey("s"),
                p.SetStopAllHotkey("k"),
                p.SetLevels({"sounds_offset_db": -6.0}), p.SetMicrophone("m"),
                p.SetAutostart(True),
                p.Rescan(), p.RunSignalCheck(), p.CheckForUpdates(), p.InstallUpdate(),
                p.RegenerateViewToken(),
                p.CompleteOnboarding(), p.SetOnboardingActive(False),
                p.SuspendHotkeys(), p.ResumeHotkeys(),
                p.RequestAddSound(), p.RequestImportPack(),
                p.RequestExportSounds(None), p.RequestSetSoundIcon("s")):
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


def main():
    test_every_command_has_a_capability()
    test_unknown_capability_names_are_refused()
    test_tokens_resolve_to_roles()
    test_the_view_may_play_and_will_be_able_to_drive_music()
    test_the_view_may_not_manage_anything()
    test_the_view_may_browse_music()
    test_the_view_may_not_touch_the_music_account()
    test_the_window_may_send_everything()
    test_the_view_token_survives_a_restart_and_can_be_rotated()
    test_the_view_token_survives_a_write_and_read_of_the_config()
    print("\nALL ACCESS CHECKS PASSED")


if __name__ == "__main__":
    main()
