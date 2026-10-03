"""Bibliothek 2.0 (spec 2026-10-01-bibliothek-2): folders, sound playlists, tags and
favorites of the sounds area. Everything lives in config.json behind the store (J1);
a playlist is only a collection (J2). Core thread only.

The state part `library` has the shape the web page already reads (types.ts
`Library`); tracks/music stay empty until the music library (phase F) exists.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config, icons
from .filedialogs import FileDialogs
from .protocol import (
    AddToPlaylist, ClearCollectionCover, CreateFolder, CreatePlaylist, DeleteFolder, DeletePlaylist,
    MoveFolder, MoveToFolder, RemoveFromPlaylist, RenameFolder, RenamePlaylist, RequestSetCollectionCover,
    SetCollectionCover, SetFavorite, SetTags,
)

log = logging.getLogger(__name__)

SOUNDS_AREA = "sounds"
PIN_KINDS = {"folder": "folder", "playlist": "sound-playlist"}

NOT_FOUND = "Diesen Ordner oder diese Playlist gibt es nicht mehr."
SOUND_NOT_FOUND = "Diesen Sound gibt es nicht mehr."
INTO_ITSELF = "Ein Ordner kann nicht in sich selbst oder einen seiner Unterordner."
BAD_NAME = f"Bitte einen Namen mit 1 bis {config.MAX_COLLECTION_NAME} Zeichen eingeben."
BAD_ID = "Die Oberfläche hat eine ungültige Kennung geschickt."
MUSIC_LATER = "Ordner und Playlists für eigene Musik kommen später."
COVER_UNREADABLE = "Dieses Bild lässt sich nicht als Cover lesen."
COVER_ROUTE = "/cover"  # served by the local server (webmain attaches it)

IDLE_MUSIC = {"track_id": None, "playing": False, "position": 0, "since": 0, "volume": 1}


def forget_sound(data: dict, sound_id: str) -> None:
    """A deleted sound leaves every playlist (A9). Called by LibraryService.delete."""
    for pl in data.get("playlists") or []:
        if sound_id in pl["item_ids"]:
            pl["item_ids"] = [i for i in pl["item_ids"] if i != sound_id]


class CollectionsService:
    def __init__(self, core, dialogs=None):
        self._core = core
        self._dialogs = dialogs if dialogs is not None else FileDialogs()
        self._cover_rev: dict[str, int] = {}  # collection id -> revision, not persisted
        self._picking = False  # the cover dialog is open
        for cls, fn in ((CreateFolder, self.create_folder), (RenameFolder, self.rename_folder),
                        (DeleteFolder, self.delete_folder), (MoveFolder, self.move_folder),
                        (MoveToFolder, self.move_to_folder),
                        (CreatePlaylist, self.create_playlist), (RenamePlaylist, self.rename_playlist),
                        (DeletePlaylist, self.delete_playlist), (AddToPlaylist, self.add_to_playlist),
                        (RemoveFromPlaylist, self.remove_from_playlist), (SetTags, self.set_tags),
                        (SetFavorite, self.set_favorite),
                        (RequestSetCollectionCover, self._request_set_cover),
                        (SetCollectionCover, self.set_cover), (ClearCollectionCover, self.clear_cover)):
            core.handle(cls, fn)
        core.add_state("library", self.snapshot, group="library")

    # ---- state (pure read) ----

    def snapshot(self) -> dict:
        return {
            "folders": [{"id": f["id"], "name": f["name"], "parent_id": f["parent_id"],
                         "area": SOUNDS_AREA, "image": self._image(f)} for f in self._folders],
            "playlists": [{"id": pl["id"], "name": pl["name"], "area": SOUNDS_AREA, "cover": None,
                           "image": self._image(pl), "item_ids": list(pl["item_ids"])}
                          for pl in self._playlists],
            "tracks": [],
            "recent_ids": [],
            "music": dict(IDLE_MUSIC),
        }

    def image_url(self, item: dict) -> str | None:
        """For the sounds state part (library.py): a sound's own cover URL, a pure read."""
        return self._image(item)

    def drop_cover(self, item_id: str) -> None:
        """A deleted sound takes its cover file along."""
        self._drop_cover_file(item_id)

    def _image(self, item: dict) -> str | None:
        """URL of the own cover; the revision makes the page reload a changed image."""
        if not item.get("cover"):
            return None
        return f"{COVER_ROUTE}?id={item['id']}&rev={self._cover_rev.get(item['id'], 0)}"

    # ---- helpers ----

    @property
    def _data(self) -> dict:
        return self._core.store.data

    @property
    def _folders(self) -> list[dict]:
        return self._data.setdefault("folders", [])

    @property
    def _playlists(self) -> list[dict]:
        return self._data.setdefault("playlists", [])

    def _folder(self, folder_id) -> dict | None:
        return next((f for f in self._folders if f["id"] == folder_id), None)

    def _playlist(self, playlist_id) -> dict | None:
        return next((pl for pl in self._playlists if pl["id"] == playlist_id), None)

    def _sound(self, sound_id) -> dict | None:
        return next((s for s in self._data.get("sounds") or [] if s["id"] == sound_id), None)

    def _refuse(self, text: str) -> None:
        self._core.notice(text, "hint")

    def _new_id_ok(self, new_id: str, area: str) -> bool:
        if area != SOUNDS_AREA:
            self._refuse(MUSIC_LATER)
            return False
        taken = {f["id"] for f in self._folders} | {pl["id"] for pl in self._playlists}
        if not isinstance(new_id, str) or not config.COLLECTION_ID_RE.fullmatch(new_id) or new_id in taken:
            self._refuse(BAD_ID)
            return False
        return True

    def _name(self, name) -> str | None:
        clean = config.clean_collection_name(name)
        if clean is None:
            self._refuse(BAD_NAME)
        return clean

    def _save(self) -> None:
        self._core.store.save_now()
        self._core.state_changed()
        self._core.changed("library")

    def _unpin(self, kind: str, item_id: str) -> None:
        pins = self._data.get("sidebar_pins") or []
        kept = [pin for pin in pins if not (pin.get("kind") == PIN_KINDS[kind] and pin.get("id") == item_id)]
        if len(kept) != len(pins):
            self._data["sidebar_pins"] = kept

    # ---- folders ----

    def create_folder(self, cmd: CreateFolder) -> None:
        if not self._new_id_ok(cmd.folder_id, cmd.area):
            return
        name = self._name(cmd.name)
        if name is None:
            return
        if cmd.parent_id is not None and self._folder(cmd.parent_id) is None:
            self._refuse(NOT_FOUND)
            return
        self._folders.append({"id": cmd.folder_id, "name": name, "parent_id": cmd.parent_id, "cover": None})
        self._save()

    def rename_folder(self, cmd: RenameFolder) -> None:
        folder = self._folder(cmd.folder_id)
        if folder is None:
            self._refuse(NOT_FOUND)
            return
        name = self._name(cmd.name)
        if name is None:
            return
        folder["name"] = name
        self._save()

    def delete_folder(self, cmd: DeleteFolder) -> None:
        """A5: sounds and subfolders move one level up; nothing else is deleted."""
        folder = self._folder(cmd.folder_id)
        if folder is None:
            self._refuse(NOT_FOUND)
            return
        parent = folder["parent_id"]
        for f in self._folders:
            if f["parent_id"] == folder["id"]:
                f["parent_id"] = parent
        for s in self._data.get("sounds") or []:
            if s.get("folder_id") == folder["id"]:
                s["folder_id"] = parent
        self._data["folders"] = [f for f in self._folders if f["id"] != folder["id"]]
        log.info("folder %s %r deleted", folder["id"], folder["name"])
        self._drop_cover_file(folder["id"])
        self._unpin("folder", folder["id"])
        self._save()

    def move_folder(self, cmd: MoveFolder) -> None:
        """Hand check 2026-10-03b: a folder hangs below another one or at the top (A4: never
        below itself or one of its own subfolders - walk up from the target)."""
        folder = self._folder(cmd.folder_id)
        if folder is None or (cmd.parent_id is not None and self._folder(cmd.parent_id) is None):
            self._refuse(NOT_FOUND)
            return
        cur, seen = cmd.parent_id, set()
        while cur is not None and cur not in seen:
            if cur == folder["id"]:
                self._refuse(INTO_ITSELF)
                return
            seen.add(cur)
            cur = (self._folder(cur) or {}).get("parent_id")
        folder["parent_id"] = cmd.parent_id
        self._save()

    def move_to_folder(self, cmd: MoveToFolder) -> None:
        if cmd.folder_id is not None and self._folder(cmd.folder_id) is None:
            self._refuse(NOT_FOUND)
            return
        moved = False
        for sid in cmd.sound_ids:
            s = self._sound(sid)
            if s is not None:
                s["folder_id"] = cmd.folder_id
                moved = True
        if moved:
            self._save()

    # ---- playlists ----

    def create_playlist(self, cmd: CreatePlaylist) -> None:
        if not self._new_id_ok(cmd.playlist_id, cmd.area):
            return
        name = self._name(cmd.name)
        if name is None:
            return
        self._playlists.append({"id": cmd.playlist_id, "name": name, "item_ids": [], "cover": None})
        self._save()

    def rename_playlist(self, cmd: RenamePlaylist) -> None:
        pl = self._playlist(cmd.playlist_id)
        if pl is None:
            self._refuse(NOT_FOUND)
            return
        name = self._name(cmd.name)
        if name is None:
            return
        pl["name"] = name
        self._save()

    def delete_playlist(self, cmd: DeletePlaylist) -> None:
        playlist = self._playlist(cmd.playlist_id)
        if playlist is None:
            self._refuse(NOT_FOUND)
            return
        self._data["playlists"] = [pl for pl in self._playlists if pl["id"] != cmd.playlist_id]
        # Fund 12 (a deleted playlist came back once, not reproduced): the log names what
        # went, so a folder and a playlist of the same name can be told apart next time
        log.info("playlist %s %r deleted", cmd.playlist_id, playlist["name"])
        self._drop_cover_file(cmd.playlist_id)
        self._unpin("playlist", cmd.playlist_id)
        self._save()

    def add_to_playlist(self, cmd: AddToPlaylist) -> None:
        pl = self._playlist(cmd.playlist_id)
        if pl is None:
            self._refuse(NOT_FOUND)
            return
        added = False
        for sid in cmd.sound_ids:
            if sid not in pl["item_ids"] and self._sound(sid) is not None:
                pl["item_ids"].append(sid)
                added = True
        if added:
            self._save()

    def remove_from_playlist(self, cmd: RemoveFromPlaylist) -> None:
        pl = self._playlist(cmd.playlist_id)
        if pl is None:
            self._refuse(NOT_FOUND)
            return
        kept = [i for i in pl["item_ids"] if i not in cmd.sound_ids]
        if len(kept) != len(pl["item_ids"]):
            pl["item_ids"] = kept
            self._save()

    # ---- covers (Schritt C, E3) ----

    def _collection(self, kind: str, collection_id) -> dict | None:
        if kind == "folder":
            return self._folder(collection_id)
        if kind == "playlist":
            return self._playlist(collection_id)
        if kind == "sound":  # hand check 2026-10-03b: sounds get their own cover the same way
            return self._sound(collection_id)
        return None

    def _cover_file(self, collection_id: str) -> Path:
        return self._core.store.data_dir / config.cover_path(collection_id)

    def _drop_cover_file(self, collection_id: str) -> None:
        self._cover_rev.pop(collection_id, None)
        try:
            self._cover_file(collection_id).unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete the cover of %s", collection_id, exc_info=True)

    def _request_set_cover(self, cmd: RequestSetCollectionCover) -> None:
        """W8: the dialog runs on a worker (the core stays usable), and clicks while it is
        open do not queue up a second dialog."""
        if self._collection(cmd.collection_kind, cmd.collection_id) is None:
            self._refuse(NOT_FOUND)
            return
        if self._picking:
            return
        self._picking = True
        self._core.workers.submit(self._pick_cover, cmd.collection_kind, cmd.collection_id)

    def _pick_cover(self, kind: str, collection_id: str) -> None:  # worker
        try:
            path = self._dialogs.pick_icon_file()
        except Exception:
            log.exception("the cover dialog failed")
            path = None
        self._core.executor.submit(self._cover_picked, kind, collection_id, path)

    def _cover_picked(self, kind: str, collection_id: str, path) -> None:
        self._picking = False
        if path:
            self.set_cover(SetCollectionCover(kind, collection_id, path))

    def set_cover(self, cmd: SetCollectionCover) -> None:
        if self._collection(cmd.collection_kind, cmd.collection_id) is None:
            self._refuse(NOT_FOUND)
            return
        self._core.workers.submit(self._save_cover, cmd.collection_kind, cmd.collection_id, Path(cmd.image_path))

    def _save_cover(self, kind: str, collection_id: str, source: Path) -> None:  # worker
        error = None
        try:
            icons.save_cover(source, self._cover_file(collection_id))
        except icons.IconError:
            error = COVER_UNREADABLE
        except Exception:
            log.exception("saving cover %s failed", source)
            error = COVER_UNREADABLE
        self._core.executor.submit(self._cover_saved, kind, collection_id, error)

    def _cover_saved(self, kind: str, collection_id: str, error: str | None) -> None:
        if error is not None:
            self._core.notice(error, "error")
            return
        item = self._collection(kind, collection_id)
        if item is None:
            self._drop_cover_file(collection_id)  # deleted while the worker ran
            return
        item["cover"] = config.cover_path(collection_id)
        self._cover_rev[collection_id] = self._cover_rev.get(collection_id, 0) + 1
        self._save()

    def clear_cover(self, cmd: ClearCollectionCover) -> None:
        item = self._collection(cmd.collection_kind, cmd.collection_id)
        if item is None:
            self._refuse(NOT_FOUND)
            return
        if item.get("cover"):
            item["cover"] = None
            self._drop_cover_file(cmd.collection_id)
            self._save()

    def attach_server(self, host) -> None:
        """webmain hands the local server over; the covers are one of its routes."""
        host.add_route(COVER_ROUTE, self.serve_cover)

    def serve_cover(self, handler, query, body):
        """`webserver` route signature, runs on the server's request thread: reads one
        file by a checked id, never a path from the request."""
        cid = (query.get("id") or [""])[0]
        if not config.COLLECTION_ID_RE.fullmatch(cid):
            return 404, "text/plain; charset=utf-8", b"not found"
        try:
            data = self._cover_file(cid).read_bytes()
        except OSError:
            return 404, "text/plain; charset=utf-8", b"not found"
        return 200, "image/png", data

    # ---- per sound ----

    def set_tags(self, cmd: SetTags) -> None:
        s = self._sound(cmd.sound_id)
        if s is None:
            self._refuse(SOUND_NOT_FOUND)
            return
        s["tags"] = config.normalize_tags(cmd.tags)
        self._save()

    def set_favorite(self, cmd: SetFavorite) -> None:
        s = self._sound(cmd.sound_id)
        if s is None:
            self._refuse(SOUND_NOT_FOUND)
            return
        s["favorite"] = bool(cmd.favorite)
        self._save()
