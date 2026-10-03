"""Spotify im Musik-Tab: OAuth-PKCE, Token-Ablage, Suche und Bibliothek (nur lesen).

Muster wie library.py: das Fachmodul registriert sich selbst beim Core. Netz laeuft nur auf
Workern, Zustand nur auf dem Kern-Thread, das Token nur im gemeinsamen Secrets-Speicher
(`Store.secret/set_secret/forget_secret`) - nie in config.json, nie im Snapshot, nie im Log.

Der Rueckkanal der Anmeldung ist eine Route des lokalen Servers (`/callback`), kein eigener
Listener: es ist derselbe Server (Spec §11). Ohne Server (reines Tk-Fenster) meldet die
Anmeldung das offen, statt auf einem zweiten Port zu lauschen, den das Spotify-Dashboard
nicht kennt.

Variante A (Connect-Remote): die App ist Fernbedienung, der Ton kommt aus der
Spotify-Desktop-App. Deshalb gibt es hier in F1 noch keinen Wiedergabe-Teil (F2).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

log = logging.getLogger(__name__)

CLIENT_ID_ENV = "RUCKUS_SPOTIFY_CLIENT_ID"
# Spec §14.1 (E1=a): "Like" (user-library-modify, Task 8) and "Zuletzt gespielt"
# (user-read-recently-played, this task) - requested together so one reconnect covers both.
NEW_SCOPES = ("user-library-modify", "user-read-recently-played")
SCOPES = ("user-read-private", "user-library-read",
          "playlist-read-private", "playlist-read-collaborative", "user-follow-read",
          "user-read-playback-state", "user-modify-playback-state", *NEW_SCOPES)
SEARCH_KINDS = ("track", "album", "playlist", "artist", "all")
# The `loading`/`_request_seq` sections (R1/R2): not Spotify's OAuth `SCOPES` above - these
# are SpotifyService's own request sections, "login" included (see `login`/`logout`).
_REQUEST_SCOPES = ("search", "library", "playlist", "recent", "login")
# Spotify halved the maximum of GET /search to 10 in February 2026 (the default is 5).
SEARCH_LIMIT = 10
# Our own page size for the list endpoints (playlist items); Spotify does not cap these at 10.
PAGE = 20
REDIRECT_PATH = "/callback"
#: The one callback URL: the local server's own route, on the configured `server_port`.
#: Spotify needs the exact URL, so it may not be a fallback port - `login` checks that.


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{REDIRECT_PATH}"


CALLBACK_PAGE = (b"<html><body><h1>Ruckus Radio</h1>"
                 b"<p>Fertig - du kannst dieses Fenster schlie\xc3\x9fen.</p></body></html>")
CALLBACK_ERROR_PAGE = (b"<html><body><h1>Ruckus Radio</h1>"
                       b"<p>Diese Anmeldung ist nicht mehr aktiv. Starte sie im Musik-Tab"
                       b" neu.</p></body></html>")
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
TOKEN_FILE = "spotify_token.json"  # only read once, by TokenStore.migrate_legacy
SECRET_NAME = "spotify_token"      # one entry in the shared secrets store, holding our JSON
LOGIN_TIMEOUT_S = 120.0
UNKNOWN_ARTIST = "Unbekannt"
DEFAULT_KIND = "track"


class SpotifyError(Exception):
    """Every Spotify problem. `text` is the one line the interface shows.

    Without an argument the class text applies; a text passed in wins. That matters:
    `_fail` always shows `text`, so without this a busy port, a callback timeout and an
    expired login would all read like "the login expired" - the interface would hide
    the actual cause. `detail` carries the technical reason for the log only.
    """

    text = "Spotify ist gerade nicht erreichbar."

    def __init__(self, text: str | None = None, *, detail: str = "") -> None:
        super().__init__(text or self.text)
        if text:
            self.text = text
        self.detail = detail


class NotConfigured(SpotifyError):
    text = ("Keine Spotify-Client-ID hinterlegt. Trag sie in config.json als "
            "spotify_client_id ein.")


class NotConnected(SpotifyError):
    text = "Nicht mit Spotify verbunden."


class NetworkError(SpotifyError):
    text = ("Keine Verbindung zu Spotify. Prüf die Internetverbindung und versuch es "
            "noch einmal.")


class AuthError(SpotifyError):
    text = "Die Spotify-Anmeldung ist abgelaufen. Verbinde dich erneut."


class RateLimitError(SpotifyError):
    text = "Spotify bremst gerade. Versuch es in ein paar Sekunden noch einmal."

    def __init__(self, retry_after: float = 5.0, *, detail: str = ""):
        super().__init__(detail=detail)
        self.retry_after = retry_after


class ApiError(SpotifyError):
    text = "Spotify hat die Anfrage abgelehnt."


class PremiumRequired(ApiError):
    text = "Steuern braucht Spotify Premium."


class NoDevice(ApiError):
    text = "Öffne die Spotify-App auf diesem Rechner."


REFUSED = "Spotify erlaubt das gerade nicht."


class Forbidden(ApiError):
    """A plain 403 with no known reason (not PREMIUM_REQUIRED/NO_ACTIVE_DEVICE).

    Since February 2026 Spotify serves playlist content only for the user's own
    playlists - live 2026-09-29 also 403 as co-author (spec §14.4); `_run_playlist` catches this one specifically and
    turns it into the empty "kein Zugriff" state instead of an error. Still an `ApiError`
    subclass, so callers that only catch `ApiError` see no change (Eng-Review brief).
    """

    text = REFUSED


# ---- pure helpers (no network) ----

def make_verifier() -> str:
    """64 URL-safe characters, RFC 7636."""
    return secrets.token_urlsafe(48)[:64]


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id: str, challenge: str, state: str, redirect: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    })


def missing_scopes(scope: str, required: tuple[str, ...] = NEW_SCOPES) -> tuple[str, ...]:
    """Which of `required` a stored token's `scope` string does not name.

    Only `NEW_SCOPES` matter here (spec §14.1, E1): an old token missing them is a hint
    to reconnect, not a broken connection - search/library keep working on the scopes it
    already has. Task 8 ("Like") reuses this for the same two scopes.
    """
    have = set((scope or "").split())
    return tuple(s for s in required if s not in have)


def has_scopes(scope: str, required: tuple[str, ...] = NEW_SCOPES) -> bool:
    return not missing_scopes(scope, required)


def callback_result(path: str) -> tuple[str | None, str]:
    """(code, state) on success; (None, error text) otherwise."""
    query = parse_qs(urlsplit(path).query)
    error = (query.get("error") or [None])[0]
    if error:
        return None, f"Spotify hat die Anmeldung abgelehnt ({error})."
    code = (query.get("code") or [None])[0]
    if not code:
        return None, "Die Spotify-Antwort enthielt keinen Code."
    return code, (query.get("state") or [""])[0]


def _image(entry: dict) -> str | None:
    for image in entry.get("images") or []:
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])
    return None


def _album_image(entry: dict) -> str | None:
    album = entry.get("album")
    return _image(album) if isinstance(album, dict) else None


def _first_artist(entry: dict) -> str:
    for artist in entry.get("artists") or []:
        name = artist.get("name") if isinstance(artist, dict) else None
        if name:
            return str(name)
    return UNKNOWN_ARTIST


def map_track(item: dict) -> dict:
    external = item.get("external_urls") or {}
    return {
        "id": str(item.get("id") or ""),
        "uri": str(item.get("uri") or ""),
        "title": str(item.get("name") or ""),
        "artist": _first_artist(item),
        "album": str((item.get("album") or {}).get("name") or ""),
        "duration_ms": int(item.get("duration_ms") or 0),
        "cover_url": _album_image(item),
        "external_url": external.get("spotify"),
    }


def map_playlist(item: dict) -> dict:
    owner = item.get("owner") or {}
    # Spotify renamed the playlist's `tracks` object to `items` in February 2026.
    contents = item.get("items") or item.get("tracks") or {}
    return {
        "id": str(item.get("id") or ""),
        "uri": str(item.get("uri") or ""),
        "name": str(item.get("name") or ""),
        "owner": str(owner.get("display_name") or owner.get("id") or ""),
        "track_count": int(contents.get("total") or 0),
        "cover_url": _image(item),
    }


def list_entry(entry: dict) -> dict | None:
    """The track/album inside one playlist page entry.

    Playlist pages used to carry the payload under `track`; since February 2026 it is
    `item`. Both are read so a stale or reverted answer cannot break the list.
    """
    for key in ("item", "track"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return None


def _library_track(entry: dict) -> dict | None:
    """One `/me/tracks` page entry, same shape `_library_loaded` reads for the first page."""
    track = entry.get("track") if isinstance(entry, dict) else None
    return map_track(track) if isinstance(track, dict) else None


def _library_playlist(entry: dict) -> dict | None:
    """One `/me/playlists` page entry - the playlist dict itself, no wrapper."""
    return map_playlist(entry) if isinstance(entry, dict) else None


def map_album(item: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "uri": str(item.get("uri") or ""),
        "name": str(item.get("name") or ""),
        "artist": _first_artist(item),
        "cover_url": _image(item),
    }


def _library_album(entry: dict) -> dict | None:
    """One `/me/albums` page entry, same wrapper shape `_library_loaded` reads for the
    first page (`entry["album"]`, spec §14.3, E5=b)."""
    album = entry.get("album") if isinstance(entry, dict) else None
    return map_album(album) if isinstance(album, dict) else None


# `SpotifyLoadLibraryMore` (spec §14.3): endpoint + row mapper per known section. An
# unknown section is discarded.
_LIBRARY_SECTIONS = {
    "tracks": ("/me/tracks", _library_track),
    "playlists": ("/me/playlists", _library_playlist),
    "albums": ("/me/albums", _library_album),
}


_MAPPERS = {"track": map_track, "album": map_album, "playlist": map_playlist}
_GROUPS = {"track": "tracks", "album": "albums", "playlist": "playlists", "artist": "artists"}
# The "Alle" chip (spec §14.2, E2=a): one `/search` call for these three types at once,
# in the order the three group headings are shown ("Titel", "Alben", "Playlists").
_ALL_SEARCH_TYPES = ("track", "album", "playlist")


def search_items(payload: dict, kind: str) -> tuple[list[dict], bool]:
    """Items and has_more for one search page. `kind` picks the group and the mapper."""
    mapper = _MAPPERS.get(kind)
    if mapper is None:
        return [], False
    group = payload.get(_GROUPS.get(kind, "")) or {}
    items = [mapper(entry) for entry in group.get("items") or [] if isinstance(entry, dict)]
    return items, bool(group.get("next"))


class TokenStore:
    """The refresh token in the shared secrets store - never in config.json.

    Its own class so the shape (`{"refresh_token", "scope"}`) and the one-time takeover
    of the old `spotify_token.json` live in one place. `store` only has to offer
    `secret()`, `set_secret()` and `forget_secret()` (see `soundboard/store.py`).
    """

    def __init__(self, store, legacy_dir=None):
        self._store = store
        # Only used to take over the file the token used to live in; None disables it.
        base = legacy_dir if legacy_dir is not None else getattr(store, "data_dir", None)
        self.legacy_path = (Path(base) / TOKEN_FILE) if base else None

    def load(self) -> dict | None:
        raw = self._store.secret(SECRET_NAME)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("the stored %s is unreadable; treating it as not connected", SECRET_NAME)
            return None
        if not isinstance(data, dict) or not data.get("refresh_token"):
            return None
        return data

    def save(self, data: dict) -> None:
        if not data.get("refresh_token"):
            return
        self._store.set_secret(SECRET_NAME, json.dumps(
            {"refresh_token": str(data["refresh_token"]),
             "scope": str(data.get("scope") or "")}))

    def clear(self) -> None:
        self._store.forget_secret(SECRET_NAME)

    def migrate_legacy(self) -> None:
        """One-time: take the token over from `spotify_token.json` of earlier versions.

        Without this every existing user would have to sign in again after the switch to
        the secrets store. A file we cannot parse is left **alone** - it may be the only
        copy of a live login, so deleting it would cost the user a new sign-in.
        """
        if self.legacy_path is None or not self.legacy_path.exists():
            return
        try:
            data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning("could not read %s (%s); leaving it in place",
                        self.legacy_path.name, exc)
            return
        if not isinstance(data, dict) or not data.get("refresh_token"):
            return
        self.save(data)
        try:
            self.legacy_path.unlink()
            log.info("moved the Spotify token into the secrets store")
        except OSError:
            log.warning("could not remove %s", self.legacy_path.name, exc_info=True)


class SpotifyApi:
    """The only place that talks HTTP to Spotify. Injected in tests via `http`."""

    def __init__(self, client_id: str, tokens: TokenStore, *, timeout: float = 10.0, http=None):
        self._client_id = client_id
        self._tokens = tokens
        self._timeout = timeout
        self._http = http if http is not None else self._urlopen
        self._access: str | None = None
        self._expires_at = 0.0
        self._verifier: str | None = None
        self._redirect = ""

    # ---- the raw call, isolated so tests can replace it ----

    @staticmethod
    def _urlopen(request, timeout=None):
        import urllib.request
        return urllib.request.urlopen(request, timeout=timeout)

    def _request(self, method: str, url: str, *, data: dict | None = None,
                 headers: dict | None = None, body: bytes | None = None,
                 lenient: bool = False) -> dict:
        import http.client
        import urllib.error
        import urllib.request
        if data is not None:
            body = urlencode(data).encode("ascii")
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with self._http(request, self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self._http_error(exc)
            raise  # unreachable: _http_error always raises
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # Keep the friendly `NetworkError.text`; the raw cause is for the log.
            raise NetworkError(detail=str(exc)) from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if lenient:
                # A player command is done once Spotify says 2xx; its body is no data
                # (live 2026-09-28: pause answered 200 with a body that was no JSON).
                return {}
            raise ApiError(detail=str(exc)) from exc

    @staticmethod
    def _http_error(exc) -> None:
        retry = exc.headers.get("Retry-After") if exc.headers else None
        # Spotify names the reason in the body ("invalid_client", "invalid_grant",
        # "redirect_uri mismatch"). Without it a failed sign-in is undiagnosable, so it
        # travels along as `detail` - for the log, never for the interface.
        detail = f"HTTP {exc.code}"
        # the path says which call failed (hand check 2026-09-29: a bare 403 did not);
        # only the path - the query may carry ids, the token is a header anyway
        path = urlsplit(getattr(exc, "url", None) or "").path
        if path:
            detail = f"{detail} {path}"
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - a missing body must not hide the status
            body = ""
        if body:
            detail = f"{detail}: {body[:400]}"
        if exc.code == 429:
            try:
                seconds = float(retry)
            except (TypeError, ValueError):
                seconds = 5.0
            raise RateLimitError(seconds, detail=detail)
        if exc.code == 401:
            raise AuthError(detail=detail)
        if exc.code in (403, 404):
            reason = ""
            try:
                reason = str((json.loads(body) or {}).get("error", {}).get("reason") or "")
            except (ValueError, AttributeError):
                pass
            if reason == "PREMIUM_REQUIRED":
                raise PremiumRequired(detail=detail)
            if reason == "NO_ACTIVE_DEVICE":
                raise NoDevice(detail=detail)
            if exc.code == 403:
                raise Forbidden(detail=detail)
        if exc.code >= 500:
            raise NetworkError(f"Spotify antwortet nicht ({exc.code}).", detail=detail)
        raise ApiError(f"Spotify hat die Anfrage abgelehnt ({exc.code}).", detail=detail)

    # ---- auth ----

    def begin_login(self, verifier: str, state: str, redirect: str = "") -> None:
        self._verifier = verifier
        # Spotify demands the very same redirect URI at the exchange as at the authorize
        # step, so the login carries it along instead of reading a constant.
        self._redirect = redirect

    def _token_request(self, data: dict) -> dict:
        filled = {"client_id": self._client_id, **data}
        return self._request("POST", TOKEN_URL, data=filled,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})

    def exchange_code(self, code: str) -> dict:
        if self._verifier is None:
            raise AuthError("Der Anmeldelauf wurde nicht begonnen.")
        answer = self._token_request({"grant_type": "authorization_code", "code": code,
                                      "redirect_uri": self._redirect,
                                      "code_verifier": self._verifier})
        self._verifier = None
        self._remember(answer)
        return answer

    @property
    def is_connected(self) -> bool:
        stored = self._tokens.load()
        return bool(self._access or (stored and stored.get("refresh_token")))

    def refresh(self) -> None:
        stored = self._tokens.load()
        if not stored or not stored.get("refresh_token"):
            raise NotConnected()
        try:
            answer = self._token_request({"grant_type": "refresh_token",
                                          "refresh_token": stored["refresh_token"]})
        except ApiError as exc:
            # a revoked or expired refresh token answers 400 invalid_grant (live
            # 2026-09-29, app removed at spotify.com/account/apps): the login is over,
            # like a 401 - not a refused request that the next try might get through
            if "invalid_grant" in exc.detail:
                raise AuthError(detail=exc.detail) from exc
            raise
        self._remember(answer)

    def _remember(self, answer: dict) -> None:
        access = answer.get("access_token")
        if access:
            self._access = str(access)
            expires = max(30.0, float(answer.get("expires_in") or 3600))
            self._expires_at = time.monotonic() + expires - 60.0
        refresh = answer.get("refresh_token")
        if refresh:
            # R4 (eng review): a refresh answer may repeat the refresh_token without
            # `scope` - Spotify does this - so a missing key keeps what was already
            # stored instead of overwriting it with "" (which would falsely demand a
            # reconnect on the next check).
            if "scope" in answer:
                scope = str(answer.get("scope") or "")
            else:
                stored = self._tokens.load()
                scope = stored.get("scope", "") if stored else ""
            self._tokens.save({"refresh_token": str(refresh), "scope": scope})

    def _ensure_token(self) -> None:
        if self._access and time.monotonic() < self._expires_at:
            return
        self.refresh()

    # ---- api ----

    def _authorized(self, method: str, url: str, body: bytes | None, json_type: bool,
                    lenient: bool = False) -> dict:
        self._ensure_token()

        def once() -> dict:
            headers = {"Authorization": f"Bearer {self._access}"}
            if json_type:
                headers["Content-Type"] = "application/json"
            return self._request(method, url, headers=headers, body=body, lenient=lenient)
        try:
            return once()
        except AuthError:
            self._access = None
            self.refresh()  # exactly one retry after a refresh
            return once()

    @staticmethod
    def _url(path: str, params: dict | None) -> str:
        url = API_BASE + path
        if params:
            clean = {k: (str(v).lower() if isinstance(v, bool) else v)
                     for k, v in params.items() if v is not None}
            url += "?" + urlencode(clean)
        return url

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._authorized("GET", self._url(path, params), None, False)

    def send(self, method: str, path: str, params: dict | None = None,
             json_body: dict | None = None) -> dict:
        """PUT/POST to the player. Spotify answers most of them with 204 -> {}.
        A PUT without JSON still carries an empty body: Spotify wants Content-Length."""
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        return self._authorized(method, self._url(path, params), body, json_body is not None,
                                lenient=True)

    def get_user(self) -> dict:
        return self.get("/me")


class _CallbackListener:
    """One-shot OAuth redirect as a route of the local server: one request, then drop it.

    Spec §11: "the OAuth listener goes away, it is the same server". `host` is the
    `webserver.SseServer`; it owns the port, the Host and Origin checks and the key rule.
    The shape of the old listener is kept on purpose - `wait`, `close`, `code`, `state`,
    `error`, `done` - so the login path did not have to change: the request thread delivers
    and sets `done`, a worker waits on it.
    """

    def __init__(self, host, state: str):
        self._host = host
        self.expected_state = state
        self.code: str | None = None
        self.state = ""
        self.error: str | None = None
        self.done = threading.Event()
        self._closed = False
        self._live = False
        host.add_route(REDIRECT_PATH, self._handle)
        self._live = True

    def _handle(self, handler, query, body):
        """`webserver` route signature. Runs on the server's request thread."""
        code, got_state = callback_result(handler.path)
        if code is None:
            self.error = got_state
        else:
            self.code = code
            self.state = got_state
        self.done.set()
        return 200, "text/html; charset=utf-8", CALLBACK_PAGE

    def wait(self, timeout: float) -> tuple[str | None, str]:
        if not self.done.wait(timeout):
            raise TimeoutError("no callback")
        if self.error is not None or self.code is None:
            raise SpotifyError(self.error or "Die Anmeldung wurde abgebrochen.")
        return self.code, self.state

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.done.set()  # release a worker still waiting
        if self._live:
            self._live = False
            self._host.remove_route(REDIRECT_PATH)


class SpotifyService:
    """The music tab's data: auth, search, library. Registers itself with the core."""

    def __init__(self, core, api=None, token_store=None):
        from .protocol import (SpotifyLoadAlbum, SpotifyLoadLibrary, SpotifyLoadPlaylist, SpotifyLogin,
                               SpotifyLogout, SpotifySearch, SpotifySearchMore,
                               SpotifyLoadLibraryMore, SpotifyLoadRecent)
        self._core = core
        self.tokens = token_store if token_store is not None else TokenStore(core.store)
        self.client_id = ""
        self._api = api
        self._pending: tuple[str, str] | None = None
        # Each login and each logout moves this on: a worker's answer for an older login
        # (the user logged out or started over meanwhile) is dropped, like R1 for sections.
        self._login_seq = 0
        self._server = None  # the local server, via attach_server: it owns /callback
        self._listener: _CallbackListener | None = None
        self._last_notice: str | None = None
        self.connected = False
        self.user: dict | None = None
        self.error: str | None = None  # derived: the most recently set error text
        self.search: dict | None = None
        self.library: dict | None = None
        self.playlist: dict | None = None
        self.recent: list[dict] | None = None  # SpotifyTrack[], spec §14.7
        # Spotify names no play counts per playlist; `recently-played`'s `context` is the
        # only approximation there is (Jan, hand check 2026-09-29) - counted fresh on every
        # `_recent_loaded`, never persisted (spec §2.6), cleared on logout like `recent`.
        self.playlist_plays: dict[str, int] = {}
        # R1/R2 (eng review): `loading` names the sections currently in flight - "login" is
        # one of them too (the simplest way to keep its own busy handling covered by the
        # derived `busy` below, instead of a second flag). `errors` holds the latest text
        # per section (`search`/`library`/`playlist`/`recent`); `_request_seq` counts each
        # section's requests so a stale answer (an older request's success or failure,
        # arriving after a newer one for the same section) is discarded, never applied.
        self.loading: list[str] = []
        self.errors: dict[str, str] = {}
        self._request_seq: dict[str, int] = {}
        core.handle(SpotifyLogin, self.login)
        core.handle(SpotifyLogout, self.logout)
        core.handle(SpotifySearch, self.search_command)
        core.handle(SpotifySearchMore, self.search_command)
        core.handle(SpotifyLoadLibrary, self.load_library)
        core.handle(SpotifyLoadPlaylist, self.load_playlist)
        core.handle(SpotifyLoadAlbum, self.load_album)
        core.handle(SpotifyLoadLibraryMore, self.load_library_more)
        core.handle(SpotifyLoadRecent, self.load_recent)
        core.add_state("spotify", self.snapshot)
        core.on_start(self._read_client_id)
        core.on_shutdown(self._close_listener)

    # ---- lifecycle ----

    @property
    def api(self):
        """The shared SpotifyApi (built once the client id is known) - F2 uses it too."""
        return self._api

    def report(self, error: SpotifyError) -> None:  # core
        """Same handling as a failed search: an AuthError drops the login."""
        self._failed(None, None, error)

    def _read_client_id(self) -> None:
        self.tokens.migrate_legacy()  # one-time, from the pre-secrets-store version
        stored = self._core.store.data.get("spotify_client_id") or ""
        self.client_id = os.environ.get(CLIENT_ID_ENV) or stored
        if self._api is None and self.client_id:
            self._api = SpotifyApi(self.client_id, self.tokens)
        self.connected = bool(self.client_id) and self.tokens.load() is not None

    def snapshot(self) -> dict:
        """Never carries a token, the client id or a file path - the interface must not need them."""
        return {"configured": bool(self.client_id), "connected": self.connected,
                "user": self.user, "busy": bool(self.loading), "error": self.error,
                "search": self.search, "library": self.library, "playlist": self.playlist,
                "recent": self.recent, "loading": list(self.loading), "errors": dict(self.errors),
                "playlist_plays": dict(self.playlist_plays),
                "needs_reconnect": self._needs_reconnect()}

    # ---- auth ----

    def attach_server(self, host) -> None:
        """Hand the local server over: the callback is one of its routes (spec §11).

        Called by `webmain.run` right after the server exists; without it the login says
        so instead of opening a second listener on a port Spotify does not know.
        """
        self._server = host

    def _callback_url(self) -> str | None:
        """The registered redirect URL, or None after reporting why it cannot be used.

        The Spotify dashboard knows exactly one URL, so a server that fell back to another
        port would fail at the token exchange with Spotify's opaque "redirect_uri
        mismatch". Much better to name the cause here.
        """
        if self._server is None:
            self._fail("login", SpotifyError("Die Spotify-Anmeldung braucht den lokalen Server - "
                                          "starte die Web-Oberfläche (Standard) oder --serve."))
            return None
        try:
            wanted = int(self._core.store.data.get("server_port") or 0)
        except (TypeError, ValueError):
            wanted = 0
        port = int(getattr(self._server, "port", 0) or 0)
        if wanted and port != wanted:
            self._fail("login", SpotifyError(
                f"Die Oberfläche läuft auf Port {port}, die Spotify-Weiterleitung zeigt auf "
                f"{wanted}. Schließ das Programm, das {wanted} belegt, und starte neu."))
            return None
        return redirect_uri(port)

    def login(self, _cmd=None) -> None:
        if not self.client_id:
            self._fail("login", NotConfigured())
            return
        if self._pending is not None:
            return
        uri = self._callback_url()
        if uri is None:
            return  # _callback_url already told the user why
        verifier, state = make_verifier(), make_verifier()
        self._pending = (verifier, state)
        assert self._api is not None
        self._api.begin_login(verifier, state, uri)
        try:
            self._listener = _CallbackListener(self._server, state)
        except ValueError as exc:  # the route is still taken: a login is running
            self._pending = None
            self._fail("login", SpotifyError("Es läuft schon eine Spotify-Anmeldung."))
            log.warning("spotify callback route busy: %s", exc)
            return
        import webbrowser
        webbrowser.open(authorize_url(self.client_id, challenge_of(verifier), state, uri))
        # "login" is a section of `loading` too (R2): the simplest way to keep its own
        # busy handling covered by the derived `busy`, without a second flag.
        self._start_loading("login")
        # a new attempt makes the last login failure moot (`errors["login"]`, final review
        # Minor 3); another section's error must stand
        self.errors.pop("login", None)
        self._recompute_error()
        self._core.state_changed()
        self._login_seq += 1
        self._core.workers.submit(self._await_callback, self._listener, self._login_seq)

    def _await_callback(self, listener: "_CallbackListener", seq: int) -> None:  # worker
        # the listener comes as an argument: a logout clears `self._listener` at any time
        try:
            code, state = listener.wait(LOGIN_TIMEOUT_S)
        except TimeoutError:
            self._core.executor.submit(self._login_failed,
                                       "Zeitüberschreitung. Verbinde dich erneut.", seq)
            return
        except SpotifyError as exc:
            self._core.executor.submit(self._login_failed, exc, seq)
            return
        except Exception as exc:  # noqa: BLE001 - never leave the login stuck
            log.exception("spotify callback wait failed")
            self._core.executor.submit(self._login_failed, str(exc), seq)
            return
        if state != listener.expected_state:
            self._core.executor.submit(self._login_failed, "Die Antwort passte nicht zur Anfrage.", seq)
            return
        try:
            self._api.exchange_code(code)
            if seq != self._login_seq:  # read only: a logout meanwhile - ask nothing more
                self._core.executor.submit(self._logged_in, None, seq)
                return
            user = self._api.get_user()  # here, not on the core thread: no network there
        except SpotifyError as exc:
            self._core.executor.submit(self._login_failed, exc, seq)
            return
        self._core.executor.submit(self._logged_in, user, seq)

    def _logged_in(self, user: dict | None, seq: int | None = None) -> None:  # core
        from .protocol import SpotifyAuthChanged
        if seq is not None and seq != self._login_seq:
            # cancelled meanwhile (logout): SpotifyApi stored the refresh token during the
            # exchange on the worker - it must not survive unless a newer login holds it
            # (connected, or still running: its exchange may have stored its own token)
            if not self.connected and self._pending is None:
                self.tokens.clear()
            return
        self._close_listener()
        self._pending = None
        self._stop_loading("login")
        self._remember_user(user or {})
        self._core.emit(SpotifyAuthChanged(True, self.user["name"]))
        self._core.notice(f"Mit Spotify verbunden: {self.user['name']}", "info")
        self._core.state_changed()
        self.load_library()
        if not self._needs_reconnect():  # a fresh login always grants both new scopes
            self.load_recent()

    def _login_failed(self, error, seq: int | None = None) -> None:  # core
        """End the login. `error` is the SpotifyError that caused it, or a plain text.

        The raw cause is important here: a failed sign-in is the one case the user cannot
        reproduce on demand, so "unable to get local issuer certificate" or Spotify's
        "invalid_client" must reach the log instead of only "the login expired".
        """
        if seq is not None and seq != self._login_seq:
            return  # a login the user already left (logout, or a newer login)
        self._close_listener()
        self._pending = None
        self._stop_loading("login")
        detail = getattr(error, "detail", "")
        if detail:
            log.info("spotify login failed: %s", detail)
        text = error.text if isinstance(error, SpotifyError) else str(error)
        if not self.connected:
            # the exchange may have stored a refresh token before a later step failed;
            # without a connection none may survive (a restart would log in with it)
            self.tokens.clear()
        self._fail("login", AuthError(text))

    def finish_login(self, refresh_token: str, user: dict, scope: str = "") -> None:
        """Programmatic hook (tests): mark connected without the browser round trip.

        `scope` defaults to every scope in `SCOPES` (a real OAuth login always grants what
        it asked for) - tests that need an old token missing the new ones (spec §14.1)
        pass a narrower string.
        """
        self._close_listener()
        self._pending = None
        self.tokens.save({"refresh_token": refresh_token, "scope": scope or " ".join(SCOPES)})
        self._remember_user(user)
        self.connected = True
        self._core.state_changed()

    def _remember_user(self, user: dict) -> None:
        images = user.get("images") or []
        avatar = images[0].get("url") if images and isinstance(images[0], dict) else None
        self.user = {"name": str(user.get("display_name") or ""), "avatar_url": avatar}
        self.connected = True
        # a fresh login makes every old section error moot ("login expired" above all);
        # the sections reload anyway
        self.errors = {}
        self._recompute_error()

    def _end_session(self) -> None:
        """Drop the account's data and cut every section's generation (logout and an
        expired login alike). A request started before (e.g. the auto `load_library()`
        after a login) is still running on a worker and does not know the session ended -
        its token must stop matching, or its answer (success or failure) would write
        library/search/playlist/error back after the user is already gone."""
        self.tokens.clear()
        self.connected = False
        self.user = None
        self.search = None
        self.library = None
        self.playlist = None
        self.recent = None
        self.playlist_plays = {}
        self.loading = [s for s in self.loading if s == "login"]  # a running login goes on
        for scope in _REQUEST_SCOPES:
            self._request_seq[scope] = self._request_seq.get(scope, 0) + 1

    def logout(self, _cmd=None) -> None:
        from .protocol import SpotifyAuthChanged
        self._close_listener()
        self._end_session()
        self.loading = []
        self.errors = {}
        self.error = None
        self._login_seq += 1  # a login still waiting or exchanging is dropped too
        self._core.emit(SpotifyAuthChanged(False))
        self._core.notice("Von Spotify getrennt.", "info")
        self._core.state_changed()

    # ---- queries ----

    def search_command(self, cmd) -> None:
        from .protocol import SpotifySearchMore
        query = cmd.query.strip()
        if not query:
            return  # the interface debounces; an empty search sends nothing
        kind = cmd.search_type if cmd.search_type in SEARCH_KINDS else DEFAULT_KIND
        if kind == "all" and isinstance(cmd, SpotifySearchMore):
            return  # "Mehr laden" does not exist for the "Alle" groups view (spec §14.2)
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        token = self._start_request("search")
        self._core.state_changed()
        self._core.workers.submit(self._run_search, query, kind, offset, token)

    def _run_search(self, query: str, kind: str, offset: int, token: int) -> None:  # worker
        if kind == "all":
            # One call for all three types (spec §14.2); no `offset` - the groups view has
            # no "Mehr laden", each group's "Alle zeigen" switches to the single-type chip.
            params = {"q": query, "type": ",".join(_ALL_SEARCH_TYPES), "limit": SEARCH_LIMIT}
            self._network(lambda: self._api.get("/search", params),
                          lambda payload: self._searched_all(query, token, payload),
                          "search", token)
            return
        params = {"q": query, "type": kind, "limit": SEARCH_LIMIT, "offset": offset}
        self._network(lambda: self._api.get("/search", params),
                      lambda payload: self._searched(query, kind, offset, token, payload),
                      "search", token)

    def _searched(self, query: str, kind: str, offset: int, token: int, payload: dict) -> None:  # core
        if not self._current("search", token):
            return  # a stale answer (an older search) is discarded (R1)
        items, has_more = search_items(payload, kind)
        previous = self.search
        if previous and previous["query"] == query and previous["kind"] == kind and offset > 0:
            items = previous["items"] + items  # "mehr laden" appends to what is shown
        self.search = {"query": query, "kind": kind, "offset": offset + SEARCH_LIMIT,
                       "has_more": has_more, "items": items, "groups": None}
        self._done("search")

    def _searched_all(self, query: str, token: int, payload: dict) -> None:  # core
        if not self._current("search", token):
            return  # a stale answer (an older search) is discarded (R1)
        groups = {kind: search_items(payload, kind)[0] for kind in _ALL_SEARCH_TYPES}
        self.search = {"query": query, "kind": "all", "offset": 0, "has_more": False,
                       "items": [], "groups": groups}
        self._done("search")

    def load_library(self, _cmd=None) -> None:
        if not self._ready():
            return
        token = self._start_request("library")
        self._core.state_changed()
        self._core.workers.submit(self._run_library, token)

    def _run_library(self, token: int) -> None:  # worker
        def fetch() -> dict:
            return {"tracks": self._api.get("/me/tracks", {"limit": 50}),
                    "playlists": self._api.get("/me/playlists", {"limit": 50}),
                    "albums": self._api.get("/me/albums", {"limit": 50})}

        self._network(fetch, lambda payload: self._library_loaded(token, payload),
                      "library", token)

    def _library_loaded(self, token: int, payload: dict) -> None:  # core
        if not self._current("library", token):
            return
        tracks, playlists, albums = payload["tracks"], payload["playlists"], payload["albums"]
        self.library = {
            "tracks": [map_track(entry["track"]) for entry in tracks.get("items") or []
                       if isinstance(entry.get("track"), dict)],
            "playlists": [map_playlist(entry) for entry in playlists.get("items") or []
                          if isinstance(entry, dict)],
            "albums": [map_album(entry["album"]) for entry in albums.get("items") or []
                       if isinstance(entry.get("album"), dict)],
            "tracks_has_more": bool(tracks.get("next")),
            "playlists_has_more": bool(playlists.get("next")),
            "albums_has_more": bool(albums.get("next")),
        }
        self._done("library")

    def load_library_more(self, cmd) -> None:
        """The next page of one library section ("mehr laden", spec §14.3).

        Muster `_playlist_loaded`: the page is appended to what is already shown. An
        unknown `section` is discarded, and so is a request before the library was ever
        loaded (nothing to append to) - neither reaches the network.
        """
        section = cmd.section
        if section not in _LIBRARY_SECTIONS:
            return
        if self.library is None:
            return
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        token = self._start_request("library")
        self._core.state_changed()
        self._core.workers.submit(self._run_library_more, section, offset, token)

    def _run_library_more(self, section: str, offset: int, token: int) -> None:  # worker
        path, _map_entry = _LIBRARY_SECTIONS[section]
        self._network(lambda: self._api.get(path, {"limit": 50, "offset": offset}),
                      lambda payload: self._library_more_loaded(section, token, payload),
                      "library", token)

    def _library_more_loaded(self, section: str, token: int, payload: dict) -> None:  # core
        if not self._current("library", token):
            return  # a stale answer (an older page for this or another section) is discarded (R1)
        if self.library is None:
            return  # the library was cleared (e.g. logout) while this was in flight
        _path, map_entry = _LIBRARY_SECTIONS[section]
        items = []
        for entry in payload.get("items") or []:
            item = map_entry(entry)
            if item is not None:
                items.append(item)
        self.library = dict(self.library)
        self.library[section] = self.library[section] + items
        self.library[f"{section}_has_more"] = bool(payload.get("next"))
        self._done("library")

    def load_recent(self, _cmd=None) -> None:
        """`SpotifyLoadRecent` (spec §14.7): loaded together with the library, both after
        login and whenever the interface asks to load the library again. Skipped without a
        network call while `needs_reconnect` stands (spec §14.1) - a hint, not an error."""
        if not self._ready():
            return
        if self._needs_reconnect():
            return
        token = self._start_request("recent")
        self._core.state_changed()
        self._core.workers.submit(self._run_recent, token)

    def _run_recent(self, token: int) -> None:  # worker
        self._network(lambda: self._api.get("/me/player/recently-played", {"limit": 50}),
                      lambda payload: self._recent_loaded(token, payload),
                      "recent", token)

    def _recent_loaded(self, token: int, payload: dict) -> None:  # core
        if not self._current("recent", token):
            return  # a stale answer (an older recent load) is discarded (R1)
        items: list[dict] = []
        plays: dict[str, int] = {}
        for entry in payload.get("items") or []:
            context = entry.get("context") if isinstance(entry, dict) else None
            # albums count too, under their own uri (hand check 2026-10-03b: albums sort by plays)
            if isinstance(context, dict) and context.get("type") in ("playlist", "album"):
                # counted over every one of the (up to 50) entries, *before* the directly-
                # consecutive-repeat collapse below - a played-three-times-in-a-row track
                # must still count as three plays of its playlist (Jan, 2026-09-29).
                uri = str(context.get("uri") or "")
                if uri:
                    plays[uri] = plays.get(uri, 0) + 1
            track = entry.get("track") if isinstance(entry, dict) else None
            if not isinstance(track, dict):
                continue
            mapped = map_track(track)
            if items and mapped["id"] and items[-1]["id"] == mapped["id"]:
                continue  # only *directly* consecutive repeats collapse (spec §14.7)
            items.append(mapped)
        self.recent = items
        self.playlist_plays = plays
        self._done("recent")

    def _needs_reconnect(self) -> bool:
        """Spec §14.1 (E1): a stored token whose `scope` does not name both new scopes is
        a hint to reconnect once, not an error - Task 8 ("Like") reads this too."""
        if not self.connected:
            return False
        stored = self.tokens.load()
        return stored is not None and not has_scopes(stored.get("scope") or "")

    def load_playlist(self, cmd) -> None:
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        token = self._start_request("playlist")
        self._core.state_changed()
        self._core.workers.submit(self._run_playlist, cmd.playlist_id, offset, token)

    def _run_playlist(self, playlist_id: str, offset: int, token: int) -> None:  # worker
        def fetch() -> dict:
            # Since February 2026 Spotify answers a foreign playlist's meta and/or items
            # with a plain 403 (spec §14.4) - caught here, per endpoint, so one Forbidden
            # cannot hide whichever of the two still answered. `meta: None` and an empty
            # page are the "no access" signal `_playlist_loaded` (core thread) reads below;
            # any other error (network, 5xx, 429, 401) still propagates to `_network`.
            try:
                meta = self._api.get(f"/playlists/{playlist_id}", None)
            except Forbidden as exc:
                log.info("spotify playlist without access: %s", exc.detail)
                meta = None
            try:
                page = self._api.get(f"/playlists/{playlist_id}/items",
                                     {"limit": PAGE, "offset": offset})
            except Forbidden as exc:
                log.info("spotify playlist without access: %s", exc.detail)
                page = {"items": [], "next": None}
            return {"meta": meta, "page": page, "offset": offset, "playlist_id": playlist_id}

        self._network(fetch, lambda payload: self._playlist_loaded(token, payload),
                      "playlist", token)

    def _playlist_loaded(self, token: int, payload: dict) -> None:  # core
        if not self._current("playlist", token):
            return  # a stale answer (playlist A after B was opened) is discarded (R1)
        meta, page, offset = payload["meta"], payload["page"], payload["offset"]
        playlist_id = payload["playlist_id"]
        items = []
        for entry in page.get("items") or []:
            track = list_entry(entry) if isinstance(entry, dict) else None
            if track is not None:
                items.append(map_track(track))
        if meta is None:
            # 403 on /playlists/{id} itself (spec §14.4): the library, not the network, is
            # the only source left for this playlist's name/cover/track_count - read here,
            # on the core thread, never on the worker above.
            found = None
            if self.library:
                found = next((pl for pl in self.library["playlists"] if pl["id"] == playlist_id),
                             None)
            mapped = dict(found) if found is not None else {
                "id": playlist_id, "uri": f"spotify:playlist:{playlist_id}", "name": "",
                "cover_url": None, "track_count": 0,
            }
        else:
            mapped = map_playlist(meta)
        previous = self.playlist
        if previous and previous["id"] == mapped["id"] and offset > 0:
            items = previous["items"] + items
        self.playlist = {"kind": "playlist", "id": mapped["id"], "name": mapped["name"],
                         "uri": mapped["uri"], "cover_url": mapped["cover_url"],
                         "track_count": mapped["track_count"],
                         "offset": offset + PAGE, "has_more": bool(page.get("next")),
                         "items": items}
        self._done("playlist")

    # ---- album detail (E5 = a, live 2026-09-29: /albums/{id}/tracks answers) ----

    def load_album(self, cmd) -> None:
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        # one open detail at a time: the album shares the playlist's slot and counter (R1)
        token = self._start_request("playlist")
        self._core.state_changed()
        self._core.workers.submit(self._run_album, cmd.album_id, offset, token)

    def _run_album(self, album_id: str, offset: int, token: int) -> None:  # worker
        def fetch() -> dict:
            meta = self._api.get(f"/albums/{album_id}", None)
            page = self._api.get(f"/albums/{album_id}/tracks", {"limit": PAGE, "offset": offset})
            return {"meta": meta, "page": page, "offset": offset}

        self._network(fetch, lambda payload: self._album_loaded(token, payload), "playlist", token)

    def _album_loaded(self, token: int, payload: dict) -> None:  # core
        if not self._current("playlist", token):
            return  # a stale answer (another detail opened meanwhile) is discarded (R1)
        meta, page, offset = payload["meta"], payload["page"], payload["offset"]
        album = map_album(meta)
        items = []
        for entry in page.get("items") or []:
            if isinstance(entry, dict) and entry.get("uri"):
                track = map_track(entry)
                # album tracks come without `album`: the album itself names and covers them
                track["album"] = track["album"] or album["name"]
                track["cover_url"] = track["cover_url"] or album["cover_url"]
                items.append(track)
        previous = self.playlist
        if previous and previous.get("kind") == "album" and previous["id"] == album["id"] and offset > 0:
            items = previous["items"] + items
        self.playlist = {"kind": "album", "id": album["id"], "name": album["name"],
                         "uri": album["uri"], "cover_url": album["cover_url"],
                         "track_count": int(meta.get("total_tracks") or len(items)),
                         "offset": offset + PAGE, "has_more": bool(page.get("next")),
                         "items": items}
        self._done("playlist")

    # ---- plumbing ----

    def _ready(self) -> bool:
        if not self.client_id:
            self._fail(None, NotConfigured())
            return False
        if not self.connected:
            self._fail(None, NotConnected())
            return False
        return True

    def _start_loading(self, scope: str) -> None:
        if scope not in self.loading:
            self.loading.append(scope)

    def _stop_loading(self, scope: str) -> None:
        if scope in self.loading:
            self.loading.remove(scope)

    def _start_request(self, scope: str) -> int:
        """Bump the section's request counter and mark it loading; returns the new token.

        R1: the token travels with the worker call and its answer. `_current` below
        tells whether that answer still belongs to the newest request for the section -
        an older one (in flight when a newer one started) is discarded either way,
        success or failure, once it arrives.
        """
        token = self._request_seq.get(scope, 0) + 1
        self._request_seq[scope] = token
        self._start_loading(scope)
        return token

    def _current(self, scope: str, token: int) -> bool:
        return self._request_seq.get(scope) == token

    def _network(self, work, done, scope: str, token: int) -> None:  # worker
        """Run `work` here; hand the payload or the error back to the core thread."""
        try:
            payload = work()
        except SpotifyError as exc:
            self._core.executor.submit(self._failed, scope, token, exc)
            return
        except Exception as exc:  # noqa: BLE001 - an unexpected error must not leave busy true
            log.exception("spotify request failed unexpectedly")
            self._core.executor.submit(self._failed, scope, token, NetworkError(str(exc)))
            return
        self._core.executor.submit(done, payload)

    def _done(self, scope: str) -> None:  # core
        self._stop_loading(scope)
        self.errors.pop(scope, None)  # a success clears only its own section's error (R2)
        self._recompute_error()
        self._last_notice = None
        self._core.state_changed()

    def _failed(self, scope: str | None, token: int | None, error: SpotifyError) -> None:  # core
        if scope is not None:
            if not self._current(scope, token):
                return  # a stale failure (an older request) is discarded too (R1)
            self._stop_loading(scope)
        if isinstance(error, AuthError):
            self._end_session()  # like logout, but the errors stay readable
        if error.detail:  # the raw cause never reaches the interface, only the log
            log.info("spotify request failed: %s", error.detail)
        self._fail(scope, error)

    def _recompute_error(self) -> None:
        """The derived `error` (old readers): the latest of the remaining section errors,
        or None once `errors` is empty. Kept simple on purpose (eng review R2) - an
        unscoped error (e.g. "not connected") is overwritten the same way by the next
        `_fail`, scoped or not."""
        self.error = next(reversed(self.errors.values()), None)

    def _fail(self, scope: str | None, error: SpotifyError) -> None:
        from .protocol import SpotifyError as SpotifyErrorEvent
        if scope is not None:
            # pop before set: re-failing a section must move it to the end of `errors`,
            # so `_recompute_error` (last value wins) picks the most recently set text.
            self.errors.pop(scope, None)
            self.errors[scope] = error.text
        self.error = error.text
        if error.text != self._last_notice:  # do not repeat the same line on every try
            self._last_notice = error.text
            self._core.notice(error.text, "error")
        self._core.emit(SpotifyErrorEvent(error.text))
        self._core.state_changed()

    def _close_listener(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self._pending = None
