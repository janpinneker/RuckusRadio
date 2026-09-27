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

    def _next(self, key):
        value = self._answers.get(key, (404, {"error": "not found"}))
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def __call__(self, request, timeout=None):
        import io
        import urllib.error
        key = (request.get_method(), request.full_url.split("?")[0])
        self.requests.append((key[0], key[1], request.data, dict(request.header_items())))
        answer = self._next(key)
        status, body = answer[0], answer[1]
        headers = answer[2] if len(answer) > 2 else {}
        if status >= 400:
            payload = json.dumps(body).encode("utf-8") if body else b""
            raise urllib.error.HTTPError(key[1], status, "err", headers, io.BytesIO(payload))
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
    test_api_reports_rate_limit_and_bad_status()
    test_api_keeps_the_spotify_reason_for_the_log()
    test_service_without_client_id_reports_it()
    test_service_search_needs_a_connection()
    test_service_searches_and_keeps_the_token_out_of_the_state()
    test_service_loads_a_playlist_from_the_renamed_items_endpoint()
    test_service_loads_the_library_and_reports_a_network_error()
    test_a_failed_login_reports_its_own_reason()
    test_service_expired_login_clears_the_token()
    test_the_callback_is_a_route_of_the_local_server()
    test_the_login_refuses_without_the_local_server_on_the_right_port()
    print("\nALL SPOTIFY LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
