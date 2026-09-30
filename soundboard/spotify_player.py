"""Spotify F2: Ruckus als Connect-Fernbedienung (Spec §13 in 2026-09-27-spotify-musik-tab-design.md).

Der Ton kommt aus der Spotify-App; hier laufen nur Befehle und ein sparsames Polling.
Kern-Thread: Zustand, Entscheidungen. Worker: jeder Netzaufruf. Kein Token im Zustand.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Callable

from . import protocol as p
from .spotify import (AuthError, NetworkError, NoDevice, NotConnected, RateLimitError,
                      SpotifyError, map_track)

POLL_PLAYING = 5.0
POLL_IDLE = 20.0
REFRESH_AFTER_COMMAND = 0.7
REPEAT_MODES = ("off", "context", "track")


def map_device(item: dict) -> dict:
    volume = item.get("volume_percent")
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "type": str(item.get("type") or ""),
        "volume_percent": int(volume) if isinstance(volume, (int, float)) else None,
        "supports_volume": bool(item.get("supports_volume", volume is not None)),
        "active": bool(item.get("is_active")),
    }


def map_player(payload: dict, now: float) -> dict:
    """GET /me/player -> the state part's playback fields. `{}` (HTTP 204) = nothing plays."""
    payload = payload or {}
    item = payload.get("item")
    track = map_track(item) if isinstance(item, dict) and item.get("type", "track") == "track" else None
    device = payload.get("device")
    repeat = payload.get("repeat_state")
    return {
        "is_playing": bool(payload.get("is_playing")),
        "progress_ms": int(payload.get("progress_ms") or 0),
        "fetched_at": now,
        "track": track,
        "context_uri": (payload.get("context") or {}).get("uri") or None,
        "shuffle": bool(payload.get("shuffle_state")),
        "repeat": repeat if repeat in REPEAT_MODES else "off",
        "device": map_device(device) if isinstance(device, dict) else None,
    }


def pick_device(devices: list[dict], hostname: str, remembered: str) -> dict | None:
    """Active device > this PC's Spotify app (type Computer, name = hostname) > remembered."""
    for device in devices:
        if device.get("active"):
            return device
    host = (hostname or "").casefold()
    for device in devices:
        if device.get("type") == "Computer" and host and device.get("name", "").casefold() == host:
            return device
    for device in devices:
        if remembered and device.get("id") == remembered:
            return device
    return None


def play_body(uris, context_uri: str, offset_uri: str) -> dict:
    if uris:
        return {"uris": list(uris)}
    if context_uri:
        body: dict = {"context_uri": context_uri}
        if offset_uri:
            body["offset"] = {"uri": offset_uri}
        return body
    return {}


def next_poll_delay(is_playing: bool, retry_after: float = 0.0) -> float:
    base = POLL_PLAYING if is_playing else POLL_IDLE
    return max(base, retry_after)


log = logging.getLogger(__name__)


class SpotifyPlayerService:
    def __init__(self, core, spotify, *, clock: Callable[[], float] = time.time,
                 hostname: str | None = None):
        self._core = core
        self._spotify = spotify
        self._clock = clock
        self._hostname = hostname if hostname is not None else socket.gethostname()
        self._viewers: Callable[[], int] = lambda: 0
        self._player: dict | None = None     # map_player result
        self._devices: list[dict] | None = None
        self.error: str | None = None
        self._error_from_fetch: bool = False
        self._last_notice: str | None = None
        self._timer = None
        self._fetching = False
        self._quiet_until = 0.0
        self._inflight: set[str] = set()     # "seek" / "volume" running
        self._queued: dict[str, object] = {}  # newest waiting command per kind
        # Task 8 ("Gefällt mir", spec §14.6/R3): the like state lives apart from the poll -
        # `_saved` is the answer, `_saved_uri` is the uri it belongs to (or the one a check
        # is in flight for), `_saved_token` supersedes a stale contains answer or an older
        # SpotifySetSaved (a track change or a click always wins over what came before it).
        self._saved: dict | None = None      # {"uri", "saved"}
        self._saved_uri: str | None = None
        self._saved_token = 0
        for cls, fn in ((p.SpotifyPlay, self.play), (p.SpotifyPause, self.pause),
                        (p.SpotifyResume, self.resume), (p.SpotifyNext, self.next),
                        (p.SpotifyPrevious, self.previous), (p.SpotifySeek, self.seek),
                        (p.SpotifySetVolume, self.set_volume), (p.SpotifySetShuffle, self.set_shuffle),
                        (p.SpotifySetRepeat, self.set_repeat), (p.SpotifyAddToQueue, self.add_to_queue),
                        (p.SpotifyTransfer, self.transfer), (p.SpotifyLoadDevices, self.load_devices),
                        (p.SpotifySetSaved, self.set_saved)):
            core.handle(cls, fn)
        core.add_state("spotify_player", self.snapshot)
        core.subscribe(self._on_event)
        core.on_start(self._start)
        core.on_shutdown(self._stop)

    # ---- state ----

    def snapshot(self) -> dict | None:
        if not self._spotify.connected:
            return None
        base = self._player or map_player({}, 0.0)
        track = base.get("track")
        if track is not None:
            saved = self._saved if self._saved and self._saved["uri"] == track["uri"] else None
            track = {**track, "is_saved": saved["saved"] if saved else None}
        return {**base, "track": track, "devices": self._devices, "error": self.error}

    def attach_viewers(self, fn: Callable[[], int]) -> None:
        self._viewers = fn

    # ---- commands (core thread) ----

    def play(self, cmd) -> None:
        body = play_body(cmd.uris, cmd.context_uri, cmd.offset_uri)
        remembered = self._remembered
        hostname = self._hostname
        self._run(lambda api: self._play(api, body, remembered, hostname), optimistic={"is_playing": True})

    def resume(self, _cmd=None) -> None:
        remembered = self._remembered
        hostname = self._hostname
        self._run(lambda api: self._play(api, {}, remembered, hostname), optimistic={"is_playing": True})

    def pause(self, _cmd=None) -> None:
        self._run(lambda api: api.send("PUT", "/me/player/pause"), optimistic={"is_playing": False})

    def next(self, _cmd=None) -> None:
        self._run(lambda api: api.send("POST", "/me/player/next"))

    def previous(self, _cmd=None) -> None:
        self._run(lambda api: api.send("POST", "/me/player/previous"))

    def seek(self, cmd) -> None:
        position = max(0, int(cmd.position_ms))
        self._run(lambda api: api.send("PUT", "/me/player/seek", {"position_ms": position}),
                  optimistic={"progress_ms": position, "fetched_at": self._clock()},
                  kind="seek", cmd=cmd)

    def set_volume(self, cmd) -> None:
        volume = max(0, min(100, int(cmd.volume_percent)))
        if self._player and self._player.get("device"):
            self._player["device"] = {**self._player["device"], "volume_percent": volume}
        self._run(lambda api: api.send("PUT", "/me/player/volume", {"volume_percent": volume}),
                  kind="volume", cmd=cmd)

    def set_shuffle(self, cmd) -> None:
        on = bool(cmd.on)
        self._run(lambda api: api.send("PUT", "/me/player/shuffle", {"state": on}),
                  optimistic={"shuffle": on})

    def set_repeat(self, cmd) -> None:
        if cmd.mode not in REPEAT_MODES:
            return
        mode = cmd.mode
        self._run(lambda api: api.send("PUT", "/me/player/repeat", {"state": mode}),
                  optimistic={"repeat": mode})

    def add_to_queue(self, cmd) -> None:
        uri = cmd.uri
        self._run(lambda api: api.send("POST", "/me/player/queue", {"uri": uri}))

    def transfer(self, cmd) -> None:
        device_id = cmd.device_id

        def call(api):
            api.send("PUT", "/me/player", None, {"device_ids": [device_id], "play": bool(cmd.play)})
            return device_id
        self._run(call)

    def set_saved(self, cmd) -> None:
        """`SpotifySetSaved` (spec §14.6/R3): optimistic, `PUT`/`DELETE /me/library` with
        `uris`, reverted on failure. No call at all while `_needs_reconnect` stands (spec
        §14.1/E1) - the heart is hidden then anyway, but a stray command must not call out."""
        if not self._ready():
            return
        if self._spotify._needs_reconnect():
            return
        uri = cmd.uri
        saved = bool(cmd.saved)
        previous = self._saved.get("saved") if self._saved and self._saved.get("uri") == uri else None
        self._saved_token += 1   # supersedes any contains answer still out for this uri
        token = self._saved_token
        self._saved_uri = uri
        self._saved = {"uri": uri, "saved": saved}
        self._core.state_changed()
        self._core.workers.submit(self._work_set_saved, uri, saved, previous, token)

    def load_devices(self, _cmd=None) -> None:
        if not self._ready():
            return
        self._core.workers.submit(self._work, lambda api: api.get("/me/player/devices"), "devices")

    def refresh_now(self) -> bool:
        """Fetch the player state once (core thread; the call runs on a worker).

        Returns whether a fetch was actually started (False if declined: not connected,
        already fetching, or no api). The caller uses this - not `_fetching` afterwards,
        which a fetch that completes synchronously (tests) already cleared - to tell a
        decline from a finished fetch."""
        if not self._spotify.connected or self._fetching or self._spotify.api is None:
            return False
        if self._clock() < self._quiet_until:
            return False
        self._fetching = True
        self._core.workers.submit(self._fetch)
        return True

    # ---- plumbing ----

    def _ready(self) -> bool:
        if not self._spotify.connected or self._spotify.api is None:
            self._set_error(NotConnected())
            return False
        return True

    def _run(self, call, *, optimistic: dict | None = None, kind: str | None = None, cmd=None) -> None:
        if not self._ready():
            return
        if kind is not None:
            if kind in self._inflight:
                self._queued[kind] = cmd   # newest wins, sent once the running call is back
                return
            self._inflight.add(kind)
        if optimistic and self._player is not None:
            self._player.update(optimistic)
        elif optimistic:
            self._player = {**map_player({}, self._clock()), **optimistic}
        self._core.state_changed()
        self._core.workers.submit(self._work, call, kind)

    def _play(self, api, body: dict, remembered: str, hostname: str):
        """PUT play; without an active device pick this PC (spec §13.1) and retry there.

        Runs on a worker thread; remembered device id and hostname are passed in as arguments.
        """
        path = "/me/player/play"
        try:
            api.send("PUT", path, None, body or None)
            return None
        except NoDevice:
            devices = [map_device(d) for d in (api.get("/me/player/devices").get("devices") or [])]
            device = pick_device(devices, hostname, remembered)
            if device is None:
                raise
            api.send("PUT", path, {"device_id": device["id"]}, body or None)
            return device["id"]

    def _work(self, call, kind) -> None:  # worker
        api = self._spotify.api
        try:
            result = call(api)
        except SpotifyError as exc:
            self._core.executor.submit(self._command_failed, exc, kind)
            return
        except Exception as exc:  # noqa: BLE001 - never leave a kind in flight
            log.exception("spotify player call failed unexpectedly")
            self._core.executor.submit(self._command_failed, NetworkError(detail=str(exc)), kind)
            return
        self._core.executor.submit(self._command_done, result, kind)

    def _command_done(self, result, kind) -> None:  # core
        if kind == "devices":
            self._devices = [map_device(d) for d in (result.get("devices") or [])]
            self._clear_error()
            self._core.state_changed()
            return
        if isinstance(result, str) and result:
            self._remember_device(result)
        self._clear_error()
        self._finish_kind(kind)
        self._schedule(REFRESH_AFTER_COMMAND)

    def _command_failed(self, error: SpotifyError, kind) -> None:  # core
        self._finish_kind(kind, drop_queued=True)
        if self._handled_auth_or_rate_limit(error, from_fetch=False):
            return
        self._set_error(error)
        self._error_from_fetch = False
        self.refresh_now()                # the truth replaces the optimism

    def _finish_kind(self, kind, drop_queued: bool = False) -> None:
        if kind is None:
            return
        self._inflight.discard(kind)
        queued = self._queued.pop(kind, None)
        if queued is not None and not drop_queued:
            {"seek": self.seek, "volume": self.set_volume}[kind](queued)

    # ---- "Gefällt mir" (Task 8, spec §14.6/R3) ----

    def _on_event(self, event) -> None:  # core
        # A logout ends the account the like answer belongs to - a login (maybe another
        # account) inside one poll interval must ask again (final review Minor 7).
        if isinstance(event, p.SpotifyAuthChanged) and not event.connected:
            self._saved = None
            self._saved_uri = None
            self._saved_token += 1  # a click or check still out belongs to the old account

    def _work_set_saved(self, uri: str, saved: bool, previous, token: int) -> None:  # worker
        api = self._spotify.api
        method = "PUT" if saved else "DELETE"
        try:
            # the uris go in the query string: a JSON body gets 400 "Missing required
            # field: uris" (live 2026-09-29)
            api.send(method, "/me/library", {"uris": uri})
        except SpotifyError as exc:
            self._core.executor.submit(self._set_saved_failed, uri, previous, token, exc)
            return
        except Exception as exc:  # noqa: BLE001 - never leave the click unresolved
            log.exception("spotify set-saved failed unexpectedly")
            self._core.executor.submit(self._set_saved_failed, uri, previous, token,
                                       NetworkError(detail=str(exc)))
            return
        self._core.executor.submit(self._set_saved_done, token)

    def _set_saved_done(self, token: int) -> None:  # core
        if token != self._saved_token:
            return  # superseded meanwhile - nothing left to confirm
        self._clear_error()
        self._core.state_changed()

    def _set_saved_failed(self, uri: str, previous, token: int, error: SpotifyError) -> None:  # core
        if token == self._saved_token and self._saved and self._saved.get("uri") == uri:
            self._saved = {"uri": uri, "saved": previous} if previous is not None else None
        if self._handled_auth_or_rate_limit(error, from_fetch=False):
            return  # 401 drops the login, 429 waits for Retry-After - like any command
        self._set_error(error)   # same reporting as any other player command failure

    def _maybe_check_saved(self) -> None:  # core, called once a fresh player state is in
        track = self._player.get("track") if self._player else None
        uri = track.get("uri") if track else ""
        if not uri or self._saved_uri == uri:
            return  # nothing playing, or already asked/known for this track (a poll keeps it)
        if self._spotify._needs_reconnect():
            return  # no scope -> no call at all (spec §14.1/E1); is_saved stays null
        self._saved_uri = uri
        self._saved_token += 1
        token = self._saved_token
        self._core.workers.submit(self._work_check_saved, uri, token)

    def _work_check_saved(self, uri: str, token: int) -> None:  # worker
        api = self._spotify.api
        try:
            result = api.get("/me/library/contains", {"uris": uri})
        except SpotifyError as exc:
            self._core.executor.submit(self._check_saved_failed, uri, token, exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("spotify saved-check failed unexpectedly")
            self._core.executor.submit(self._check_saved_failed, uri, token, NetworkError(detail=str(exc)))
            return
        self._core.executor.submit(self._checked_saved, uri, token, result)

    def _checked_saved(self, uri: str, token: int, result) -> None:  # core
        if token != self._saved_token or self._saved_uri != uri:
            return  # a track change or a click superseded this - the late answer is discarded
        saved = bool(result[0]) if isinstance(result, list) and result else False
        self._saved = {"uri": uri, "saved": saved}
        self._core.state_changed()

    def _check_saved_failed(self, uri: str, token: int, error: SpotifyError) -> None:  # core
        if token != self._saved_token or self._saved_uri != uri:
            return  # superseded already; nothing to retry
        if error.detail:
            log.info("spotify saved-check: %s", error.detail)
        self._saved_uri = None  # unresolved - a later poll of the same track tries again
        self._handled_auth_or_rate_limit(error, from_fetch=False)  # 401/429 as everywhere

    @property
    def _remembered(self) -> str:
        return str(self._core.store.data.get("spotify_device_id") or "")

    def _remember_device(self, device_id: str) -> None:
        if device_id and device_id != self._remembered:
            self._core.store.data["spotify_device_id"] = device_id
            self._core.store.save_soon()

    def _set_error(self, error: SpotifyError) -> None:
        if error.detail:
            log.info("spotify player: %s", error.detail)
        self.error = error.text
        if error.text != self._last_notice:
            self._last_notice = error.text
            self._core.notice(error.text, "error")
        self._core.emit(p.SpotifyError(error.text))
        self._core.state_changed()

    def _clear_error(self) -> None:
        self.error = None
        self._error_from_fetch = False
        self._last_notice = None

    def _handled_auth_or_rate_limit(self, error: SpotifyError, *, from_fetch: bool) -> bool:
        """The two answers commands and fetches treat alike. True if `error` was one of them."""
        if isinstance(error, AuthError):
            self._player = None
            self._spotify.report(error)   # drops the login like F1
            if from_fetch:
                self._schedule(POLL_IDLE)
            return True
        if isinstance(error, RateLimitError):
            self._quiet_until = self._clock() + error.retry_after
            self._set_error(error)
            self._error_from_fetch = from_fetch
            self._schedule(error.retry_after)
            return True
        return False

    # ---- fetching + polling ----

    def _fetch(self) -> None:  # worker
        try:
            payload = self._spotify.api.get("/me/player")
        except SpotifyError as exc:
            self._core.executor.submit(self._fetch_failed, exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("spotify player fetch failed unexpectedly")
            self._core.executor.submit(self._fetch_failed, NetworkError(detail=str(exc)))
            return
        self._core.executor.submit(self._fetched, payload)

    def _fetched(self, payload: dict) -> None:  # core
        self._fetching = False
        self._player = map_player(payload, self._clock())
        if self._error_from_fetch:
            self._clear_error()
        self._maybe_check_saved()
        self._core.state_changed()
        self._schedule(next_poll_delay(self._player["is_playing"]))

    def _fetch_failed(self, error: SpotifyError) -> None:  # core
        self._fetching = False
        if self._handled_auth_or_rate_limit(error, from_fetch=True):
            return
        self._set_error(error)
        self._error_from_fetch = True
        self._schedule(POLL_IDLE)

    def _schedule(self, delay: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self._core.executor.call_later(delay, self._tick)

    def _tick(self) -> None:  # core
        """One timer, never two. Quiet (no network) without connection, viewer or during
        Retry-After; then it only wakes every POLL_IDLE s to look again."""
        self._timer = None
        if not self._spotify.connected or self._spotify.api is None:
            self._player = None
            self._devices = None
            self.error = None
            self._error_from_fetch = False
            self._last_notice = None
            self._quiet_until = 0.0
            self._saved = None
            self._saved_uri = None
            self._schedule(POLL_IDLE)
            return
        now = self._clock()
        if now < self._quiet_until:
            self._schedule(self._quiet_until - now)
            return
        if self._viewers() <= 0:
            self._schedule(POLL_IDLE)
            return
        if not self.refresh_now():      # declined: keep the alarm alive
            self._schedule(POLL_IDLE)

    def _start(self) -> None:
        self._schedule(0.0)

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
