"""Spotify im Musik-Tab: reine Helfer, Token-Ablage, HTTP-Client und Dienst gegen Attrappen."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-spotify-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
# Hermetisch wie RUCKUS_DATA_DIR: die Sandbox darf Jans "unzerstoerbare"
# Client-ID (Windows-Benutzervariable RUCKUS_SPOTIFY_CLIENT_ID, hat Vorrang vor
# der Config) nicht sehen - sonst ist "ohne Client-ID" hier nicht pruefbar.
os.environ.pop("RUCKUS_SPOTIFY_CLIENT_ID", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soundboard import protocol as p, spotify  # noqa: E402
import core_fakes  # noqa: E402


def test_pkce():
    verifier = spotify.make_verifier()
    assert len(verifier) == 64, verifier
    assert all(c.isalnum() or c in "-._~" for c in verifier), verifier
    assert spotify.challenge_of(verifier) != verifier
    assert "=" not in spotify.challenge_of(verifier)
    # RFC 7636, Appendix B: fixed verifier -> fixed challenge
    assert spotify.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    print("PKCE verifier and S256 challenge: OK")


def test_authorize_url():
    # The redirect URI is the local server's route on `server_port` (spec §11: no second
    # listener), so it is passed in rather than read from a constant.
    url = spotify.authorize_url("cid123", "CHAL", "STATE", spotify.redirect_uri(47800))
    assert url.startswith(spotify.AUTHORIZE_URL + "?")
    for part in ("client_id=cid123", "response_type=code", "code_challenge=CHAL",
                 "code_challenge_method=S256", "state=STATE",
                 "redirect_uri=http%3A%2F%2F127.0.0.1%3A47800%2Fcallback"):
        assert part in url, part
    assert "user-library-read" in url and "user-modify-playback-state" in url
    assert spotify.redirect_uri(47800) == "http://127.0.0.1:47800/callback"
    print("authorize url carries PKCE, state and the scopes: OK")


def test_callback_result():
    code, state = spotify.callback_result("/callback?code=abc&state=STATE")
    assert (code, state) == ("abc", "STATE")
    code, error = spotify.callback_result("/callback?error=access_denied&state=STATE")
    assert code is None and "access_denied" in error
    code, error = spotify.callback_result("/callback?state=STATE")
    assert code is None and error
    print("callback parses code, error and missing code: OK")


TRACK = {"id": "t1", "uri": "spotify:track:t1", "name": "Neon Drift", "duration_ms": 214000,
         "external_urls": {"spotify": "https://open.spotify.com/track/t1"},
         "artists": [{"name": "Kassette 84"}, {"name": "Gast"}],
         "album": {"name": "Nachtfahrt",
                   "images": [{"url": "https://i.scdn.co/image/big"},
                              {"url": "https://i.scdn.co/image/small"}]}}

# Task 3 (spec §14.2, E2=a): raw Spotify shapes for the "Alle" chip's album/playlist groups.
ALBUM = {"id": "a1", "uri": "spotify:album:a1", "name": "Wege",
         "artists": [{"name": "Mira Holm"}], "images": []}
PLAYLIST = {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
            "owner": {"display_name": "Jan"}, "items": {"total": 12}, "images": []}


def test_mapping():
    track = spotify.map_track(TRACK)
    assert track == {"id": "t1", "uri": "spotify:track:t1", "title": "Neon Drift",
                     "artist": "Kassette 84", "album": "Nachtfahrt", "duration_ms": 214000,
                     "cover_url": "https://i.scdn.co/image/big",
                     "external_url": "https://open.spotify.com/track/t1"}, track
    assert spotify.map_track({**TRACK, "album": {"name": "X", "images": []}})["cover_url"] is None
    assert spotify.map_track({**TRACK, "artists": []})["artist"] == spotify.UNKNOWN_ARTIST

    playlist = spotify.map_playlist({"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
                                     "owner": {"display_name": "Jan"}, "items": {"total": 12},
                                     "images": [{"url": "https://i.scdn.co/image/pl"}]})
    assert playlist == {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus", "owner": "Jan",
                        "track_count": 12, "cover_url": "https://i.scdn.co/image/pl"}, playlist
    # February 2026 renamed the playlist's `tracks` object to `items`; the old name
    # must still count, a reverted answer cannot zero the playlist.
    legacy = spotify.map_playlist({"id": "p2", "name": "Alt", "tracks": {"total": 7}})
    assert legacy["track_count"] == 7, legacy
    assert spotify.map_playlist({"id": "p3", "name": "Leer"})["track_count"] == 0

    # a playlist page entry carries its payload under `item` now, `track` before
    assert spotify.list_entry({"item": TRACK})["id"] == "t1"
    assert spotify.list_entry({"track": TRACK})["id"] == "t1"
    assert spotify.list_entry({"item": None, "track": TRACK})["id"] == "t1", "removed item falls back"
    assert spotify.list_entry({"item": None}) is None
    assert spotify.list_entry({}) is None

    album = spotify.map_album({"id": "a1", "uri": "spotify:album:a1", "name": "Wege",
                               "artists": [{"name": "Mira Holm"}], "images": []})
    assert album == {"id": "a1", "uri": "spotify:album:a1", "name": "Wege",
                     "artist": "Mira Holm", "cover_url": None}, album

    items, has_more = spotify.search_items({"tracks": {"items": [TRACK], "next": None}}, "track")
    assert has_more is False and items[0]["title"] == "Neon Drift"
    items, has_more = spotify.search_items({"tracks": {"items": [TRACK], "next": "https://x"}},
                                           "track")
    assert has_more is True and len(items) == 1
    assert spotify.search_items({}, "album") == ([], False)
    assert spotify.search_items({"tracks": {"items": [TRACK]}}, "artist") == ([], False)
    print("mapping turns Spotify answers into the UI shapes: OK")


def test_error_texts_keep_the_specific_message():
    # `_fail` shows `error.text`. Without the override in SpotifyError.__init__ a busy
    # port, a callback timeout and an expired login would all read the same, i.e. the
    # interface would name the wrong cause.
    assert spotify.SpotifyError("Port 8899 ist belegt.").text == "Port 8899 ist belegt."
    assert spotify.SpotifyError().text == spotify.SpotifyError.text, "a bare error keeps its text"
    technical = spotify.NetworkError(detail="[Errno 11001] getaddrinfo failed")
    assert technical.text == spotify.NetworkError.text, "a raw cause stays out of the text"
    assert technical.detail == "[Errno 11001] getaddrinfo failed", technical.detail
    rate = spotify.RateLimitError(7.0)
    assert (rate.retry_after, rate.text) == (7.0, spotify.RateLimitError.text)
    print("a specific error text wins over the class default: OK")


class _SecretBox:
    """Stands in for Store's secret API: the same three calls, but no disk."""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.secrets: dict[str, str] = {}

    def secret(self, name):
        return self.secrets.get(name)

    def set_secret(self, name, value):
        self.secrets[name] = value

    def forget_secret(self, name):
        self.secrets.pop(name, None)


def test_token_store_uses_the_shared_secrets_store():
    box = _SecretBox(_TMP)
    store = spotify.TokenStore(box)
    store.migrate_legacy()  # nothing to take over: the file does not exist
    assert store.load() is None
    store.save({"refresh_token": "r1", "scope": "user-library-read"})
    assert store.load() == {"refresh_token": "r1", "scope": "user-library-read"}, store.load()
    # exactly one entry, so a leaked file is easy to audit and delete
    assert list(box.secrets) == [spotify.SECRET_NAME], box.secrets
    assert "config" not in json.dumps(box.secrets).lower()
    store.save({})  # nothing to remember -> nothing written
    assert "" not in box.secrets.values()
    store.clear()
    assert store.load() is None and box.secrets == {}

    # an unreadable entry must read as "not connected", not crash the tab
    box.set_secret(spotify.SECRET_NAME, "{not json")
    assert store.load() is None
    box.set_secret(spotify.SECRET_NAME, json.dumps({"scope": "x"}))  # no refresh token
    assert store.load() is None
    print("the token lives in the shared secrets store: OK")


def test_token_store_takes_over_the_old_file_once():
    legacy = Path(_TMP) / spotify.TOKEN_FILE
    store = spotify.TokenStore(_SecretBox(_TMP), legacy_dir=_TMP)
    legacy.write_text(json.dumps({"refresh_token": "old-r1", "scope": "x"}), encoding="utf-8")
    store.migrate_legacy()
    assert store.load()["refresh_token"] == "old-r1", "the old token is taken over"
    assert not legacy.exists(), "the old file goes away: one place for the secret"
    store.migrate_legacy()  # a second run must not undo anything
    assert store.load()["refresh_token"] == "old-r1"

    # An unreadable file is left where it is: it may be the only copy of a live login,
    # and throwing it away would cost the user a new sign-in.
    legacy.write_text("{not json", encoding="utf-8")
    fresh = spotify.TokenStore(_SecretBox(_TMP), legacy_dir=_TMP)
    fresh.migrate_legacy()
    assert fresh.load() is None and legacy.exists()
    legacy.write_text(json.dumps({"scope": "x"}), encoding="utf-8")  # no refresh token
    fresh.migrate_legacy()
    assert fresh.load() is None and legacy.exists()
    legacy.unlink()

    assert spotify.TokenStore(object()).legacy_path is None, "no data dir -> no takeover"
    print("the old token file is taken over once, a broken one is left alone: OK")


class _Reply:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeHttp:
    """Stands in for urllib: fixed answers per (method, url); records every request.

    A value may be one answer `(status, body[, headers])` or a list of them, consumed
    in order (the last one repeats) - that is how a 401-then-200 retry is tested.
    """

    def __init__(self, answers):
        self._answers = answers
        self.requests = []
        self.urls = []

    def _next(self, key):
        value = self._answers.get(key, (404, {"error": "not found"}))
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def __call__(self, request, timeout=None):
        import io
        import urllib.error
        key = (request.get_method(), request.full_url.split("?")[0])
        self.urls.append(request.full_url)
        self.requests.append((key[0], key[1], request.data, dict(request.header_items())))
        answer = self._next(key)
        status, body = answer[0], answer[1]
        headers = answer[2] if len(answer) > 2 else {}
        if status >= 400:
            payload = json.dumps(body).encode("utf-8") if body else b""
            raise urllib.error.HTTPError(key[1], status, "err", headers, io.BytesIO(payload))
        if status == 204:
            return _Reply(b"")
        if isinstance(body, bytes):  # a raw, non-JSON answer (Spotify's player does that)
            return _Reply(body)
        return _Reply(json.dumps(body).encode("utf-8"))


def _api_with(answers):
    from soundboard import spotify as sp
    tokens = sp.TokenStore(_SecretBox(_TMP))
    tokens.clear()
    http = _FakeHttp(answers)
    return sp.SpotifyApi("cid", tokens, http=http), tokens, http


def test_api_exchange_and_authorized_get():
    api, tokens, http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"refresh_token": "r1", "access_token": "a1",
                                            "expires_in": 3600, "scope": "x"}),
        ("GET", spotify.API_BASE + "/me"): (200, {"display_name": "Jan"}),
    })
    api.begin_login("VER", "STATE")
    answer = api.exchange_code("CODE")
    assert answer["refresh_token"] == "r1"
    body = http.requests[0][2].decode("ascii")
    assert "grant_type=authorization_code" in body and "code_verifier=VER" in body
    assert "client_id=cid" in body
    assert tokens.load()["refresh_token"] == "r1", "the refresh token is persisted"

    assert api.get_user()["display_name"] == "Jan"
    method, _url, _data, headers = http.requests[1]
    assert headers.get("Authorization") == "Bearer a1", headers
    print("api exchanges the code and calls with the auth header: OK")


def test_api_refreshes_once_after_a_401():
    api, tokens, http = _api_with({
        ("POST", spotify.TOKEN_URL): [
            (200, {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600, "scope": "x"}),
            (200, {"access_token": "a2", "expires_in": 3600}),
        ],
        ("GET", spotify.API_BASE + "/me/tracks"): [
            (401, {"error": {"status": 401}}),
            (200, {"items": [], "next": None}),
        ],
    })
    api.begin_login("VER", "STATE")
    api.exchange_code("CODE")
    assert api.get("/me/tracks") == {"items": [], "next": None}
    assert http.requests[-1][3].get("Authorization") == "Bearer a2", http.requests[-1]
    refresh_calls = [r for r in http.requests if r[1] == spotify.TOKEN_URL]
    assert len(refresh_calls) == 2, http.requests  # exchange + exactly one refresh
    print("a 401 refreshes exactly once and retries: OK")


def test_refresh_without_scope_keeps_the_stored_scope():
    # R4 (eng review): a refresh answer can carry a new refresh_token without repeating
    # `scope` (Spotify does this) - `_remember` must leave the previously stored scope
    # alone then, or a perfectly valid token would look like it needs reconnecting.
    full_scope = "user-library-read user-library-modify user-read-recently-played"
    api, tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): [
            (200, {"refresh_token": "r0", "access_token": "a0", "expires_in": 3600,
                   "scope": full_scope}),
            (200, {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600}),
        ],
    })
    api.begin_login("VER", "STATE")
    api.exchange_code("CODE")
    assert tokens.load()["scope"] == full_scope
    api.refresh()
    stored = tokens.load()
    assert stored["refresh_token"] == "r1", stored
    assert stored["scope"] == full_scope, "a refresh answer without `scope` must not erase it (R4)"
    print("a refresh answer without scope keeps the previously stored scope (R4): OK")


def test_api_reports_rate_limit_and_bad_status():
    api, _tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"refresh_token": "r1", "access_token": "a1",
                                            "expires_in": 3600, "scope": "x"}),
        ("GET", spotify.API_BASE + "/me"): (429, {}, {"Retry-After": "1"}),
    })
    api.begin_login("VER", "STATE")
    api.exchange_code("CODE")
    try:
        api.get_user()
    except spotify.RateLimitError as exc:
        assert exc.retry_after == 1.0
    else:
        raise AssertionError("a 429 must raise RateLimitError")

    api2, _t, _h = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"refresh_token": "r1", "access_token": "a1",
                                            "expires_in": 3600, "scope": "x"}),
        ("GET", spotify.API_BASE + "/me"): (503, {}),
    })
    api2.begin_login("VER", "STATE")
    api2.exchange_code("CODE")
    try:
        api2.get_user()
    except spotify.NetworkError:
        pass
    else:
        raise AssertionError("a 5xx must read as a network problem")
    print("429 and 5xx map to their error classes: OK")


def test_api_keeps_the_spotify_reason_for_the_log():
    api, _tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"refresh_token": "r1", "access_token": "a1",
                                            "expires_in": 3600, "scope": "x"}),
        ("GET", spotify.API_BASE + "/me"): (400, {"error": "invalid_client"}),
    })
    api.begin_login("VER", "STATE")
    api.exchange_code("CODE")
    try:
        api.get_user()
    except spotify.ApiError as exc:
        # Spotify's own reason must survive for the log, but must not reach the interface.
        assert "invalid_client" in exc.detail, exc.detail
        # hand check 2026-09-29: a bare "HTTP 403" did not say which call failed
        assert "/v1/me" in exc.detail, exc.detail
        assert exc.text == "Spotify hat die Anfrage abgelehnt (400).", exc.text
    else:
        raise AssertionError("a 400 must raise ApiError")
    print("Spotify's reason travels in detail, not in the user text: OK")


class _FakeServer:
    """Stands in for webserver.SseServer: the route table and the port, nothing else."""

    def __init__(self, port=47800):
        self.port = port
        self.routes: dict[str, object] = {}

    def add_route(self, path, handler):
        if path in self.routes:
            raise ValueError(f"route already registered: {path}")
        self.routes[path] = handler

    def remove_route(self, path):
        self.routes.pop(path, None)


def test_the_callback_is_a_route_of_the_local_server():
    server = _FakeServer()
    listener = spotify._CallbackListener(server, "STATE")
    assert spotify.REDIRECT_PATH in server.routes

    class _Handler:
        path = "/callback?code=abc&state=STATE"

    # the registered callable is the route contract: (handler, query, body) -> reply
    status, content_type, body = server.routes[spotify.REDIRECT_PATH](_Handler(), {}, "")
    assert (status, content_type) == (200, "text/html; charset=utf-8")
    assert body == spotify.CALLBACK_PAGE
    assert listener.wait(1.0) == ("abc", "STATE")
    listener.close()
    assert server.routes == {}, "a one-shot route is dropped after the login"
    # a rejected login reaches the waiter as its own reason, not as "abgebrochen"
    again = spotify._CallbackListener(server, "STATE")

    class _Denied:
        path = "/callback?error=access_denied&state=STATE"

    server.routes[spotify.REDIRECT_PATH](_Denied(), {}, "")
    try:
        again.wait(1.0)
    except spotify.SpotifyError as exc:
        assert "access_denied" in exc.text, exc.text
    else:
        raise AssertionError("a denied login must raise")
    again.close()
    print("the callback is a one-shot route of the local server: OK")


def test_the_login_refuses_without_the_local_server_on_the_right_port():
    c, events = _core_with_spotify(core_fakes.FakeSpotifyApi())
    # no server handed over: the Tk-only window cannot host the return leg
    assert c.spotify._callback_url() is None
    error = c.state()["spotify"]["error"]
    assert "lokalen Server" in error, error
    assert any("lokalen Server" in n.text for n in core_fakes.of_type(events, p.Notice)), events

    # the dashboard knows one URL: a server on a fallback port would only fail at Spotify
    c.spotify.attach_server(_FakeServer(port=47999))
    assert c.spotify._callback_url() is None
    error = c.state()["spotify"]["error"]
    assert "47999" in error and "47800" in error, error

    c.spotify.attach_server(_FakeServer(port=47800))
    assert c.spotify._callback_url() == "http://127.0.0.1:47800/callback"
    print("the login names a missing or wrong-port server instead of a Spotify mismatch: OK")


def _core_with_spotify(api, client_id="cid"):
    c, events = core_fakes.make_core(spotify_api=api)
    c.store.data["spotify_client_id"] = client_id
    c.spotify.tokens.clear()  # earlier tests share the temp dir; start from "not connected"
    c.start()
    return c, events


def test_service_without_client_id_reports_it():
    c, events = _core_with_spotify(core_fakes.FakeSpotifyApi(), client_id="")
    state = c.state()["spotify"]
    assert state["configured"] is False and state["connected"] is False
    c.send(p.SpotifyLogin())
    assert any("Client-ID" in n.text for n in core_fakes.of_type(events, p.Notice))
    assert core_fakes.of_type(events, p.SpotifyAuthChanged) == []
    print("without a client id nothing is opened and the UI is told: OK")


def test_service_search_needs_a_connection():
    api = core_fakes.FakeSpotifyApi()
    c, events = _core_with_spotify(api)
    assert c.state()["spotify"]["configured"] is True
    c.send(p.SpotifySearch("neon"))
    assert c.state()["spotify"]["connected"] is False
    assert api.calls == [], "no network call without a connection"
    messages = [e.message for e in core_fakes.of_type(events, p.SpotifyError)]
    assert any("Nicht mit Spotify verbunden" in m for m in messages), messages
    assert c.state()["spotify"]["error"] == spotify.NotConnected.text
    print("a search without a connection is refused with a clear error: OK")


def test_service_searches_and_keeps_the_token_out_of_the_state():
    api = core_fakes.FakeSpotifyApi(search=[TRACK], has_more=True)
    c, events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan", "images": []})
    assert c.spotify.tokens.load()["refresh_token"] == "r1"
    c.send(p.SpotifySearch("neon"))
    state = c.state()["spotify"]
    assert state["connected"] is True and state["user"]["name"] == "Jan"
    assert state["search"]["query"] == "neon" and state["search"]["has_more"] is True
    assert state["search"]["items"][0]["title"] == "Neon Drift"
    assert state["search"]["offset"] == spotify.SEARCH_LIMIT
    search_call = [c for c in api.calls if c[0] == "get" and c[1] == "/search"][-1]
    assert search_call[2]["limit"] == spotify.SEARCH_LIMIT, search_call
    assert spotify.SEARCH_LIMIT <= 10, "Spotify capped /search at 10 per request in February 2026"
    assert state["busy"] is False and state["error"] is None
    assert "token" not in json.dumps(state).lower(), state
    assert core_fakes.of_type(events, p.SpotifyAuthChanged) == []  # finish_login is quiet

    c.send(p.SpotifySearchMore("neon", "track", spotify.SEARCH_LIMIT))
    assert len(c.state()["spotify"]["search"]["items"]) == 2, "\"mehr laden\" appends"

    c.send(p.SpotifyLogout())
    state = c.state()["spotify"]
    assert state["connected"] is False and state["search"] is None
    assert c.spotify.tokens.load() is None, "logout removes the token"
    assert [e.connected for e in core_fakes.of_type(events, p.SpotifyAuthChanged)] == [False]
    print("search fills the state, mehr laden appends, logout clears the token: OK")


def test_service_searches_all_kinds_at_once_in_groups():
    # Spec §14.2 (E2=a): the "Alle" chip fetches all three types in one call and the core
    # maps them into `search.groups`; the groups view has no "Mehr laden" (`has_more` False).
    api = core_fakes.FakeSpotifyApi(search=[TRACK], search_albums=[ALBUM], search_playlists=[PLAYLIST],
                                     has_more=True)
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifySearch("neon", "all"))
    state = c.state()["spotify"]
    search_calls = [call for call in api.calls if call[0] == "get" and call[1] == "/search"]
    assert len(search_calls) == 1, "one call for all three types, not three"
    assert search_calls[0][2] == {"q": "neon", "type": "track,album,playlist",
                                  "limit": spotify.SEARCH_LIMIT}, search_calls[0]
    groups = state["search"]["groups"]
    assert groups["track"][0]["title"] == "Neon Drift"
    assert groups["album"][0]["name"] == "Wege"
    assert groups["playlist"][0]["name"] == "Fokus"
    assert state["search"]["kind"] == "all"
    assert state["search"]["has_more"] is False, 'the groups view has no "Mehr laden" (§14.2)'

    # SpotifySearchMore for "all" is discarded outright (no network call, no state change)
    before = state["search"]
    c.send(p.SpotifySearchMore("neon", "all", spotify.SEARCH_LIMIT))
    assert len(api.calls) == 1, 'SpotifySearchMore("all") must not reach the network'
    assert c.state()["spotify"]["search"] == before
    print("search \"all\" fetches once and groups the three types; SearchMore(\"all\") is discarded: OK")


def test_service_loads_a_playlist_from_the_renamed_items_endpoint():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], has_more=True)
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadPlaylist("p1"))
    playlist = c.state()["spotify"]["playlist"]
    assert playlist["id"] == "p1" and playlist["name"] == "Fokus"
    assert playlist["items"][0]["title"] == "Neon Drift"
    assert playlist["has_more"] is True and playlist["offset"] == spotify.PAGE
    paths = [call[1] for call in api.calls if call[0] == "get"]
    assert "/playlists/p1/items" in paths, paths
    assert "/playlists/p1/tracks" not in paths, "Spotify removed the /tracks path in February 2026"
    print("a playlist loads through /playlists/{id}/items and reads `item`: OK")


def test_service_loads_a_playlist_with_uri_cover_and_track_count():
    # Task 5 (spec §14.4): the detail page's header needs a play button (`uri`), a cover
    # and the track count - not just id/name/items as before.
    meta = {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
            "images": [{"url": "https://i.scdn.co/image/pl"}], "items": {"total": 42}}
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], playlist_meta=meta)
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadPlaylist("p1"))
    playlist = c.state()["spotify"]["playlist"]
    assert playlist["uri"] == "spotify:playlist:p1", playlist
    assert playlist["cover_url"] == "https://i.scdn.co/image/pl", playlist
    assert playlist["track_count"] == 42, playlist
    print("spotify.playlist carries uri, cover_url and track_count: OK")


def test_service_playlist_403_on_items_is_no_access_not_an_error():
    # Live 2026-09-29 (Jan): since February 2026 Spotify answers a foreign playlist's
    # `/items` with a plain 403 (spec §14.4) - that is "kein Zugriff", not an error.
    meta = {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
            "images": [{"url": "https://i.scdn.co/image/pl"}], "items": {"total": 42}}
    api = core_fakes.FakeSpotifyApi(playlist_meta=meta, forbid_playlist={"items"})
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadPlaylist("p1"))
    state = c.state()["spotify"]
    playlist = state["playlist"]
    assert playlist["items"] == [] and playlist["has_more"] is False, playlist
    assert playlist["name"] == "Fokus" and playlist["track_count"] == 42, playlist
    assert state["errors"] == {}, "a 403 on items alone must not become an error"
    print("a 403 on a foreign playlist's items is the empty state, meta still shows: OK")


def test_service_playlist_403_on_meta_falls_back_to_the_library():
    # A 403 on /playlists/{id} itself (meta): the header still needs a name/cover/count,
    # read from the already-loaded library entry for the same id (spec §14.4).
    library_entry = {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
                     "owner": {"display_name": "Jan"}, "items": {"total": 7},
                     "images": [{"url": "https://i.scdn.co/image/pl"}]}
    api = core_fakes.FakeSpotifyApi(playlists=[library_entry], forbid_playlist={"meta"})
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    c.send(p.SpotifyLoadPlaylist("p1"))
    playlist = c.state()["spotify"]["playlist"]
    assert playlist["name"] == "Fokus", playlist
    assert playlist["cover_url"] == "https://i.scdn.co/image/pl", playlist
    assert playlist["track_count"] == 7, playlist
    assert c.state()["spotify"]["errors"] == {}
    print("a 403 on meta falls back to the library's own copy of that playlist: OK")


def test_service_playlist_403_on_both_meta_and_items_without_a_library_match():
    # Neither endpoint answers, and this playlist id is not in the (already loaded, empty)
    # library either: a minimal meta so the header still has something to show.
    api = core_fakes.FakeSpotifyApi(forbid_playlist={"meta", "items"})
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    c.send(p.SpotifyLoadPlaylist("p9"))
    state = c.state()["spotify"]
    playlist = state["playlist"]
    assert playlist == {"kind": "playlist", "id": "p9", "uri": "spotify:playlist:p9", "name": "",
                        "cover_url": None, "track_count": 0, "offset": spotify.PAGE,
                        "has_more": False, "items": []}, playlist
    assert state["errors"] == {}
    print("both endpoints forbidden and no library match still yields the empty state, no error: OK")


def test_service_loads_the_library_and_reports_a_network_error():
    playlist = {"id": "p1", "uri": "spotify:playlist:p1", "name": "Fokus",
                "owner": {"display_name": "Jan"}, "items": {"total": 3}, "images": []}
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], playlists=[playlist])
    c, events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    library = c.state()["spotify"]["library"]
    assert library["tracks"][0]["title"] == "Neon Drift"
    assert library["playlists"][0]["name"] == "Fokus"
    assert library["albums"] == [] and library["albums_has_more"] is False

    api.fail = spotify.NetworkError("down")
    c.send(p.SpotifyLoadLibrary())
    state = c.state()["spotify"]
    assert state["error"] == "down" and state["busy"] is False, state["error"]
    assert any(e.message == "down" for e in core_fakes.of_type(events, p.SpotifyError))
    print("library loads; a network error lands in the state and as an event: OK")


# Task 4 (spec §14.3, E6=a): "Mehr laden" for the library's tracks/playlists sections.
TRACK2 = {**TRACK, "id": "t2", "uri": "spotify:track:t2", "name": "Zweite Seite"}


def test_service_library_has_more_flags_come_from_next():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], playlists=[{"id": "p1", "name": "Fokus"}],
                                    tracks_has_more=True, playlists_has_more=True)
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    library = c.state()["spotify"]["library"]
    assert library["tracks_has_more"] is True, library
    assert library["playlists_has_more"] is True, library
    print("library.tracks_has_more/playlists_has_more come from Spotify's `next`: OK")


def test_service_loads_more_library_pages_and_appends():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], tracks_has_more=True, tracks_page2=[TRACK2])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    assert c.state()["spotify"]["library"]["tracks_has_more"] is True

    c.send(p.SpotifyLoadLibraryMore("tracks", 50))
    calls = [call for call in api.calls if call[0] == "get" and call[1] == "/me/tracks"]
    assert calls[-1][2] == {"limit": 50, "offset": 50}, calls
    library = c.state()["spotify"]["library"]
    assert [t["title"] for t in library["tracks"]] == ["Neon Drift", "Zweite Seite"], library["tracks"]
    assert library["tracks_has_more"] is False, "the second page's `next` is None"
    print("SpotifyLoadLibraryMore appends the next page (muster _playlist_loaded): OK")


def test_service_discards_an_unknown_library_section():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    api.calls.clear()
    c.send(p.SpotifyLoadLibraryMore("artists", 50))  # never a library section
    assert api.calls == [], "an unknown section never reaches the network"
    assert c.state()["spotify"]["loading"] == [], "an unknown section does not start loading"
    print("SpotifyLoadLibraryMore discards an unknown section: OK")


# Task 9 (spec §14.1/§14.3, E5=b): "Mehr laden" for the library's albums section too.
ALBUM2 = {**ALBUM, "id": "a2", "uri": "spotify:album:a2", "name": "Zweiter Weg"}


def test_service_library_albums_has_more_comes_from_next():
    api = core_fakes.FakeSpotifyApi(albums=[ALBUM], albums_has_more=True)
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    library = c.state()["spotify"]["library"]
    assert library["albums"] == [spotify.map_album(ALBUM)], library["albums"]
    assert library["albums_has_more"] is True, library
    print("library.albums/albums_has_more come from /me/albums and its `next`: OK")


def test_service_loads_more_album_pages_and_appends():
    api = core_fakes.FakeSpotifyApi(albums=[ALBUM], albums_has_more=True, albums_page2=[ALBUM2])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    assert c.state()["spotify"]["library"]["albums_has_more"] is True

    c.send(p.SpotifyLoadLibraryMore("albums", 50))
    calls = [call for call in api.calls if call[0] == "get" and call[1] == "/me/albums"]
    assert calls[-1][2] == {"limit": 50, "offset": 50}, calls
    library = c.state()["spotify"]["library"]
    assert [a["name"] for a in library["albums"]] == ["Wege", "Zweiter Weg"], library["albums"]
    assert library["albums_has_more"] is False, "the second page's `next` is None"
    print("SpotifyLoadLibraryMore appends the next album page: OK")


def test_service_discards_library_more_before_the_library_was_ever_loaded():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    assert c.state()["spotify"]["library"] is None
    c.send(p.SpotifyLoadLibraryMore("tracks", 50))
    assert api.calls == [], "nothing to append to before the library ever loaded"
    assert c.state()["spotify"]["loading"] == []
    print("SpotifyLoadLibraryMore before the library ever loaded is discarded: OK")


def test_service_clamps_a_negative_library_more_offset():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], tracks_has_more=True, tracks_page2=[TRACK2])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    c.send(p.SpotifyLoadLibraryMore("tracks", -5))
    calls = [call for call in api.calls if call[0] == "get" and call[1] == "/me/tracks"]
    assert calls[-1][2]["offset"] == 0, calls[-1]
    print("a negative offset is clamped to 0: OK")


# Task 7 (spec §14.1/§14.7, E1=a): the two new scopes and "Zuletzt gespielt".

def test_scopes_carry_the_two_new_ones():
    assert "user-library-modify" in spotify.SCOPES
    assert "user-read-recently-played" in spotify.SCOPES
    print("SCOPES names both new scopes (E1=a): OK")


def test_an_old_token_without_the_new_scopes_needs_reconnect_not_an_error():
    api = core_fakes.FakeSpotifyApi(recent=[TRACK])
    c, _events = _core_with_spotify(api)
    # a token from before E1 - it never named the new scopes
    c.spotify.finish_login("r1", {"display_name": "Jan"},
                           scope="user-read-private user-library-read")
    state = c.state()["spotify"]
    assert state["connected"] is True
    assert state["needs_reconnect"] is True
    assert state["errors"] == {}, "a hint, not an error (E1)"
    assert state["error"] is None

    c.send(p.SpotifyLoadRecent())
    assert [call for call in api.calls if call[1] == "/me/player/recently-played"] == [], \
        "Recent is not attempted while needs_reconnect stands"
    assert c.state()["spotify"]["recent"] is None
    assert c.state()["spotify"]["loading"] == []
    print("a stored token missing the new scopes is a hint, not an error; Recent is skipped: OK")


def test_a_token_with_both_new_scopes_needs_no_reconnect():
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})  # default scope: all of SCOPES
    assert c.state()["spotify"]["needs_reconnect"] is False
    print("a token naming both new scopes needs no reconnect: OK")


def test_service_loads_recent_and_collapses_consecutive_repeats():
    # spec §14.7: GET /me/player/recently-played?limit=50, mapped like other tracks; only
    # *directly* consecutive repeats of the same track collapse.
    api = core_fakes.FakeSpotifyApi(recent=[TRACK, TRACK, TRACK2, TRACK])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadRecent())
    recent = c.state()["spotify"]["recent"]
    assert [t["id"] for t in recent] == ["t1", "t2", "t1"], recent
    call = next(call for call in api.calls
                if call[0] == "get" and call[1] == "/me/player/recently-played")
    assert call[2] == {"limit": 50}, call
    print("recently-played is mapped and only directly consecutive repeats collapse: OK")


def test_service_recent_counts_playlist_plays_from_context_before_collapsing():
    # Task 2 (2026-09-29): Spotify has no per-playlist play count, so `recently-played`'s
    # `context` is counted as an approximation - over every entry (before the directly-
    # consecutive-repeat collapse), a non-playlist/missing context simply not counted.
    api = core_fakes.FakeSpotifyApi(
        recent=[TRACK, TRACK, TRACK2, TRACK],
        recent_contexts=[
            {"type": "playlist", "uri": "spotify:playlist:p1"},
            {"type": "playlist", "uri": "spotify:playlist:p1"},  # same track&context, still 2
            {"type": "artist", "uri": "spotify:artist:x"},       # not a playlist: not counted
            None,                                                 # no context: not counted
        ],
    )
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadRecent())
    state = c.state()["spotify"]
    assert state["playlist_plays"] == {"spotify:playlist:p1": 2}, state["playlist_plays"]
    assert [t["id"] for t in state["recent"]] == ["t1", "t2", "t1"], state["recent"]
    print("playlist_plays counts recently-played's context before the repeat collapse: OK")


def test_service_tracks_loading_and_errors_per_section():
    # R1/R2: `loading`/`errors` are per section, `busy`/`error` stay as derived fields.
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], search=[TRACK])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    workers = core_fakes.QueuedWorkers()
    c.workers = workers

    c.send(p.SpotifySearch("neon"))
    c.send(p.SpotifyLoadLibrary())
    state = c.state()["spotify"]
    assert set(state["loading"]) == {"search", "library"}, state["loading"]
    assert state["busy"] is True, "busy == len(loading) > 0"

    # library finishes; search stays in flight (two sections load in parallel)
    lib_job = next(j for j in workers.jobs if j[0] == c.spotify._run_library)
    workers.jobs.remove(lib_job)
    lib_job[0](*lib_job[1])
    state = c.state()["spotify"]
    assert state["loading"] == ["search"], state["loading"]
    assert state["busy"] is True and state["errors"] == {}

    # search fails: the error lands only under its own section
    api.fail = spotify.NetworkError("down")
    search_job = workers.jobs.pop(0)
    search_job[0](*search_job[1])
    state = c.state()["spotify"]
    assert state["loading"] == [], state["loading"]
    assert state["busy"] is False
    assert state["errors"] == {"search": "down"}, state["errors"]
    assert state["error"] == "down", "the derived `error` is the last text, for old readers"

    # R2: a fresh success in another section must not clear search's error
    api.fail = None
    c.send(p.SpotifyLoadLibrary())
    workers.run_all()
    state = c.state()["spotify"]
    assert state["errors"] == {"search": "down"}, "a library success does not touch search's error"
    print("loading/errors are tracked per section; a success clears only its own error: OK")


def test_service_discards_a_stale_answer_of_the_same_section():
    # R1: `_network(fetch, done, scope)` keeps a per-section counter; an older request's
    # answer (success or failure) is discarded once a newer one for the same section exists.
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    workers = core_fakes.QueuedWorkers()
    c.workers = workers

    # playlist A -> B before A answers; A arrives last -> state stays B
    c.send(p.SpotifyLoadPlaylist("A"))
    c.send(p.SpotifyLoadPlaylist("B"))
    assert len(workers.jobs) == 2
    job_a, job_b = workers.jobs
    workers.jobs = []
    job_b[0](*job_b[1])
    assert c.state()["spotify"]["playlist"]["id"] == "B"
    job_a[0](*job_a[1])
    assert c.state()["spotify"]["playlist"]["id"] == "B", "the stale A answer must not win"

    # search: "Alle" (track) -> a kind chip (album) before the first answers
    c.send(p.SpotifySearch("neon", "track"))
    c.send(p.SpotifySearch("neon", "album"))
    older, newer = workers.jobs
    workers.jobs = []
    newer[0](*newer[1])
    assert c.state()["spotify"]["search"]["kind"] == "album"
    older[0](*older[1])
    assert c.state()["spotify"]["search"]["kind"] == "album", "the stale chip answer must not win"

    # an old failure arriving after a newer success must change nothing
    c.send(p.SpotifyLoadPlaylist("X"))  # will answer late, and badly
    c.send(p.SpotifyLoadPlaylist("Y"))  # answers first, successfully
    job_x, job_y = workers.jobs
    workers.jobs = []
    job_y[0](*job_y[1])
    assert c.state()["spotify"]["playlist"]["id"] == "Y"
    api.fail = spotify.NetworkError("late failure")
    job_x[0](*job_x[1])
    state = c.state()["spotify"]
    assert state["playlist"]["id"] == "Y", "a stale failure must not replace the newer success"
    assert state["errors"] == {}, "a stale failure must not add an error either"
    assert state["error"] is None
    print("a stale answer of the same section (success or failure) is discarded: OK")


def test_service_logout_clears_loading_errors_and_recent():
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], search=[TRACK])
    c, events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    workers = core_fakes.QueuedWorkers()
    c.workers = workers

    c.send(p.SpotifyLoadLibrary())  # left in flight
    api.fail = spotify.NetworkError("down")
    c.send(p.SpotifySearch("neon"))
    search_job = next(j for j in workers.jobs if j[0] == c.spotify._run_search)
    workers.jobs.remove(search_job)
    search_job[0](*search_job[1])
    state = c.state()["spotify"]
    assert state["loading"] == ["library"], state["loading"]
    assert state["errors"] == {"search": "down"}, state["errors"]
    c.spotify.recent = []  # Task 7's real shape (SpotifyTrack[]); logout below must clear it
    c.spotify.playlist_plays = {"spotify:playlist:p1": 3}  # cleared like `recent` (2026-09-29)

    c.send(p.SpotifyLogout())
    state = c.state()["spotify"]
    assert state["loading"] == [] and state["errors"] == {}, state
    assert state["recent"] is None
    assert state["playlist_plays"] == {}, state["playlist_plays"]

    # Fix round 1: the library request that was still queued at logout time (e.g. the
    # auto load_library() after a login) must not write anything back once it finally
    # runs - neither a success nor (separately) a failure.
    api.fail = None
    notices = len(core_fakes.of_type(events, p.Notice))
    workers.run_all()  # the stale library job answers successfully, too late
    state = c.state()["spotify"]
    assert state["library"] is None, "a stale success after logout must not write library back"
    assert state["loading"] == [] and state["errors"] == {}, state
    assert len(core_fakes.of_type(events, p.Notice)) == notices, "no notice for a discarded answer"

    c.spotify.finish_login("r2", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    c.send(p.SpotifyLogout())
    api.fail = spotify.NetworkError("late failure")
    notices = len(core_fakes.of_type(events, p.Notice))
    workers.run_all()  # the stale library job answers with an error, too late
    state = c.state()["spotify"]
    assert state["library"] is None
    assert state["loading"] == [] and state["errors"] == {}, state
    assert len(core_fakes.of_type(events, p.Notice)) == notices, "no notice for a discarded failure"
    print("logout clears loading/errors/recent and stops any in-flight request cold: OK")


def test_service_error_follows_the_most_recently_set_section():
    # Fix round 1: `_fail` must pop the section before re-setting it, so re-failing a
    # section moves it to the end of `errors` - the derived `error` follows it, not
    # whichever section merely happened to fail first.
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    workers = core_fakes.QueuedWorkers()
    c.workers = workers

    api.fail = spotify.NetworkError("search down")
    c.send(p.SpotifySearch("neon"))
    workers.run_all()
    api.fail = spotify.NetworkError("library down")
    c.send(p.SpotifyLoadLibrary())
    workers.run_all()
    assert c.state()["spotify"]["error"] == "library down"

    api.fail = spotify.NetworkError("search down again")
    c.send(p.SpotifySearch("neon"))
    workers.run_all()
    state = c.state()["spotify"]
    assert state["error"] == "search down again", state["error"]
    # dict == ignores order: check the order itself, then let a third section's success
    # recompute `error` - only the pop-before-set keeps "search" last.
    assert list(state["errors"]) == ["library", "search"], list(state["errors"])
    api.fail = None
    c.send(p.SpotifyLoadRecent())
    workers.run_all()
    state = c.state()["spotify"]
    assert state["error"] == "search down again", state["error"]
    print("the derived error follows the most recently (re-)set section: OK")


def test_login_does_not_clobber_a_scoped_error():
    # Fix round 1: `login()` used to set `self.error = None` directly, which could disagree
    # with `errors` (a scoped error still standing). It must go through `_recompute_error`.
    import webbrowser
    original_open = webbrowser.open
    webbrowser.open = lambda *a, **k: None
    try:
        api = core_fakes.FakeSpotifyApi()
        c, _events = _core_with_spotify(api)
        c.spotify.finish_login("r1", {"display_name": "Jan"})
        workers = core_fakes.QueuedWorkers()
        c.workers = workers
        c.spotify.attach_server(_FakeServer(port=47800))

        api.fail = spotify.NetworkError("library down")
        c.send(p.SpotifyLoadLibrary())
        workers.run_all()
        assert c.state()["spotify"]["error"] == "library down"

        c.send(p.SpotifyLogin())
        state = c.state()["spotify"]
        assert state["error"] == "library down", \
            "login must not silently clear a scoped section's error"
        assert state["errors"] == {"library": "library down"}, state["errors"]
    finally:
        webbrowser.open = original_open
    print("login does not clobber a scoped section's error: OK")


def _start_login(c):
    """Opens a login with the browser muted; the callback wait stays queued on `workers`."""
    import webbrowser
    original_open = webbrowser.open
    webbrowser.open = lambda *a, **k: None
    try:
        c.send(p.SpotifyLogin())
    finally:
        webbrowser.open = original_open


def _deliver_callback(c, code="c1"):
    listener = c.spotify._listener
    listener.code, listener.state = code, listener.expected_state
    listener.done.set()


def test_logout_during_the_callback_wait_shows_no_login_error():
    # Parallel finding on 52ef42f: logout closes the listener, the waiting worker wakes up
    # with "abgebrochen" and used to report it as a login failure after the user left.
    api = core_fakes.FakeSpotifyApi()
    c, events = _core_with_spotify(api)
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.spotify.attach_server(_FakeServer(port=47800))
    _start_login(c)
    c.send(p.SpotifyLogout())
    before = len(core_fakes.of_type(events, p.SpotifyError))
    workers.run_all()
    state = c.state()["spotify"]
    assert state["error"] is None and state["errors"] == {}, state
    assert len(core_fakes.of_type(events, p.SpotifyError)) == before, "no error after logout"
    assert state["connected"] is False and state["busy"] is False
    print("a logout during the callback wait shows no login error: OK")


def test_logout_during_the_code_exchange_does_not_log_back_in():
    # Parallel finding on 52ef42f: the worker already had the code and was exchanging it
    # (the real SpotifyApi stores the refresh token right there) when the user logged out.
    api = core_fakes.FakeSpotifyApi()
    c, events = _core_with_spotify(api)
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.spotify.attach_server(_FakeServer(port=47800))
    _start_login(c)
    real_exchange = api.exchange_code

    def exchange_with_logout(code):
        c.send(p.SpotifyLogout())  # the user clicks "Abmelden" mid-exchange
        answer = real_exchange(code)
        c.spotify.tokens.save({"refresh_token": answer["refresh_token"], "scope": answer["scope"]})
        return answer

    api.exchange_code = exchange_with_logout
    _deliver_callback(c)
    workers.run_all()
    state = c.state()["spotify"]
    assert state["connected"] is False and state["user"] is None, state
    assert c.spotify.tokens.load() is None, "the token of a cancelled login must not survive"
    assert not [e for e in core_fakes.of_type(events, p.SpotifyAuthChanged) if e.connected]
    assert ("get_user",) not in api.calls, "a cancelled login asks Spotify nothing more"
    print("a logout during the code exchange does not log back in: OK")


def test_a_new_login_clears_the_old_login_expired_errors():
    # Parallel finding on 52ef42f: `_remember_user` set `error = None` directly while
    # `errors` still held the section's "login expired" - the section kept showing it.
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    api.fail = spotify.AuthError()
    c.send(p.SpotifyLoadLibrary())
    assert "library" in c.state()["spotify"]["errors"]
    api.fail = None
    c.spotify.finish_login("r2", {"display_name": "Jan"})
    state = c.state()["spotify"]
    assert state["errors"] == {} and state["error"] is None, state
    print("a new login clears the old login-expired errors: OK")


def test_a_failed_login_reports_its_own_reason():
    api = core_fakes.FakeSpotifyApi()
    c, events = _core_with_spotify(api)
    c.spotify._login_failed("Zeitüberschreitung. Verbinde dich erneut.")
    state = c.state()["spotify"]
    assert state["error"] == "Zeitüberschreitung. Verbinde dich erneut.", state["error"]
    assert state["busy"] is False and state["connected"] is False
    assert any(e.message == state["error"] for e in core_fakes.of_type(events, p.SpotifyError))

    # An exception carries its own text into the interface, its raw cause into the log.
    c.spotify._login_failed(spotify.NetworkError(detail="urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]"))
    assert c.state()["spotify"]["error"] == spotify.NetworkError.text
    print("a failed login shows its own reason, not \"login expired\": OK")


def test_service_expired_login_clears_the_token():
    api = core_fakes.FakeSpotifyApi(fail=spotify.AuthError())
    c, events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadLibrary())
    state = c.state()["spotify"]
    assert state["connected"] is False and state["user"] is None
    assert c.spotify.tokens.load() is None, "an expired login drops the token"
    assert any("abgelaufen" in e.message for e in core_fakes.of_type(events, p.SpotifyError))
    print("an expired login drops the token and says so: OK")


def test_api_send_puts_json_and_takes_204():
    api, tokens, http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"access_token": "a1", "expires_in": 3600}),
        ("PUT", spotify.API_BASE + "/me/player/play"): (204, None),
        ("PUT", spotify.API_BASE + "/me/player/shuffle"): (204, None),
    })
    tokens.save({"refresh_token": "r1"})
    assert api.send("PUT", "/me/player/play", {"device_id": "d1"},
                    {"uris": ["spotify:track:t1"]}) == {}
    method, url, body, headers = http.requests[-1]
    assert method == "PUT" and json.loads(body) == {"uris": ["spotify:track:t1"]}
    assert headers.get("Content-type") == "application/json", headers
    assert headers.get("Authorization") == "Bearer a1"
    api.send("PUT", "/me/player/shuffle", {"state": True})
    _m, _u, body, _h = http.requests[-1]
    assert body == b"", "PUT without JSON still sends Content-Length 0"
    assert http.urls[-1].endswith("?state=true"), http.urls[-1]
    print("send puts JSON, answers 204 with {} and sends booleans as true/false: OK")


def test_api_send_takes_a_non_json_success_as_done():
    """Live 2026-09-28: PUT /me/player/pause answered 200 with a body that is no JSON;
    the pause had worked, but the client reported "Spotify hat die Anfrage abgelehnt"."""
    api, tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"access_token": "a1", "expires_in": 3600}),
        ("PUT", spotify.API_BASE + "/me/player/pause"): (200, b"ok"),
        ("GET", spotify.API_BASE + "/me/player"): (200, b"not json"),
    })
    tokens.save({"refresh_token": "r1"})
    assert api.send("PUT", "/me/player/pause") == {}
    try:
        api.get("/me/player")
    except spotify.ApiError:
        pass
    else:
        raise AssertionError("a GET still needs JSON - its answer is data")
    print("a player command answered with a non-JSON 200 counts as done: OK")


def test_api_send_names_premium_and_missing_device():
    reason = lambda r: {"error": {"status": 403, "message": "x", "reason": r}}
    api, tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): (200, {"access_token": "a1", "expires_in": 3600}),
        ("PUT", spotify.API_BASE + "/me/player/pause"): (403, reason("PREMIUM_REQUIRED")),
        ("POST", spotify.API_BASE + "/me/player/next"): (404, {"error": {"status": 404, "message": "x", "reason": "NO_ACTIVE_DEVICE"}}),
        ("PUT", spotify.API_BASE + "/me/player/shuffle"): (403, reason("UNKNOWN")),
    })
    tokens.save({"refresh_token": "r1"})
    for path, method, cls, text in (
            ("/me/player/pause", "PUT", spotify.PremiumRequired, "Steuern braucht Spotify Premium."),
            ("/me/player/next", "POST", spotify.NoDevice, "Öffne die Spotify-App auf diesem Rechner."),
            # 403 without a known reason (spec §14.4, live 2026-09-29): `Forbidden`, still an
            # `ApiError` subclass - a caller that only catches `ApiError` sees no change.
            ("/me/player/shuffle", "PUT", spotify.Forbidden, "Spotify erlaubt das gerade nicht.")):
        try:
            api.send(method, path)
        except cls as exc:
            assert type(exc) is cls, type(exc)
            assert exc.text == text, exc.text
        else:
            raise AssertionError(path)
    assert issubclass(spotify.Forbidden, spotify.ApiError)
    print("403/404 reasons become Premium, no device and a plain refusal (Forbidden): OK")




def _login_core():
    api = core_fakes.FakeSpotifyApi(user={"display_name": "Jan"})
    c, events = _core_with_spotify(api)
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.spotify.attach_server(_FakeServer(port=47800))
    return api, c, events, workers


def test_a_failed_login_is_its_own_section_and_a_new_attempt_clears_it():
    # Final review Minor 3: login failures were unscoped - the interface could not tell
    # them from a player error. They live in `errors["login"]` now.
    _api, c, _events, _workers = _login_core()
    c.spotify._login_failed("Zeitüberschreitung. Verbinde dich erneut.")
    state = c.state()["spotify"]
    assert state["errors"] == {"login": "Zeitüberschreitung. Verbinde dich erneut."}, state["errors"]
    assert state["error"] == "Zeitüberschreitung. Verbinde dich erneut."
    _start_login(c)
    state = c.state()["spotify"]
    assert "login" not in state["errors"] and state["error"] is None, state
    assert "login" in state["loading"]
    print("a failed login is its own error section, a new attempt clears it: OK")


def test_the_login_port_refusal_is_a_login_error():
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.attach_server(_FakeServer(port=47801))
    _start_login(c)
    assert "login" in c.state()["spotify"]["errors"], c.state()["spotify"]["errors"]
    print("the login's port refusal lands in errors['login']: OK")


def test_the_user_is_fetched_on_the_worker_not_on_the_core_thread():
    # Pre-existing since F1 (final review): `_logged_in` called `get_user()` on the core
    # thread - no network I/O belongs there (SESSION-CONTEXT §8).
    api, c, _events, workers = _login_core()
    on_core = [False]
    real_submit = c.executor.submit

    def submit(fn, *args, **kwargs):
        def run(*a, **k):
            on_core[0] = True
            try:
                return fn(*a, **k)
            finally:
                on_core[0] = False
        return real_submit(run, *args, **kwargs)

    c.executor.submit = submit
    real_get_user = api.get_user

    def get_user():
        assert not on_core[0], "get_user() ran on the core thread"
        return real_get_user()

    api.get_user = get_user
    _start_login(c)
    _deliver_callback(c)
    workers.run_all()
    c.executor.submit = real_submit
    state = c.state()["spotify"]
    assert state["connected"] is True and state["user"]["name"] == "Jan", state
    print("the user is fetched on the worker, not on the core thread: OK")


def test_a_cancelled_login_does_not_clear_a_newer_logins_token():
    # Final review Minor 8: the stale `_logged_in` cleared the token whenever not
    # connected - also a newer login's token that was stored but not yet confirmed.
    _api, c, _events, _workers = _login_core()
    _start_login(c)
    old_seq = c.spotify._login_seq
    c.send(p.SpotifyLogout())
    _start_login(c)                                   # login B
    c.spotify.tokens.save({"refresh_token": "rB", "scope": "x"})  # B's exchange stored it
    c.spotify._logged_in({"display_name": "Jan"}, old_seq)       # A's late answer
    assert (c.spotify.tokens.load() or {}).get("refresh_token") == "rB", \
        "login B's token survives login A's late answer"
    c.spotify._login_failed("Zeitüberschreitung. Verbinde dich erneut.", c.spotify._login_seq)
    assert c.spotify.tokens.load() is None, "a failed login leaves no token while disconnected"
    print("a cancelled login does not clear a newer login's token: OK")


def test_an_expired_login_is_a_generation_cut_like_logout():
    # Final review Minor 10: an AuthError disconnect left data and in-flight answers alive.
    api = core_fakes.FakeSpotifyApi(tracks=[TRACK], search=[TRACK])
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifySearch("neon"))
    assert c.state()["spotify"]["search"] is not None
    workers = core_fakes.QueuedWorkers()
    c.workers = workers
    c.send(p.SpotifySearch("drift"))                 # still out when the login expires
    api.fail = spotify.AuthError()
    c.send(p.SpotifyLoadLibrary())
    library_job = next(j for j in workers.jobs if j[1] and "library" in str(j))
    workers.jobs.remove(library_job)
    library_job[0](*library_job[1])
    state = c.state()["spotify"]
    assert state["connected"] is False
    assert state["search"] is None and state["library"] is None, state
    assert state["loading"] == [], state["loading"]
    assert state["errors"].get("library"), "the expiry text stays readable"
    api.fail = None
    workers.run_all()                                 # the old search answers late
    assert c.state()["spotify"]["search"] is None, "a late answer after the expiry is discarded"
    print("an expired login is a generation cut like logout: OK")




def test_a_revoked_refresh_token_is_an_expired_login():
    # Hand check 2026-09-29 (Jan revoked the app at spotify.com/account/apps): the token
    # refresh answers 400 invalid_grant "Refresh token revoked". That was an ApiError, so
    # the login never dropped - every request failed and no connect button appeared.
    api, tokens, _http = _api_with({
        ("POST", spotify.TOKEN_URL): (400, {"error": "invalid_grant",
                                            "error_description": "Refresh token revoked"}),
    })
    tokens.save({"refresh_token": "r1", "scope": "x"})
    try:
        api.get("/me/tracks")
    except spotify.AuthError as exc:
        assert "invalid_grant" in exc.detail, exc.detail
    else:
        raise AssertionError("a revoked refresh token must raise AuthError")
    print("a revoked refresh token counts as an expired login: OK")




ALBUM_META = {"id": "a1", "uri": "spotify:album:a1", "name": "Wege", "total_tracks": 12,
              "artists": [{"name": "Mira Holm"}],
              "images": [{"url": "https://i.scdn.co/image/album"}]}
# /albums/{id}/tracks answers simplified tracks: no `album` (and so no cover) inside
ALBUM_TRACK = {"id": "t9", "uri": "spotify:track:t9", "name": "Weit", "duration_ms": 180000,
               "external_urls": {}, "artists": [{"name": "Mira Holm"}]}


def test_service_loads_an_album_with_its_tracks():
    # E5 = a (Jan 2026-09-29, /albums/{id}/tracks answered live): an album gets a detail
    # page like a playlist - one open detail at a time, so it shares the "playlist" slot.
    api = core_fakes.FakeSpotifyApi()
    api.album_meta = ALBUM_META
    api.album_tracks = [ALBUM_TRACK]
    api.has_more = True
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadAlbum("a1"))
    view = c.state()["spotify"]["playlist"]
    assert view["kind"] == "album" and view["id"] == "a1", view
    assert view["name"] == "Wege" and view["uri"] == "spotify:album:a1"
    assert view["cover_url"] == "https://i.scdn.co/image/album" and view["track_count"] == 12
    assert view["has_more"] is True and view["offset"] == spotify.PAGE
    track = view["items"][0]
    assert track["title"] == "Weit" and track["album"] == "Wege", track
    assert track["cover_url"] == "https://i.scdn.co/image/album", "the album's cover stands in"
    call = next(x for x in api.calls if x[0] == "get" and x[1] == "/albums/a1/tracks")
    assert call[2] == {"limit": spotify.PAGE, "offset": 0}, call

    api.has_more = False
    c.send(p.SpotifyLoadAlbum("a1", offset=spotify.PAGE))
    view = c.state()["spotify"]["playlist"]
    assert len(view["items"]) == 2 and view["has_more"] is False, view
    print("an album loads with its tracks into the detail slot: OK")


def test_service_a_playlist_view_says_it_is_a_playlist():
    api = core_fakes.FakeSpotifyApi()
    c, _events = _core_with_spotify(api)
    c.spotify.finish_login("r1", {"display_name": "Jan"})
    c.send(p.SpotifyLoadPlaylist("p1"))
    assert c.state()["spotify"]["playlist"]["kind"] == "playlist"
    print("a playlist view carries kind 'playlist': OK")


def main():
    test_pkce()
    test_authorize_url()
    test_callback_result()
    test_mapping()
    test_error_texts_keep_the_specific_message()
    test_token_store_uses_the_shared_secrets_store()
    test_token_store_takes_over_the_old_file_once()
    test_api_exchange_and_authorized_get()
    test_api_refreshes_once_after_a_401()
    test_refresh_without_scope_keeps_the_stored_scope()
    test_api_reports_rate_limit_and_bad_status()
    test_api_keeps_the_spotify_reason_for_the_log()
    test_api_send_puts_json_and_takes_204()
    test_api_send_names_premium_and_missing_device()
    test_api_send_takes_a_non_json_success_as_done()
    test_service_without_client_id_reports_it()
    test_service_search_needs_a_connection()
    test_service_searches_and_keeps_the_token_out_of_the_state()
    test_service_searches_all_kinds_at_once_in_groups()
    test_service_loads_a_playlist_from_the_renamed_items_endpoint()
    test_service_loads_a_playlist_with_uri_cover_and_track_count()
    test_service_playlist_403_on_items_is_no_access_not_an_error()
    test_service_playlist_403_on_meta_falls_back_to_the_library()
    test_service_playlist_403_on_both_meta_and_items_without_a_library_match()
    test_service_loads_the_library_and_reports_a_network_error()
    test_service_library_has_more_flags_come_from_next()
    test_service_loads_more_library_pages_and_appends()
    test_service_discards_an_unknown_library_section()
    test_service_discards_library_more_before_the_library_was_ever_loaded()
    test_service_clamps_a_negative_library_more_offset()
    test_service_library_albums_has_more_comes_from_next()
    test_service_loads_more_album_pages_and_appends()
    test_scopes_carry_the_two_new_ones()
    test_an_old_token_without_the_new_scopes_needs_reconnect_not_an_error()
    test_a_token_with_both_new_scopes_needs_no_reconnect()
    test_service_loads_recent_and_collapses_consecutive_repeats()
    test_service_recent_counts_playlist_plays_from_context_before_collapsing()
    test_service_tracks_loading_and_errors_per_section()
    test_service_discards_a_stale_answer_of_the_same_section()
    test_service_logout_clears_loading_errors_and_recent()
    test_service_error_follows_the_most_recently_set_section()
    test_login_does_not_clobber_a_scoped_error()
    test_logout_during_the_callback_wait_shows_no_login_error()
    test_logout_during_the_code_exchange_does_not_log_back_in()
    test_a_new_login_clears_the_old_login_expired_errors()
    test_a_failed_login_reports_its_own_reason()
    test_service_expired_login_clears_the_token()
    test_the_callback_is_a_route_of_the_local_server()
    test_the_login_refuses_without_the_local_server_on_the_right_port()
    test_a_failed_login_is_its_own_section_and_a_new_attempt_clears_it()
    test_the_login_port_refusal_is_a_login_error()
    test_the_user_is_fetched_on_the_worker_not_on_the_core_thread()
    test_a_cancelled_login_does_not_clear_a_newer_logins_token()
    test_an_expired_login_is_a_generation_cut_like_logout()
    test_a_revoked_refresh_token_is_an_expired_login()
    test_service_loads_an_album_with_its_tracks()
    test_service_a_playlist_view_says_it_is_a_playlist()
    print("\nALL SPOTIFY LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
