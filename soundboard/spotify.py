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
SCOPES = ("user-read-private", "user-library-read",
          "playlist-read-private", "playlist-read-collaborative", "user-follow-read",
          "user-read-playback-state", "user-modify-playback-state")
SEARCH_KINDS = ("track", "album", "playlist", "artist")
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


def map_album(item: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "uri": str(item.get("uri") or ""),
        "name": str(item.get("name") or ""),
        "artist": _first_artist(item),
        "cover_url": _image(item),
    }


_MAPPERS = {"track": map_track, "album": map_album, "playlist": map_playlist}
_GROUPS = {"track": "tracks", "album": "albums", "playlist": "playlists", "artist": "artists"}


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
                raise ApiError(REFUSED, detail=detail)
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
        answer = self._token_request({"grant_type": "refresh_token",
                                      "refresh_token": stored["refresh_token"]})
        self._remember(answer)

    def _remember(self, answer: dict) -> None:
        access = answer.get("access_token")
        if access:
            self._access = str(access)
            expires = max(30.0, float(answer.get("expires_in") or 3600))
            self._expires_at = time.monotonic() + expires - 60.0
        refresh = answer.get("refresh_token")
        if refresh:
            self._tokens.save({"refresh_token": str(refresh), "scope": answer.get("scope", "")})

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
        from .protocol import (SpotifyLoadLibrary, SpotifyLoadPlaylist, SpotifyLogin,
                               SpotifyLogout, SpotifySearch, SpotifySearchMore)
        self._core = core
        self.tokens = token_store if token_store is not None else TokenStore(core.store)
        self.client_id = ""
        self._api = api
        self._pending: tuple[str, str] | None = None
        self._server = None  # the local server, via attach_server: it owns /callback
        self._listener: _CallbackListener | None = None
        self._last_notice: str | None = None
        self.connected = False
        self.user: dict | None = None
        self.busy = False
        self.error: str | None = None
        self.search: dict | None = None
        self.library: dict | None = None
        self.playlist: dict | None = None
        core.handle(SpotifyLogin, self.login)
        core.handle(SpotifyLogout, self.logout)
        core.handle(SpotifySearch, self.search_command)
        core.handle(SpotifySearchMore, self.search_command)
        core.handle(SpotifyLoadLibrary, self.load_library)
        core.handle(SpotifyLoadPlaylist, self.load_playlist)
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
        self._failed(error)

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
                "user": self.user, "busy": self.busy, "error": self.error,
                "search": self.search, "library": self.library, "playlist": self.playlist}

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
            self._fail(SpotifyError("Die Spotify-Anmeldung braucht den lokalen Server - "
                                    "starte die Web-Oberfläche (Standard) oder --serve."))
            return None
        try:
            wanted = int(self._core.store.data.get("server_port") or 0)
        except (TypeError, ValueError):
            wanted = 0
        port = int(getattr(self._server, "port", 0) or 0)
        if wanted and port != wanted:
            self._fail(SpotifyError(
                f"Die Oberfläche läuft auf Port {port}, die Spotify-Weiterleitung zeigt auf "
                f"{wanted}. Schließ das Programm, das {wanted} belegt, und starte neu."))
            return None
        return redirect_uri(port)

    def login(self, _cmd=None) -> None:
        if not self.client_id:
            self._fail(NotConfigured())
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
            self._fail(SpotifyError("Es läuft schon eine Spotify-Anmeldung."))
            log.warning("spotify callback route busy: %s", exc)
            return
        import webbrowser
        webbrowser.open(authorize_url(self.client_id, challenge_of(verifier), state, uri))
        self.busy = True
        self.error = None
        self._core.state_changed()
        self._core.workers.submit(self._await_callback)

    def _await_callback(self) -> None:  # worker
        assert self._listener is not None
        try:
            code, state = self._listener.wait(LOGIN_TIMEOUT_S)
        except TimeoutError:
            self._core.executor.submit(self._login_failed,
                                       "Zeitüberschreitung. Verbinde dich erneut.")
            return
        except SpotifyError as exc:
            self._core.executor.submit(self._login_failed, exc)
            return
        except Exception as exc:  # noqa: BLE001 - never leave the login stuck
            log.exception("spotify callback wait failed")
            self._core.executor.submit(self._login_failed, str(exc))
            return
        if self._pending is None or state != self._pending[1]:
            self._core.executor.submit(self._login_failed, "Die Antwort passte nicht zur Anfrage.")
            return
        try:
            answer = self._api.exchange_code(code)
        except SpotifyError as exc:
            self._core.executor.submit(self._login_failed, exc)
            return
        self._core.executor.submit(self._logged_in, answer)

    def _logged_in(self, answer: dict) -> None:  # core
        from .protocol import SpotifyAuthChanged
        self._close_listener()
        self._pending = None
        self.busy = False
        try:
            self._remember_user(self._api.get_user())
        except SpotifyError as exc:
            self._login_failed(exc)
            return
        self._core.emit(SpotifyAuthChanged(True, self.user["name"]))
        self._core.notice(f"Mit Spotify verbunden: {self.user['name']}", "info")
        self._core.state_changed()
        self.load_library()

    def _login_failed(self, error) -> None:  # core
        """End the login. `error` is the SpotifyError that caused it, or a plain text.

        The raw cause is important here: a failed sign-in is the one case the user cannot
        reproduce on demand, so "unable to get local issuer certificate" or Spotify's
        "invalid_client" must reach the log instead of only "the login expired".
        """
        self._close_listener()
        self._pending = None
        self.busy = False
        detail = getattr(error, "detail", "")
        if detail:
            log.info("spotify login failed: %s", detail)
        text = error.text if isinstance(error, SpotifyError) else str(error)
        self._fail(AuthError(text))

    def finish_login(self, refresh_token: str, user: dict) -> None:
        """Programmatic hook (tests): mark connected without the browser round trip."""
        self._close_listener()
        self._pending = None
        self.tokens.save({"refresh_token": refresh_token})
        self._remember_user(user)
        self.connected = True
        self._core.state_changed()

    def _remember_user(self, user: dict) -> None:
        images = user.get("images") or []
        avatar = images[0].get("url") if images and isinstance(images[0], dict) else None
        self.user = {"name": str(user.get("display_name") or ""), "avatar_url": avatar}
        self.connected = True
        self.error = None

    def logout(self, _cmd=None) -> None:
        from .protocol import SpotifyAuthChanged
        self._close_listener()
        self.tokens.clear()
        self.connected = False
        self.user = None
        self.search = None
        self.library = None
        self.playlist = None
        self.busy = False
        self.error = None
        self._core.emit(SpotifyAuthChanged(False))
        self._core.notice("Von Spotify getrennt.", "info")
        self._core.state_changed()

    # ---- queries ----

    def search_command(self, cmd) -> None:
        query = cmd.query.strip()
        if not query:
            return  # the interface debounces; an empty search sends nothing
        kind = cmd.search_type if cmd.search_type in SEARCH_KINDS else DEFAULT_KIND
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        self.busy = True
        self.error = None
        self._core.state_changed()
        self._core.workers.submit(self._run_search, query, kind, offset)

    def _run_search(self, query: str, kind: str, offset: int) -> None:  # worker
        params = {"q": query, "type": kind, "limit": SEARCH_LIMIT, "offset": offset}
        self._network(lambda: self._api.get("/search", params),
                      lambda payload: self._searched(query, kind, offset, payload))

    def _searched(self, query: str, kind: str, offset: int, payload: dict) -> None:  # core
        items, has_more = search_items(payload, kind)
        previous = self.search
        if previous and previous["query"] == query and previous["kind"] == kind and offset > 0:
            items = previous["items"] + items  # "mehr laden" appends to what is shown
        self.search = {"query": query, "kind": kind, "offset": offset + SEARCH_LIMIT,
                       "has_more": has_more, "items": items}
        self._done()

    def load_library(self, _cmd=None) -> None:
        if not self._ready():
            return
        self.busy = True
        self.error = None
        self._core.state_changed()
        self._core.workers.submit(self._run_library)

    def _run_library(self) -> None:  # worker
        def fetch() -> dict:
            return {"tracks": self._api.get("/me/tracks", {"limit": 50}),
                    "playlists": self._api.get("/me/playlists", {"limit": 50}),
                    "albums": self._api.get("/me/albums", {"limit": 50})}

        self._network(fetch, self._library_loaded)

    def _library_loaded(self, payload: dict) -> None:  # core
        tracks, playlists, albums = payload["tracks"], payload["playlists"], payload["albums"]
        self.library = {
            "tracks": [map_track(entry["track"]) for entry in tracks.get("items") or []
                       if isinstance(entry.get("track"), dict)],
            "playlists": [map_playlist(entry) for entry in playlists.get("items") or []
                          if isinstance(entry, dict)],
            "albums": [map_album(entry["album"]) for entry in albums.get("items") or []
                       if isinstance(entry.get("album"), dict)],
            "albums_has_more": bool(albums.get("next")),
        }
        self._done()

    def load_playlist(self, cmd) -> None:
        if not self._ready():
            return
        offset = max(0, int(cmd.offset))
        self.busy = True
        self.error = None
        self._core.state_changed()
        self._core.workers.submit(self._run_playlist, cmd.playlist_id, offset)

    def _run_playlist(self, playlist_id: str, offset: int) -> None:  # worker
        def fetch() -> dict:
            return {"meta": self._api.get(f"/playlists/{playlist_id}", None),
                    "page": self._api.get(f"/playlists/{playlist_id}/items",
                                          {"limit": PAGE, "offset": offset}),
                    "offset": offset}

        self._network(fetch, self._playlist_loaded)

    def _playlist_loaded(self, payload: dict) -> None:  # core
        meta, page, offset = payload["meta"], payload["page"], payload["offset"]
        items = []
        for entry in page.get("items") or []:
            track = list_entry(entry) if isinstance(entry, dict) else None
            if track is not None:
                items.append(map_track(track))
        previous = self.playlist
        if previous and previous["id"] == str(meta.get("id") or "") and offset > 0:
            items = previous["items"] + items
        self.playlist = {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or ""),
                         "offset": offset + PAGE, "has_more": bool(page.get("next")),
                         "items": items}
        self._done()

    # ---- plumbing ----

    def _ready(self) -> bool:
        if not self.client_id:
            self._fail(NotConfigured())
            return False
        if not self.connected:
            self._fail(NotConnected())
            return False
        return True

    def _network(self, work, done) -> None:  # worker
        """Run `work` here; hand the payload or the error back to the core thread."""
        try:
            payload = work()
        except SpotifyError as exc:
            self._core.executor.submit(self._failed, exc)
            return
        except Exception as exc:  # noqa: BLE001 - an unexpected error must not leave busy true
            log.exception("spotify request failed unexpectedly")
            self._core.executor.submit(self._failed, NetworkError(str(exc)))
            return
        self._core.executor.submit(done, payload)

    def _done(self) -> None:  # core
        self.busy = False
        self.error = None
        self._last_notice = None
        self._core.state_changed()

    def _failed(self, error: SpotifyError) -> None:  # core
        if isinstance(error, AuthError):
            self.connected = False
            self.user = None
            self.tokens.clear()
        if error.detail:  # the raw cause never reaches the interface, only the log
            log.info("spotify request failed: %s", error.detail)
        self._fail(error)

    def _fail(self, error: SpotifyError) -> None:
        from .protocol import SpotifyError as SpotifyErrorEvent
        self.busy = False
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
