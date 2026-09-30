"""Attrappen fuer die Kern-Tests: keine Audio-Hardware, kein Fenster, keine Hooks.

Importieren erst, nachdem das Testskript RUCKUS_DATA_DIR gesetzt hat."""

import copy
import logging
import tempfile
import threading
from pathlib import Path

from soundboard import config, core
from soundboard.executors import InlineExecutor

FIXTURES = Path(__file__).parent / "fixtures"

# Error paths are exercised on purpose and log; without a handler Python would print
# their tracebacks to stderr ("last resort") and the test output would not be clean.
logging.getLogger("soundboard").addHandler(logging.NullHandler())


class FakeEngine:
    """Duck-typed AudioEngine. `threads` records which thread ran each device call."""

    def __init__(self):
        self.loaded: set[str] = set()
        self.playing: set[str] = set()
        self.plays: list[tuple[str, float]] = []
        self.monitor_only: list[str] = []  # previews (VolumeDialog)
        self.preloads: list[str] = []
        self.forgotten: list[str] = []
        self.stop_calls: list[str] = []
        self.stopped = 0
        self.fail_play = False
        self.threads: list[tuple[str, str]] = []
        self.sink = None
        self.voicemeeter_device = None
        self.monitor_device = None
        self.monitor_volume = 0.5
        self.music_plays: list[str] = []  # Klangbild: plays marked as music
        self.trims: dict[str, object] = {}  # sound id -> trim of its last preload
        self.clips: list[tuple[str, int, float]] = []  # trim previews: (id, frames, gain)
        self.no_target = False  # K6: no headphone, no mixer - play_clip must start nothing

    def _record(self, call: str) -> None:
        self.threads.append((call, threading.current_thread().name))

    def preload(self, sound_id, path, trim=None):
        self.preloads.append(sound_id)
        self.trims[sound_id] = trim
        if not Path(path).exists():
            raise FileNotFoundError(path)
        self.loaded.add(sound_id)

    def play_clip(self, sound_id, samples, volume=1.0):
        self._record("play_clip")
        if self.fail_play:
            self.fail_play = False
            raise RuntimeError("fake device error")
        if self.no_target:
            return None  # K6: no headphone, no mixer - nothing starts
        self.clips.append((sound_id, len(samples), volume))
        self.playing.add(sound_id)
        return object()  # any non-None sentinel; callers only check for None

    def is_loaded(self, sound_id):
        return sound_id in self.loaded

    def forget(self, sound_id):
        self.forgotten.append(sound_id)
        self.loaded.discard(sound_id)

    def play(self, sound_id, volume=1.0, monitor_only=False, music=False):
        self._record("play")
        if sound_id not in self.loaded:
            raise KeyError(sound_id)
        if self.fail_play:
            self.fail_play = False
            raise RuntimeError("fake device error")
        if self.no_target:
            return None  # K6: no headphone, no mixer/VoiceMeeter - nothing starts
        self.plays.append((sound_id, volume))
        if monitor_only:
            self.monitor_only.append(sound_id)
        if music:
            self.music_plays.append(sound_id)
        self.playing.add(sound_id)
        return object()  # any non-None sentinel; callers only check for None

    def playing_ids(self):
        self._record("playing_ids")
        return set(self.playing)

    def stop_all(self):
        self._record("stop_all")
        self.stopped += 1
        self.playing.clear()

    def stop(self, sound_id):
        self._record("stop")
        self.stop_calls.append(sound_id)
        self.playing.discard(sound_id)


class FakeSink:
    def __init__(self, mic_active=True):
        self.mic_active = mic_active
        self.mic_muted = False
        self.applied: list[tuple[str, dict]] = []
        self.levels: list[tuple[float, bool, float]] = []
        self.klangbild: list[tuple] = []  # (music_offset_db, musicbus_db) per apply_levels
        self.music: list = []  # blocks the music bus pushed through the hook
        self.music_enabled: list[bool] = []  # set_music_enabled() calls, in order
        self.stopped = False

    def set_mic_muted(self, muted):
        self.mic_muted = bool(muted)

    def set_music_enabled(self, on):
        self.music_enabled.append(bool(on))

    def apply(self, key, settings):
        self.applied.append((key, dict(settings)))

    def apply_levels(self, offset_db, ducking_enabled, ducking_db,
                     music_offset_db=None, musicbus_db=0.0):
        self.levels.append((offset_db, ducking_enabled, ducking_db))
        self.klangbild.append((music_offset_db, musicbus_db))

    def distribute_music(self, block):
        self.music.append(block)

    def stop(self):
        self.stopped = True


CABLE_KEY = "CABLE Output (VB-Audio Virtual Cable)"

RESOLVED = {
    "voicemeeter": 29,
    "monitor": 10,
    "monitor_name": "Kopfhörer (KT USB Audio)",
    "connected": True,
    "virtual_mic": {"mode": "cable", "label": "VB-CABLE", "connected": True,
                    "out_index": 29, "in_index": 39,
                    "out_name": "CABLE Input (VB-Audio Virtual Cable)",
                    "discord_device_name": CABLE_KEY},
    "virtual_mics": [{"key": CABLE_KEY, "label": "CABLE", "out_index": 29, "in_index": 39,
                      "out_name": "CABLE Input (VB-Audio Virtual Cable)", "in_name": CABLE_KEY}],
    "mic": 34,
    "mic_name": "Mikrofon (Endorfy Solum Voice S Mic)",
    "microphones": ["Mikrofon (Endorfy Solum Voice S Mic)", "Mikrofon (NVIDIA Broadcast)"],
}


class FakeBackend:
    def __init__(self):
        self.resolved = copy.deepcopy(RESOLVED)
        self.sinks: list[FakeSink] = []
        self.resolves = 0
        self.rescans: list[bool] = []
        self.checks: list[tuple] = []
        self.verify_ok = True
        self.default_id = "{A}"
        self.mic_choice_list = ["Mikrofon (Endorfy Solum Voice S Mic)", "Mikrofon (eMeet Nova)"]
        self.default_mic = "Mikrofon (Endorfy Solum Voice S Mic)"
        self.threads: list[tuple[str, str]] = []

    def _record(self, call):
        self.threads.append((call, threading.current_thread().name))

    def resolve(self, cfg):
        self._record("resolve")
        self.resolves += 1
        return copy.deepcopy(self.resolved)

    def rescan(self, cfg, reinit):
        self._record("rescan")
        self.rescans.append(reinit)
        return copy.deepcopy(self.resolved)

    def build_sink(self, cfg, resolved):
        self._record("build_sink")
        sink = FakeSink()
        self.sinks.append(sink)
        return sink

    def verify_path(self, out_index, in_index):
        self._record("verify_path")
        self.checks.append((out_index, in_index))
        return {"ok": self.verify_ok, "rms": 0.1, "reason": "Signal kommt an."}

    def mic_choices(self):
        self._record("mic_choices")
        return list(self.mic_choice_list), self.default_mic

    def default_render_id(self):
        self._record("default_render_id")
        return self.default_id


class FakeHotkeys:
    def __init__(self):
        self.registered: dict = {}

    def register(self, hotkey, callback):
        if hotkey == "bad":
            raise ValueError("unparsable hotkey")
        self.registered[hotkey] = callback

    def unregister(self, hotkey):
        self.registered.pop(hotkey, None)

    def unregister_all(self):
        self.registered.clear()

    def validate(self, hotkey):
        # stands in for keyboard.parse_hotkey: "bad" is a key name it cannot map
        if "bad" in hotkey.split("+"):
            raise ValueError("unparsable hotkey")

    def rebind(self, old_hotkey, new_hotkey, callback):
        if old_hotkey:
            self.registered.pop(old_hotkey, None)
        self.register(new_hotkey, callback)


class FakeAutostart:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list[bool] = []

    def apply(self, enabled):
        self.calls.append(enabled)
        return self.ok


_UPDATE_DIR = tempfile.mkdtemp(prefix="ruckus-upd-")

RELEASE_BASE = "https://example.invalid/"


def fake_release(version="1.2.0", notes="Neu: Updates.", installer=b"setup-bytes",
                 assets=("RuckusRadioSetup.exe", "RuckusRadioSetup.exe.sha256")):
    """A GitHub 'latest release' answer plus the files behind its asset URLs."""
    import hashlib
    release = {"tag_name": f"v{version}", "body": notes,
               "assets": [{"name": name, "browser_download_url": RELEASE_BASE + name}
                          for name in assets]}
    files = {RELEASE_BASE + "RuckusRadioSetup.exe": installer,
             RELEASE_BASE + "RuckusRadioSetup.exe.sha256":
                 f"{hashlib.sha256(installer).hexdigest()}  RuckusRadioSetup.exe\n".encode()}
    return release, files


class FakeReleaseSource:
    """Stands in for GitHub: no network in tests. `fail` raises on latest()."""

    def __init__(self, release=None, files=None, fail=None):
        if release is None:
            release, default_files = fake_release(version="1.0.0")  # older: "current"
            files = default_files if files is None else files
        self.release = release
        self.files = dict(files or {})
        self.fail = fail
        self.latest_calls = 0
        self.downloads: list[str] = []

    def latest(self):
        self.latest_calls += 1
        if self.fail is not None:
            raise self.fail
        return self.release

    def download(self, url, dest, on_progress, should_stop):
        from soundboard import updates
        self.downloads.append(url)
        data = self.files.get(url)
        if data is None:
            raise updates.NetworkError(f"404 {url}")
        half = max(1, len(data) // 2)
        with open(dest, "wb") as out:
            for start in range(0, len(data), half):
                if should_stop():
                    raise updates.UpdateCancelled()
                out.write(data[start:start + half])
                on_progress(min(start + half, len(data)), len(data))


class FakeSpotifyApi:
    """Stands in for SpotifyApi: no network. `fail` raises on any call."""

    def __init__(self, user=None, search=None, search_albums=None, search_playlists=None,
                 tracks=None, playlists=None, albums=None, fail=None,
                 has_more=False, playlist_items=None, player=None, devices=None,
                 send_fail=None, on_send=None,
                 tracks_has_more=False, playlists_has_more=False, albums_has_more=False,
                 tracks_page2=None, playlists_page2=None, albums_page2=None,
                 playlist_meta=None, recent=None,
                 library_contains=None, forbid_playlist=frozenset(), recent_contexts=None):
        self.user = user if user is not None else {
            "display_name": "Jan", "images": [{"url": "https://i.scdn.co/u"}]}
        self.search_items = list(search or [])
        # The "Alle" chip's album/playlist groups (spec §14.2) - separate from the library's
        # own `playlists` below, so a test fixture for one cannot leak into the other.
        self.search_albums = list(search_albums or [])
        self.search_playlists = list(search_playlists or [])
        self.tracks = list(tracks or [])
        self.playlists = list(playlists or [])
        self.albums = list(albums or [])
        # what one page of /playlists/{id}/items carries; default: the saved tracks
        self.playlist_items = list(playlist_items if playlist_items is not None else (tracks or []))
        self.has_more = has_more
        # SpotifyLoadLibraryMore (spec §14.3): a page beyond offset 0, and whether the
        # library's own list endpoints report a further page - separate from `has_more`
        # above (search/playlist), so setting one cannot leak into the other's test.
        self.tracks_has_more = tracks_has_more
        self.playlists_has_more = playlists_has_more
        self.albums_has_more = albums_has_more
        self.tracks_page2 = list(tracks_page2 or [])
        self.playlists_page2 = list(playlists_page2 or [])
        self.albums_page2 = list(albums_page2 or [])
        # GET /playlists/{id} (Task 5, spec §14.4): overrides the default {"id", "name": "Fokus"}
        # so a test can carry uri/images/items.total through to `spotify.playlist`.
        self.playlist_meta = dict(playlist_meta) if playlist_meta is not None else None
        # Task 7: raw track dicts for GET /me/player/recently-played, wrapped in {"track": t}
        # the way Spotify's own payload does - a test passes the same sequence it expects
        # collapsed (e.g. [TRACK, TRACK, TRACK2] for the consecutive-repeat check).
        self.recent = list(recent or [])
        # Task 2 (2026-09-29): each entry's `context` ({"type": "playlist", "uri": ...} or
        # None), aligned by index with `self.recent` - see `recent_contexts` in `get()`.
        self.recent_contexts = list(recent_contexts or [])
        # Task 8 ("Gefällt mir"): GET /me/library/contains?uris=... answers with the
        # matching uri's saved state (default False for a uri not named here).
        self.library_contains = dict(library_contains or {})
        # GET /playlists/{id} and/or /playlists/{id}/items answer 403 (spec §14.4, "kein
        # Zugriff"): a subset of {"meta", "items"} - raised as `spotify.Forbidden`, the same
        # class `_http_error` builds for a plain 403 with no known reason.
        self.forbid_playlist = set(forbid_playlist)
        self.fail = fail
        self.calls: list[tuple] = []
        self.logins: list[tuple] = []
        self.player = dict(player) if player else {}          # GET /me/player
        self.devices = list(devices or [])                    # GET /me/player/devices, Spotify shape
        self.send_fail = send_fail   # exception, or dict {(method, path): exception}
        self.on_send = on_send       # hook for re-entrancy (seek/volume coalescing)
        self.sent: list[tuple] = []

    def send(self, method, path, params=None, json_body=None):
        self.sent.append((method, path, dict(params or {}), json_body))
        if self.on_send is not None:
            self.on_send(method, path, params)
        fail = self.send_fail
        if isinstance(fail, dict):
            fail = fail.pop((method, path), None)
        if fail is not None:
            raise fail
        return {}

    def begin_login(self, verifier, state, redirect=""):
        self.logins.append((verifier, state, redirect))

    def exchange_code(self, code):
        self.calls.append(("exchange_code", code))
        if self.fail is not None:
            raise self.fail
        return {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600, "scope": "x"}

    @property
    def is_connected(self):
        return True

    def get_user(self):
        self.calls.append(("get_user",))
        if self.fail is not None:
            raise self.fail
        return self.user

    def get(self, path, params=None):
        self.calls.append(("get", path, dict(params or {})))
        if self.fail is not None:
            raise self.fail
        if path == "/search":
            types = str((params or {}).get("type") or "track").split(",")
            answer = {}
            if "track" in types:
                answer["tracks"] = {"items": self.search_items,
                                    "next": "x" if self.has_more else None}
            if "album" in types:
                answer["albums"] = {"items": self.search_albums,
                                    "next": "x" if self.has_more else None}
            if "playlist" in types:
                answer["playlists"] = {"items": self.search_playlists,
                                       "next": "x" if self.has_more else None}
            return answer
        if path == "/me/tracks":
            offset = int((params or {}).get("offset") or 0)
            page = self.tracks_page2 if offset > 0 else self.tracks
            # only the first page's `next` is driven by the fixture - the second page
            # (offset > 0) is the last one in these tests, so it reports none further.
            more = self.tracks_has_more if offset == 0 else False
            return {"items": [{"track": t} for t in page], "next": "x" if more else None}
        if path == "/me/playlists":
            offset = int((params or {}).get("offset") or 0)
            page = self.playlists_page2 if offset > 0 else self.playlists
            more = self.playlists_has_more if offset == 0 else False
            return {"items": page, "next": "x" if more else None}
        if path == "/me/albums":
            offset = int((params or {}).get("offset") or 0)
            page = self.albums_page2 if offset > 0 else self.albums
            more = self.albums_has_more if offset == 0 else False
            return {"items": [{"album": a} for a in page], "next": "x" if more else None}
        if path.startswith("/albums/"):
            # album detail (E5=a, live 2026-09-29): simplified tracks without `album`
            if path.endswith("/tracks"):
                return {"items": list(getattr(self, "album_tracks", [])),
                        "next": "x" if self.has_more else None}
            meta = getattr(self, "album_meta", None)
            return dict(meta) if meta is not None else {"id": path.split("/")[2], "name": "Album"}
        if path.startswith("/playlists/"):
            if path.endswith("/items"):
                if "items" in self.forbid_playlist:
                    from soundboard import spotify as _spotify
                    raise _spotify.Forbidden(detail="HTTP 403")
                # February 2026: the payload sits under `item`, not `track`
                return {"items": [{"item": t} for t in self.playlist_items],
                        "next": "x" if self.has_more else None,
                        "total": len(self.playlist_items)}
            if "meta" in self.forbid_playlist:
                from soundboard import spotify as _spotify
                raise _spotify.Forbidden(detail="HTTP 403")
            if self.playlist_meta is not None:
                return dict(self.playlist_meta)
            return {"id": path.split("/")[2], "name": "Fokus"}
        if path == "/me/player":
            return dict(self.player)
        if path == "/me/player/devices":
            return {"devices": list(self.devices)}
        if path == "/me/player/recently-played":
            # `recent_contexts[i]` (Task 2, 2026-09-29): the `context` of `self.recent[i]`,
            # aligned by index - a test names only the ones it needs, `None` elsewhere.
            contexts = self.recent_contexts
            return {"items": [
                {"track": t, "context": (contexts[i] if i < len(contexts) else None)}
                for i, t in enumerate(self.recent)
            ]}
        if path == "/me/library/contains":
            uris = str((params or {}).get("uris") or "").split(",")
            return [bool(self.library_contains.get(u, False)) for u in uris]
        return {}


class FakeMusicBus:
    """Stands in for musicbus.MusicBus: never touches WASAPI in the green run."""

    def __init__(self, gain=1.0, **_kwargs):
        self._gain = float(gain)
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.running_flag = False
        self.waiting = False
        self.error = None
        self.on_block = None
        self.on_error = None
        self.on_state = None

    @property
    def running(self):
        return self.running_flag

    @property
    def gain(self):
        return self._gain

    def set_gain(self, gain):
        self._gain = float(gain)

    def start(self):
        self.started += 1
        self.error = None
        self.running_flag = True

    def stop(self):
        self.stopped += 1
        self.running_flag = False

    def close(self):
        self.closed += 1
        self.running_flag = False

    def fail(self, exc):
        """Test helper: exactly what the real bus thread does on a runtime error."""
        self.error = exc
        self.running_flag = False
        if self.on_error is not None:
            self.on_error(exc)


class QueuedDevices:
    """A device thread stand-in whose jobs sit in a queue until the test lets them
    run, so a command sent while a device job is still queued lands ahead of it -
    exactly the race some ordering bugs depend on."""

    def __init__(self):
        self._inline = InlineExecutor()
        self._jobs: list[tuple] = []

    def submit(self, fn, *args):
        self._jobs.append((fn, args))

    def run_all(self):
        while self._jobs:
            fn, args = self._jobs.pop(0)
            fn(*args)

    def call_later(self, *args, **kwargs):
        return self._inline.call_later(*args, **kwargs)

    def is_current(self):
        return self._inline.is_current()

    @property
    def alive(self):
        return self._inline.alive

    def stop(self, *args, **kwargs):
        return self._inline.stop(*args, **kwargs)


class QueuedWorkers:
    """Stands in for `core.workers`: `submit` records the job instead of running it, so a
    test can hold back a worker task (e.g. Spotify's `_run_search`) and run it later, in
    whatever order the test wants - that is how the stale-answer guard (R1) gets tested."""

    def __init__(self):
        self.jobs: list[tuple] = []

    def submit(self, fn, *args) -> None:
        self.jobs.append((fn, args))

    def run_one(self, index: int = 0) -> None:
        fn, args = self.jobs.pop(index)
        fn(*args)

    def run_all(self) -> None:
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)


def bare_core(engine=None):
    """Core in test mode with a fake engine and every emitted event collected."""
    c = core.Core(inline=True, store_data=config._default_config())
    c.engine = engine if engine is not None else FakeEngine()
    events: list = []
    c.subscribe(events.append)
    return c, events


def of_type(events, cls):
    return [e for e in events if isinstance(e, cls)]


def make_core(**overrides):
    """The fully assembled core in test mode with every fake plugged in."""
    c = core.create_core(
        inline=True,
        engine=overrides.get("engine", FakeEngine()),
        backend=overrides.get("backend", FakeBackend()),
        hotkey_manager=overrides.get("hotkeys", FakeHotkeys()),
        store_data=overrides.get("store_data", config._default_config()),
        autostart_module=overrides.get("autostart", FakeAutostart()),
        update_source=overrides.get("updates", FakeReleaseSource()),
        update_dir=overrides.get("update_dir") or _UPDATE_DIR,
        spotify_api=overrides.get("spotify_api"),
        musicbus_factory=overrides.get("musicbus_factory", FakeMusicBus),
    )
    events: list = []
    c.subscribe(events.append)
    return c, events
