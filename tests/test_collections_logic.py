"""Bibliothek 2.0: Ordner, Playlists, Stichwoerter, Favoriten im Kern (Spec 2026-10-01)."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-collections-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import config  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def make():
    data = config._default_config()
    data["sounds"] = [{"id": sid, "name": name, "file": f"sounds/{sid}.mp3", "icon": f"icons/{sid}.png",
                       "hotkey": None, "volume": 1.0}
                      for sid, name in (("s1", "Horn"), ("s2", "Tröte"), ("s3", "Gong"))]
    # like production: the store's data always comes through config.load_config
    return core_fakes.make_core(store_data=config._with_defaults(data))


def lib(c):
    return c.state()["library"]


def sound(c, sid):
    return next(s for s in c.state()["sounds"] if s["id"] == sid)


def notices(events):
    return [n.text for n in core_fakes.of_type(events, p.Notice)]


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_the_library_part_has_the_shape_the_page_reads():
    c, _ = make()
    assert lib(c) == {"folders": [], "playlists": [], "tracks": [], "recent_ids": [],
                      "music": {"track_id": None, "playing": False, "position": 0, "since": 0, "volume": 1}}
    print("Zustandsteil library hat die Form der Seite: OK")


def test_folders_create_rename_nest_and_persist():
    c, _ = make()
    c.send(p.CreateFolder("f-1", "  Memes  "))
    c.send(p.CreateFolder("f-2", "Unter", "f-1"))
    c.send(p.RenameFolder("f-1", "Witze"))
    assert lib(c)["folders"] == [
        {"id": "f-1", "name": "Witze", "parent_id": None, "area": "sounds", "image": None},
        {"id": "f-2", "name": "Unter", "parent_id": "f-1", "area": "sounds", "image": None},
    ], lib(c)["folders"]
    assert on_disk()["folders"] == [{"id": "f-1", "name": "Witze", "parent_id": None, "cover": None},
                                    {"id": "f-2", "name": "Unter", "parent_id": "f-1", "cover": None}]
    print("Ordner anlegen, umbenennen, verschachteln, speichern: OK")


def test_bad_folder_commands_change_nothing_and_say_so():
    c, events = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    before = lib(c)
    for cmd in (p.CreateFolder("f-1", "Doppelt"),          # id taken
                p.CreateFolder("bad id!", "X"),            # invalid id
                p.CreateFolder("f-9", "   "),              # empty name
                p.CreateFolder("f-9", "x" * 81),           # name too long
                p.CreateFolder("f-9", "X", "f-weg"),       # unknown parent
                p.CreateFolder("f-9", "X", None, "music"),  # music area is phase F
                p.RenameFolder("f-weg", "X"), p.RenameFolder("f-1", ""),
                p.DeleteFolder("f-weg")):
        events.clear()
        c.send(cmd)
        assert lib(c) == before, cmd
        assert notices(events), f"no notice for {cmd}"
    print("ungueltige Ordner-Befehle aendern nichts und sagen es: OK")


def test_a_folder_cannot_move_into_itself():
    """A4 is about folders; MoveToFolder moves sounds only, so the cycle guard is the
    parent check at creation - a new folder can only hang below an existing one."""
    c, events = make()
    c.send(p.CreateFolder("f-1", "A"))
    events.clear()
    c.send(p.CreateFolder("f-1", "B", "f-1"))
    assert len(lib(c)["folders"]) == 1 and notices(events)
    print("ein Ordner kann nicht in sich selbst: OK")


def test_a_folder_moves_into_another_and_back_to_the_top():
    """Hand check 2026-10-03b: right-click "Verschieben nach…" - a folder hangs below another
    one or back at the top; never below itself or one of its own subfolders (A4)."""
    c, events = make()
    c.send(p.CreateFolder("f-1", "A"))
    c.send(p.CreateFolder("f-2", "B"))
    c.send(p.CreateFolder("f-3", "C", "f-2"))
    c.send(p.MoveFolder("f-2", "f-1"))
    parents = {f["id"]: f["parent_id"] for f in lib(c)["folders"]}
    assert parents == {"f-1": None, "f-2": "f-1", "f-3": "f-2"}, parents
    assert {f["id"]: f["parent_id"] for f in on_disk()["folders"]}["f-2"] == "f-1"
    before = lib(c)
    for cmd in (p.MoveFolder("f-1", "f-1"),     # into itself
                p.MoveFolder("f-1", "f-3"),     # into its own sub-subfolder
                p.MoveFolder("f-weg", None),    # unknown folder
                p.MoveFolder("f-2", "f-weg")):  # unknown target
        events.clear()
        c.send(cmd)
        assert lib(c) == before, cmd
        assert notices(events), f"no notice for {cmd}"
    c.send(p.MoveFolder("f-2", None))
    assert {f["id"]: f["parent_id"] for f in lib(c)["folders"]}["f-2"] is None
    print("Ordner verschieben, nie in sich selbst: OK")


def test_moving_sounds_into_and_out_of_folders():
    c, events = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    c.send(p.MoveToFolder(("s1", "s2"), "f-1"))
    assert sound(c, "s1")["folder_id"] == "f-1" and sound(c, "s2")["folder_id"] == "f-1"
    c.send(p.MoveToFolder(("s1",), None))
    assert sound(c, "s1")["folder_id"] is None
    events.clear()
    c.send(p.MoveToFolder(("s3",), "f-weg"))
    assert sound(c, "s3").get("folder_id") is None and notices(events)
    c.send(p.MoveToFolder(("s3", "weg"), "f-1"))  # unknown sound ids are skipped
    assert sound(c, "s3")["folder_id"] == "f-1"
    assert {s["id"]: s.get("folder_id") for s in on_disk()["sounds"]} == {"s1": None, "s2": "f-1", "s3": "f-1"}
    print("Sounds in Ordner hinein und heraus: OK")


def test_deleting_a_folder_moves_its_content_one_level_up():
    c, _ = make()
    c.send(p.CreateFolder("f-1", "Oben"))
    c.send(p.CreateFolder("f-2", "Mitte", "f-1"))
    c.send(p.CreateFolder("f-3", "Unten", "f-2"))
    c.send(p.MoveToFolder(("s1",), "f-2"))
    c.send(p.MoveToFolder(("s2",), "f-3"))
    c.send(p.DeleteFolder("f-2"))
    folders = {f["id"]: f["parent_id"] for f in lib(c)["folders"]}
    assert folders == {"f-1": None, "f-3": "f-1"}, folders
    assert sound(c, "s1")["folder_id"] == "f-1", "sounds move to the parent, never deleted"
    assert sound(c, "s2")["folder_id"] == "f-3"
    assert len(c.state()["sounds"]) == 3
    print("Ordner loeschen schiebt den Inhalt eine Ebene hoch: OK")


def test_playlists_collect_each_sound_once_and_persist():
    c, events = make()
    c.send(p.CreatePlaylist("p-1", "Lieblinge"))
    c.send(p.AddToPlaylist("p-1", ("s2", "s1", "s2", "weg")))
    c.send(p.AddToPlaylist("p-1", ("s1", "s3")))
    pl = lib(c)["playlists"][0]
    assert pl == {"id": "p-1", "name": "Lieblinge", "area": "sounds", "cover": None, "image": None,
                  "item_ids": ["s2", "s1", "s3"]}, pl
    c.send(p.RemoveFromPlaylist("p-1", ("s1",)))
    c.send(p.RenamePlaylist("p-1", "Beste"))
    assert lib(c)["playlists"][0]["item_ids"] == ["s2", "s3"]
    assert on_disk()["playlists"] == [{"id": "p-1", "name": "Beste", "item_ids": ["s2", "s3"], "cover": None}]
    for cmd in (p.CreatePlaylist("p-1", "Doppelt"), p.CreatePlaylist("p-2", ""),
                p.CreatePlaylist("p-2", "X", "music"), p.AddToPlaylist("p-weg", ("s1",)),
                p.RenamePlaylist("p-weg", "X"), p.DeletePlaylist("p-weg")):
        events.clear()
        c.send(cmd)
        assert notices(events), f"no notice for {cmd}"
    assert len(lib(c)["playlists"]) == 1
    c.send(p.DeletePlaylist("p-1"))
    assert lib(c)["playlists"] == [] and len(c.state()["sounds"]) == 3, "only the list goes"
    print("Playlists sammeln jeden Sound einmal, speichern, loeschen nur die Liste: OK")


def test_deleting_a_sound_removes_it_from_every_playlist():
    c, _ = make()
    c.send(p.CreatePlaylist("p-1", "A"))
    c.send(p.CreatePlaylist("p-2", "B"))
    c.send(p.AddToPlaylist("p-1", ("s1", "s2")))
    c.send(p.AddToPlaylist("p-2", ("s1",)))
    c.send(p.DeleteSound("s1"))
    assert [pl["item_ids"] for pl in lib(c)["playlists"]] == [["s2"], []]
    assert [pl["item_ids"] for pl in on_disk()["playlists"]] == [["s2"], []]
    print("geloeschter Sound verschwindet aus jeder Playlist: OK")


def test_deleting_a_collection_unpins_it():
    c, _ = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    c.send(p.CreatePlaylist("p-1", "Beste"))
    pins = ({"kind": "folder", "id": "f-1", "name": "Memes", "image": None},
            {"kind": "sound-playlist", "id": "p-1", "name": "Beste", "image": None},
            {"kind": "spotify-liked", "id": "liked", "name": "Lieblingssongs", "image": None})
    c.send(p.SetSidebarPins(pins))
    c.send(p.DeleteFolder("f-1"))
    c.send(p.DeletePlaylist("p-1"))
    assert [pin["id"] for pin in c.state()["settings"]["sidebar_pins"]] == ["liked"]
    assert [pin["id"] for pin in on_disk()["sidebar_pins"]] == ["liked"]
    print("geloeschte Ordner/Playlists verschwinden aus der Seitenleiste: OK")


def test_tags_are_normalized_and_favorites_toggle():
    c, events = make()
    c.send(p.SetTags("s1", (" Lustig ", "lustig", "kurz", "", "x" * 40)))
    assert sound(c, "s1")["tags"] == ["Lustig", "kurz", "x" * 32], sound(c, "s1")["tags"]
    c.send(p.SetTags("s1", tuple(f"t{i}" for i in range(30))))
    assert len(sound(c, "s1")["tags"]) == config.MAX_TAGS
    c.send(p.SetFavorite("s2", True))
    assert sound(c, "s2")["favorite"] is True
    c.send(p.SetFavorite("s2", False))
    assert sound(c, "s2")["favorite"] is False
    assert {s["id"]: s.get("favorite") for s in on_disk()["sounds"]}["s2"] is False
    for cmd in (p.SetTags("weg", ("a",)), p.SetFavorite("weg", True)):
        events.clear()
        c.send(cmd)
        assert notices(events), cmd
    print("Stichwoerter normalisiert, Favorit umschaltbar: OK")


def test_library_changes_reach_the_library_group():
    """A page must learn about a new folder from the library group, like a new sound."""
    c, events = make()
    events.clear()
    c.send(p.CreateFolder("f-1", "Memes"))
    parts = [e for e in core_fakes.of_type(events, p.PartChanged) if e.group == "library"]
    assert parts and "library" in parts[-1].part, parts
    print("Aenderungen erreichen die Gruppe library: OK")


def test_what_the_page_sends_works_end_to_end_and_survives_a_restart():
    """T9 without devices: the exact JSON the web page builds (makeCommand in
    dropCommand.ts, SoundDialogs.tsx, NewCollectionDialog.tsx, CollectionActions.tsx)
    goes through protocol.from_json like the server does; a second core started from
    config.json shows the same library."""
    c, events = make()
    v = p.PROTOCOL_VERSION
    page = [
        {"type": "CreateFolder", "data": {"folder_id": "f-mo1", "name": " Memes ", "parent_id": None, "area": "sounds"}},
        {"type": "CreateFolder", "data": {"folder_id": "f-mo2", "name": "CS", "parent_id": "f-mo1", "area": "sounds"}},
        {"type": "CreatePlaylist", "data": {"playlist_id": "p-mo3", "name": "Beste", "area": "sounds"}},
        {"type": "MoveToFolder", "data": {"sound_ids": ["s1", "s2"], "folder_id": "f-mo2"}},
        {"type": "AddToPlaylist", "data": {"playlist_id": "p-mo3", "sound_ids": ["s3", "s1"]}},
        {"type": "SetTags", "data": {"sound_id": "s1", "tags": ["laut", "Laut", "kurz"]}},
        {"type": "SetFavorite", "data": {"sound_id": "s3", "favorite": True}},
        {"type": "RenamePlaylist", "data": {"playlist_id": "p-mo3", "name": "Allerbeste"}},
        {"type": "SetSidebarPins", "data": {"pins": [{"kind": "folder", "id": "f-mo1", "name": "Memes", "image": None}]}},
    ]
    for msg in page:
        c.send(p.from_json({**msg, "kind": "command", "v": v}))
    assert not notices(events), notices(events)
    before = {k: c.state()[k] for k in ("library", "sounds")}

    restarted = core_fakes.make_core(store_data=config.load_config())[0]
    after = {k: restarted.state()[k] for k in ("library", "sounds")}
    assert after == before, (after, before)
    s1 = next(s for s in after["sounds"] if s["id"] == "s1")
    assert (s1["folder_id"], s1["tags"]) == ("f-mo2", ["laut", "kurz"])
    assert after["library"]["playlists"][0]["item_ids"] == ["s3", "s1"]
    assert restarted.state()["settings"]["sidebar_pins"][0]["id"] == "f-mo1"
    print("was die Seite schickt, wirkt Ende-zu-Ende und ueberlebt einen Neustart: OK")


def test_new_sounds_carry_the_library_fields_at_once():
    """Added or imported sounds have the same shape as loaded ones - not only after
    the next restart."""
    c, _ = make()
    c.send(p.AddSound(str(core_fakes.FIXTURES / "test_tone.mp3"), "Neu"))
    added = c.state()["sounds"][-1]
    assert (added["folder_id"], added["tags"], added["favorite"]) == (None, [], False), added
    print("neue Sounds tragen die Bibliotheksfelder sofort: OK")


class PickCover:
    def __init__(self, path):
        self.path = path
        self.asked = 0

    def pick_icon_file(self):
        self.asked += 1
        return self.path


def cover_url(c, kind, cid):
    items = lib(c)["folders" if kind == "folder" else "playlists"]
    return next(i for i in items if i["id"] == cid)["image"]


def test_collection_covers_are_picked_shown_and_cleared():
    """Schritt C (E3): eigenes Cover fuer Ordner und Playlist, eckig, ueber den Kern-Dialog."""
    c, events = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    c.send(p.CreatePlaylist("pl-1", "Abend"))
    assert cover_url(c, "folder", "f-1") is None and cover_url(c, "playlist", "pl-1") is None
    c.collections._dialogs = PickCover(str(core_fakes.FIXTURES / "test_image.png"))
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    c.send(p.RequestSetCollectionCover("playlist", "pl-1"))
    url = cover_url(c, "folder", "f-1")
    assert url and url.startswith("/cover?id=f-1&rev="), url
    assert cover_url(c, "playlist", "pl-1").startswith("/cover?id=pl-1&rev=")
    stored = Path(_TMP) / "covers" / "f-1.png"
    assert stored.exists() and on_disk()["folders"][0]["cover"] == "covers/f-1.png"
    from PIL import Image
    with Image.open(stored) as img:
        assert img.size[0] == img.size[1] and img.mode == "RGB", (img.size, img.mode)
    # a second pick bumps the revision so the page reloads the image
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    assert cover_url(c, "folder", "f-1") != url
    # the page serves it through the server route
    status, ctype, body = c.collections.serve_cover(None, {"id": ["f-1"]}, "")
    assert status == 200 and ctype == "image/png" and body == stored.read_bytes()
    assert c.collections.serve_cover(None, {"id": ["../config"]}, "")[0] == 404
    assert c.collections.serve_cover(None, {"id": ["nope"]}, "")[0] == 404
    c.send(p.ClearCollectionCover("folder", "f-1"))
    assert cover_url(c, "folder", "f-1") is None and not stored.exists()
    assert on_disk()["folders"][0]["cover"] is None
    # deleting a playlist deletes its cover file too
    c.send(p.DeletePlaylist("pl-1"))
    assert not (Path(_TMP) / "covers" / "pl-1.png").exists()
    print("Cover fuer Ordner/Playlist waehlen, anzeigen, entfernen: OK")


def test_a_sound_gets_its_own_cover_like_a_folder():
    """Hand check 2026-10-03b: a sound's own square cover, picked via the core dialog, shown
    as `image` on the sound, served by /cover, cleared, and gone with the sound."""
    c, events = make()
    assert sound(c, "s1").get("image") is None
    c.collections._dialogs = PickCover(str(core_fakes.FIXTURES / "test_image.png"))
    c.send(p.RequestSetCollectionCover("sound", "s1"))
    url = sound(c, "s1")["image"]
    assert url and url.startswith("/cover?id=s1&rev="), url
    stored = Path(_TMP) / "covers" / "s1.png"
    assert stored.exists()
    assert next(x for x in on_disk()["sounds"] if x["id"] == "s1")["cover"] == "covers/s1.png"
    assert c.collections.serve_cover(None, {"id": ["s1"]}, "")[0] == 200
    c.send(p.ClearCollectionCover("sound", "s1"))
    assert sound(c, "s1").get("image") is None and not stored.exists()
    c.send(p.RequestSetCollectionCover("sound", "s2"))
    assert (Path(_TMP) / "covers" / "s2.png").exists()
    c.send(p.DeleteSound("s2"))
    assert not (Path(_TMP) / "covers" / "s2.png").exists()
    print("eigenes Cover fuer Sounds waehlen, anzeigen, entfernen: OK")


def test_bad_cover_requests_change_nothing():
    c, events = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    dialogs = PickCover(str(core_fakes.FIXTURES / "test_image.png"))
    c.collections._dialogs = dialogs
    c.send(p.RequestSetCollectionCover("folder", "missing"))
    c.send(p.RequestSetCollectionCover("song", "f-1"))
    assert dialogs.asked == 0, "no dialog for a collection that does not exist"
    bad = Path(_TMP) / "kein-bild.png"
    bad.write_text("x", encoding="utf-8")
    c.send(p.SetCollectionCover("folder", "f-1", str(bad)))
    assert cover_url(c, "folder", "f-1") is None
    assert any("Bild" in n for n in notices(events)), notices(events)
    dialogs.path = None  # cancelled
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    assert cover_url(c, "folder", "f-1") is None
    print("ungueltige Cover-Anfragen aendern nichts: OK")


def test_config_keeps_only_a_valid_cover_path():
    data = config._default_config()
    data["folders"] = [{"id": "a", "name": "A", "parent_id": None, "cover": "covers/a.png"},
                       {"id": "b", "name": "B", "parent_id": None, "cover": "../../evil.png"},
                       {"id": "c", "name": "C", "parent_id": None}]
    data["playlists"] = [{"id": "p", "name": "P", "item_ids": [], "cover": "covers/x.png"}]
    out = config._with_defaults(data)
    assert [f["cover"] for f in out["folders"]] == ["covers/a.png", None, None]
    assert out["playlists"][0]["cover"] is None, "a cover belongs to its own id"
    print("config behaelt nur gueltige Cover-Pfade: OK")


def test_a_folder_loop_from_a_hand_edited_file_is_broken_on_load():
    """Nachtlauf B1: a -> b -> c -> a survived loading, and every walk up the parents
    (move check, breadcrumb on the page) then circles. One folder of the loop goes to
    the top; the rest keeps its order."""
    data = config._default_config()
    data["folders"] = [{"id": "a", "name": "A", "parent_id": "c"},
                       {"id": "b", "name": "B", "parent_id": "a"},
                       {"id": "c", "name": "C", "parent_id": "b"},
                       {"id": "d", "name": "D", "parent_id": "b"}]
    out = config._with_defaults(data)
    parents = {f["id"]: f["parent_id"] for f in out["folders"]}
    assert parents == {"a": None, "b": "a", "c": "b", "d": "b"}, parents
    print("eine Ordner-Schleife aus einer Hand-Datei wird beim Laden aufgeloest: OK")


def test_the_cover_dialog_opens_only_once():
    """Handpruefung W8: weitere Klicks waehrend der Dialog offen ist oeffnen ihn nicht erneut;
    der Dialog laeuft auf einem Worker, der Kern bleibt bedienbar."""
    c, _ = make()
    c.send(p.CreateFolder("f-1", "Memes"))
    dialogs = PickCover(str(core_fakes.FIXTURES / "test_image.png"))
    c.collections._dialogs = dialogs
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    c.send(p.RequestSetCollectionCover("folder", "f-1"))
    assert dialogs.asked == 0, "the dialog does not run on the core thread"
    workers.run_all()
    assert dialogs.asked == 1, dialogs.asked
    assert cover_url(c, "folder", "f-1"), "the one pick still sets the cover"
    c.send(p.RequestSetCollectionCover("folder", "f-1"))  # closed again: a new click opens it
    workers.run_all()
    assert dialogs.asked == 2
    print("Cover-Dialog oeffnet nur einmal: OK")


def main():
    test_the_library_part_has_the_shape_the_page_reads()
    test_folders_create_rename_nest_and_persist()
    test_bad_folder_commands_change_nothing_and_say_so()
    test_a_folder_cannot_move_into_itself()
    test_a_folder_moves_into_another_and_back_to_the_top()
    test_moving_sounds_into_and_out_of_folders()
    test_deleting_a_folder_moves_its_content_one_level_up()
    test_playlists_collect_each_sound_once_and_persist()
    test_deleting_a_sound_removes_it_from_every_playlist()
    test_deleting_a_collection_unpins_it()
    test_tags_are_normalized_and_favorites_toggle()
    test_library_changes_reach_the_library_group()
    test_what_the_page_sends_works_end_to_end_and_survives_a_restart()
    test_new_sounds_carry_the_library_fields_at_once()
    test_collection_covers_are_picked_shown_and_cleared()
    test_a_sound_gets_its_own_cover_like_a_folder()
    test_bad_cover_requests_change_nothing()
    test_config_keeps_only_a_valid_cover_path()
    test_a_folder_loop_from_a_hand_edited_file_is_broken_on_load()
    test_the_cover_dialog_opens_only_once()
    print("\nALL COLLECTIONS CHECKS PASSED")


if __name__ == "__main__":
    main()
