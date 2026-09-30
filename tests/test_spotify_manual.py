r"""Manueller Ende-zu-Ende-Test der Spotify-Anmeldung - echter Browser, echtes Spotify.

    venv\Scripts\python.exe tests\test_spotify_manual.py [--fresh-login] [--player] [--f3]

**``--f3``** prueft die neuen Scopes, "Zuletzt gespielt", Alben (auch ``/albums/{id}/tracks``,
E5-Folgefrage) und schaltet "Gefaellt mir" eines Titels einmal um und zurueck. Nur mit Jan.

**Warum ``--player`` existiert.** Prueft F2 - die Fernbedienung des echten Spotify-
Players (``/me/player*``) - mit echten Befehlen: Geraete lesen, aktueller Titel, ein
echtes Pause/Play und eine echte Lautstaerkeaenderung (danach sofort zurueckgesetzt).
Pause/Play stellt den Zustand wieder her, den es vorfindet: laeuft etwas, heisst es
Pause -> Play; laeuft nichts, Play -> Pause (die Wiedergabe bleibt danach also aus).
**Nur zusammen mit Jan ausfuehren:** es pausiert und startet die *echte* Wiedergabe auf
seinem Geraet. Voraussetzungen: ausserhalb der Sandbox (die mutet Audio-Aufnahme, siehe
Projektnotizen - hier aber ohnehin nur HTTP, kein Ton), die Spotify-App auf dem
Zielgeraet **offen** und ein **Premium**-Konto (Steuerbefehle lehnt Spotify sonst mit
``PREMIUM_REQUIRED`` ab). Ohne gespeicherten Login (kein Token in der Temp-Kopie) wird
der Schritt uebersprungen - er baut keinen eigenen Anmeldelauf auf.

**Warum ``--fresh-login`` existiert.** Das Skript kopiert die echte ``secrets.json`` mit,
also startet der Kern mit gespeichertem Token als ``connected=True`` und die Anmeldung
wird uebersprungen ("der Browser bleibt zu"). Genau der Browser-Weg ist aber der einzige
Teil, den sonst nichts prueft - ein Lauf ohne ``--fresh-login`` belegt ihn also *nicht*.
``--fresh-login`` loescht den Token in der **Temp-Kopie** und erzwingt den echten Weg.

Braucht eine Client-ID: entweder die Windows-Benutzervariable
``RUCKUS_SPOTIFY_CLIENT_ID`` oder ``%APPDATA%\Soundboard\config.json`` unter
``"spotify_client_id"``. Ohne Client-ID prueft das Skript nur den Rueckkanal auf der
Route ``/callback`` des lokalen Servers (``127.0.0.1:47800``, der einzige Teil, der sich
ohne Konto testen laesst) und sagt, was fehlt.

Mit Client-ID laeuft der echte Weg: Browser oeffnen, auf die Freigabe warten, Code
tauschen, danach ``/me``, ``/search`` (mit dem Februar-2026-Limit 10), ``/me/tracks``,
``/me/playlists`` und - falls eine Playlist da ist - ``/playlists/{id}/items``. Genau
die drei Stellen also, die Spotify im Februar 2026 umbenannt oder beschnitten hat.

Zum Schluss prueft es die **CSP der Oberflaeche**: jeder Cover-Host, den der Zustand
liefert, wird gegen die ``img-src``-Liste aus ``webui/vite.config.ts`` gehalten. Der
grund ist eine stille Fehlerquelle - ein nicht erlaubter Bild-Host wird vom Browser
ohne Fehlermeldung geblockt, und die Oberflaeche zeigt einfach ihren Ersatz-Cover.

Der Kern laeuft ``inline`` (wie in den Logiktests): geprueft wird nur der Spotify-Weg,
angefasst wird keine Audio-Hardware. Der lokale Server wird fuer den Anmeldelauf **echt
hergestellt** (``webserver.SseServer`` auf dem konfigurierten Port, an ``spotify``
angehaengt wie in ``webmain.run``) - ohne ihn bricht die Anmeldung mit "braucht den
lokalen Server" ab, und ein Prueflauf ohne Server haette den Rueckweg nie ausgeuebt.

**Das Skript schreibt nie in das echte Datenverzeichnis.** Es kopiert ``config.json`` und
``secrets.json`` aus ``%APPDATA%\Soundboard`` in ein Temp-Verzeichnis und setzt
``RUCKUS_DATA_DIR`` darauf (Projektregel). Es *liest* also die echte Client-ID und den
echten Token, laesst den Kern aber gegen die Kopie arbeiten - so testet es auch den Start
mit gespeichertem Token, ohne Nutzerdaten anfassen zu koennen. Der Grund ist nicht
Theorie: eine laufende ``RuckusRadio.exe`` schreibt ``config.json`` beim Speichern neu,
und eine aeltere Exe kennt neue Schluessel nicht - ein Prueflauf gegen das echte
Verzeichnis kann so eine gerade eingetragene Client-ID wieder verlieren.

Der Rueckkanal wird live gebaut: ein echter ``webserver.SseServer`` mit der echten Route
(``spotify._CallbackListener``), die eine echte HTTP-Anfrage annimmt. Er ist das Bauteil,
das live brechen kann - genau er soll hier ohne Attrappe laufen. Seit Spec §11 ist er eine
Route des lokalen Servers, kein eigener Listener auf einem zweiten Port.
"""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

# The real data dir, read-only: we copy out of it and never write into it (see the
# docstring). Captured before RUCKUS_DATA_DIR is redirected, so the messages below can
# still point the user at the real config.json.
_REAL_DIR = Path(os.environ.get("APPDATA", "")) / "Soundboard"
_REAL_CONFIG = _REAL_DIR / "config.json"
_DATA_DIR = Path(tempfile.mkdtemp(prefix="ruckus-spotify-manual-"))
for _name in ("config.json", "secrets.json"):
    if (_REAL_DIR / _name).exists():
        shutil.copy2(_REAL_DIR / _name, _DATA_DIR / _name)
os.environ["RUCKUS_DATA_DIR"] = str(_DATA_DIR)

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(_TESTS.parent))

from soundboard import config, protocol as p, spotify  # noqa: E402
import core_fakes  # noqa: E402


def read_client_id() -> tuple[str, str]:
    """(Client-ID, wo sie herkommt). Env gewinnt - wie in spotify._read_client_id."""
    from_env = os.environ.get(spotify.CLIENT_ID_ENV) or ""
    if from_env:
        return from_env, f"Umgebungsvariable {spotify.CLIENT_ID_ENV}"
    from_config = config.load_config().get("spotify_client_id") or ""
    return from_config, f"{_REAL_CONFIG}" if from_config else "config.json"


def configured_port() -> int:
    """Der Port, auf dem die Oberflaeche laeuft - und auf den Spotify weiterleitet."""
    from soundboard import webserver
    try:
        return int(config.load_config().get("server_port") or webserver.DEFAULT_PORT)
    except (TypeError, ValueError):
        return webserver.DEFAULT_PORT


def registered_uri() -> str:
    """Die Adresse, die im Spotify-Dashboard stehen muss: der Port aus config.json."""
    return spotify.redirect_uri(configured_port())


def drop_saved_token() -> bool:
    r"""Loescht ``spotify_token`` aus der **Temp-Kopie** der secrets.json.

    Nur die Kopie: das echte ``%APPDATA%\Soundboard`` wird nie angefasst. Ohne diesen
    Schritt startet der Kern ``connected`` (der Token ist ja da), die Anmeldung wird
    uebersprungen - und ein Prueflauf, der den Browser-Weg belegen soll, belegt ihn
    nicht. Rueckgabe: True, wenn wirklich ein Token entfernt wurde.
    """
    path = _DATA_DIR / "secrets.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict) or data.pop("spotify_token", None) is None:
        return False
    path.write_text(json.dumps(data), encoding="utf-8")
    return True


def probe_callback_channel() -> bool:
    """Der Rueckkanal ohne Konto: echter Server, echte Route, echte HTTP-Antwort."""
    from soundboard import webserver
    print(f"Rueckkanal:       {registered_uri()}  (Route des lokalen Servers)")
    server = webserver.SseServer(None, _DATA_DIR / "dist", port=0)
    threading.Thread(target=server.serve_forever, name="spotify-probe", daemon=True).start()
    deadline = time.monotonic() + 5.0
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.port:
        print("                  FEHLER - der Probe-Server hat nicht gebunden.")
        return False
    listener = spotify._CallbackListener(server, "PROBE")

    answer: dict = {}

    def hit() -> None:
        url = spotify.redirect_uri(server.port) + "?code=probe-code&state=PROBE"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                answer["status"] = response.status
                answer["body"] = response.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - the report below is the point
            answer["error"] = exc

    thread = threading.Thread(target=hit, name="spotify-probe-hit", daemon=True)
    thread.start()
    try:
        code, state = listener.wait(10.0)
    except Exception as exc:  # noqa: BLE001 - Timeout oder abgelehnte Anfrage
        thread.join(5.0)
        listener.close()
        server.stop()
        print(f"                  FEHLER - die Route hat nicht geantwortet ({exc}).")
        return False
    thread.join(5.0)
    listener.close()
    dropped = spotify.REDIRECT_PATH not in server._routes
    server.stop()

    if (code, state) != ("probe-code", "PROBE"):
        print(f"                  FEHLER - falsch gelesen: {code!r}, {state!r}")
        return False
    if answer.get("error") or answer.get("status") != 200:
        print(f"                  FEHLER - die Antwort an den Browser: {answer}")
        return False
    if not dropped:
        print("                  FEHLER - die Route blieb nach der einen Anfrage stehen.")
        return False
    print("                  OK - nimmt genau eine Anfrage an, antwortet und raeumt die Route weg")
    return True


CSP_CONFIG = Path(__file__).resolve().parent.parent / "webui" / "vite.config.ts"


def csp_image_hosts() -> list[str]:
    """The `img-src` hosts read straight out of `webui/vite.config.ts`.

    Read instead of copied, so this check cannot go stale against the real policy.
    """
    text = CSP_CONFIG.read_text(encoding="utf-8")
    line = next((l for l in text.splitlines() if "img-src" in l), "")
    return re.findall(r"https://([^\"'\s;]+)", line)


def host_allowed(host: str, allowed: list[str]) -> bool:
    """CSP matching: a `*.x` entry needs at least one label before `.x`."""
    for pattern in allowed:
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".scdn.co"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == pattern:
            return True
    return False


# The keys the interface feeds into an <img> (see webui/src/components/media/CoverArt.tsx).
# `external_url` is deliberately NOT here: it becomes an <a href>, and img-src does not
# govern navigation - checking it would only produce a false alarm about open.spotify.com.
IMAGE_KEYS = ("cover_url", "avatar_url")


def _image_urls(obj, depth: int = 0):
    """Every URL the interface would actually load as artwork."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in IMAGE_KEYS and isinstance(value, str) and value.startswith("http"):
                yield value
            else:
                yield from _image_urls(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _image_urls(item, depth + 1)


def image_hosts(state: dict) -> dict[str, str]:
    """(Host -> one example URL) for every artwork the interface would load."""
    found: dict[str, str] = {}
    for part in (state.get("search"), state.get("library"), state.get("playlist"),
                 state.get("user")):
        for value in _image_urls(part or {}):
            found.setdefault(urlsplit(value).netloc, value)
    return found


def _step(label: str, state: dict) -> bool:
    """Report one command's outcome from the state part. False on a Spotify error."""
    if state["error"]:
        print(f"                  FEHLER - {state['error']}")
        return False
    print(f"                  OK{label}")
    return True


def login_and_probe(client_id: str, origin: str, fresh_login: bool = False) -> bool:
    print()
    print(f"Client-ID:        {client_id[:6]}...{client_id[-4:]}  (aus {origin}; kein Geheimnis)")
    print("                  Muss im Dashboard exakt unter dieser Redirect-URI stehen:")
    print(f"                  {registered_uri()}   (nur diese IP, kein 'localhost')")

    if fresh_login:
        if drop_saved_token():
            print("                  --fresh-login: Token aus der Temp-Kopie entfernt")
        else:
            print("                  --fresh-login: kein Token in der Temp-Kopie - "
                  "der Weg laeuft trotzdem")
    print()

    # Seit Spec §11 ist der Rueckkanal eine Route des LOKALEN SERVERS. Ohne einen
    # angehaengten Server bricht login() mit "braucht den lokalen Server" ab - hier wird
    # deshalb derselbe Server hergestellt, den webmain.run uebergibt: auf dem Port, auf
    # den die Spotify-Weiterleitung zeigt. Ein abweichender Port wird benannt, nicht
    # stillschweigend hingenommen (Spotify kennt genau eine URL).
    from soundboard import webserver
    port = configured_port()
    server = webserver.SseServer(None, _DATA_DIR / "dist", port=port)
    threading.Thread(target=server.serve_forever, name="spotify-live-server",
                     daemon=True).start()
    deadline = time.monotonic() + 5.0
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.port:
        print("Lokaler Server:   FEHLER - hat nicht gebunden; der Anmeldelauf entfaellt.")
        return False
    warning = ("" if server.port == port else
               f"   ACHTUNG - erwartet {port}: die Spotify-Weiterleitung zeigt auf den "
               f"anderen Port, Spotify wird mit 'redirect_uri mismatch' ablehnen")
    print(f"Lokaler Server:   port {server.port}{warning}")

    core, events = core_fakes.make_core()
    core.store.data["spotify_client_id"] = client_id
    core.store.data["server_port"] = port
    core.spotify.attach_server(server)
    core.start()
    try:
        state = core.state()["spotify"]
        print(f"Start:            configured={state['configured']} connected={state['connected']}")

        if state["connected"]:
            print("Gespeichertes Token vorhanden - der Browser bleibt zu.")
            print("                  Der Anmeldeweg wird damit NICHT geprueft.")
            print("                  Dafuer erneut mit --fresh-login starten.")
        else:
            print("Kein Token: es oeffnet sich gleich der Browser. Dort die Freigabe erteilen.")
            print(f"Wartezeit: bis zu {int(spotify.LOGIN_TIMEOUT_S)} Sekunden ...")
            # Den Browser-Aufruf mitlesen und die URL drucken: in einer Sandbox oder bei
            # einem blockierten Standardbrowser oeffnet sich nichts, und ohne die URL
            # haette man nichts zum Anklicken. Danach wird ganz normal geoeffnet.
            opened: list[str] = []
            real_open = webbrowser.open

            def capture(url: str, *args, **kwargs):
                opened.append(url)
                print()
                print(f"Freigabe-URL:     {url}")
                print("                  Falls sich kein Browser oeffnet: diese Adresse von Hand "
                      "aufrufen.")
                return real_open(url, *args, **kwargs)

            webbrowser.open = capture
            try:
                core.send(p.SpotifyLogin())
            finally:
                webbrowser.open = real_open
            if not opened:
                print("                  HINWEIS - es wurde gar keine Freigabe-URL geoeffnet.")
            state = core.state()["spotify"]
            if not state["connected"]:
                print(f"                  FEHLER - nicht verbunden: {state['error']}")
                print("                  Die Ursache steht in der 'log:'-Zeile oben. Typisch:")
                print(f"                  - Redirect URI {registered_uri()} im Dashboard")
                print("                    nicht gespeichert (Add - danach Save!)")
                print("                  - Konto fehlt unter Settings -> User Management")
                print("                  - Freigabe im Browser nicht erteilt, oder Browser ging nicht auf")
                print("                  - ein anderes Programm haelt den Port, der Server wich aus")
                return False
            if not _step(" - SpotifyAuthChanged(True) kam", state):
                return False
            auth = core_fakes.of_type(events, p.SpotifyAuthChanged)
            print(f"                  Angemeldet, Events: {[e.connected for e in auth]}")

        # 1) Die Bibliothek: /me/tracks, /me/playlists, /me/albums. Der erste echte
        #    Aufruf refresht bei Bedarf lazy - ein abgelaufenes Token faellt hier auf.
        print()
        print(f"Bibliothek:       {spotify.API_BASE}/me/tracks?limit=50 + /me/playlists + /me/albums")
        core.send(p.SpotifyLoadLibrary())
        state = core.state()["spotify"]
        if not _step("", state):
            return False
        library = state["library"]
        print(f"                  {len(library['tracks'])} gespeicherte Titel, "
              f"{len(library['playlists'])} Playlists, {len(library['albums'])} Alben")
        if library["tracks"]:
            first = library["tracks"][0]
            print(f"                  Erster Titel: {first['title']!r} von {first['artist']!r}, "
                  f"Cover: {'ja' if first['cover_url'] else 'nein'}")

        # 2) Die Suche mit dem neuen Limit (10). Vor Februar 2026 war 20 erlaubt; mit
        #    einem groesseren Wert lehnt Spotify ab - hier wuerde es also auffallen.
        print()
        print(f"Suche:            {spotify.API_BASE}/search?limit={spotify.SEARCH_LIMIT}")
        core.send(p.SpotifySearch("Radiohead", "track", 0))
        state = core.state()["spotify"]
        if not _step("", state):
            return False
        search = state["search"]
        print(f"                  {len(search['items'])} Treffer, has_more={search['has_more']}, "
              f"naechster offset={search['offset']}")
        if search["has_more"]:
            core.send(p.SpotifySearchMore(search["query"], search["kind"], search["offset"]))
            state = core.state()["spotify"]
            if not _step("", state):
                return False
            print(f"                  Nach 'mehr laden': {len(state['search']['items'])} Treffer")

        # 3) Eine Playlist ueber den umbenannten Pfad. Inhalte kommen seit Februar 2026
        #    nur bei eigenen/mitbearbeiteten Playlists - leere items sind kein Fehler.
        if library["playlists"]:
            playlist = library["playlists"][0]
            print()
            print(f"Playlist:         {spotify.API_BASE}/playlists/{playlist['id']}/items "
                  f"(statt /tracks, Feb 2026)")
            print(f"                  {playlist['name']!r} von {playlist['owner']!r}, "
                  f"laut Metadaten {playlist['track_count']} Titel")
            core.send(p.SpotifyLoadPlaylist(playlist["id"], 0))
            state = core.state()["spotify"]
            if not _step("", state):
                return False
            view = state["playlist"]
            if view["items"]:
                print(f"                  {len(view['items'])} Titel gelesen, "
                      f"erster: {view['items'][0]['title']!r}")
            else:
                print("                  Keine Titel zurueck - erwartet bei fremden Playlists "
                      "(nur eigene/mitbearbeitete liefern Inhalte).")
        else:
            print()
            print("Playlist:         uebersprungen - die Bibliothek hat keine Playlist.")

        # 4) Die CSP der Oberflaeche gegen die echten Cover-URLs. Genau hier fiel schon
        #    einmal ein ganzer Bild-Host still aus (image-cdn-*): der Browser blockte ihn
        #    ohne Fehlermeldung, und die Oberflaeche zeigte ihren Ersatz-Cover. Ein neuer
        #    Host darf deshalb nicht wieder unbemerkt dazukommen.
        state = core.state()["spotify"]
        print()
        print("CSP:              img-src aus webui/vite.config.ts gegen die echten Cover-URLs")
        hosts = image_hosts(state)
        allowed = csp_image_hosts()
        blocked = sorted(h for h in hosts if not host_allowed(h, allowed))
        for host in sorted(hosts):
            print(f"                  {host:<36} {'OK' if host_allowed(host, allowed) else 'NICHT ERLAUBT'}")
        if blocked:
            print()
            print(f"                  FEHLER - img-src erlaubt nicht: {', '.join(blocked)}")
            print(f"                  steht in der CSP: {allowed}")
            print(f"                  Beispiel: {hosts[blocked[0]]}")
            print("                  Ohne Eintrag zeigt die Oberflaeche still den Ersatz-Cover.")
            print("                  Fix: in webui/vite.config.ts die passende Host-Familie ergaenzen.")
            return False
        print(f"                  Alle {len(hosts)} Hosts sind von der CSP erlaubt.")
    finally:
        core.shutdown()
        server.stop()

    print()
    print("OHNE FEHLER - der Python-Weg steht. Jetzt die Oberflaeche pruefen:")
    print("  Ruckus starten -> Musik-Tab -> 'Mit Spotify verbinden' bzw. die Bibliothek laden")
    print("  und ein Cover-Bild ansehen (CSP muss https://i.scdn.co erlauben).")
    return True


def build_player_api(client_id: str):
    """Ein echtes ``spotify.SpotifyApi`` aus dem gespeicherten Token der Temp-Kopie.

    ``None``, wenn kein Login gespeichert ist (z. B. nach ``--fresh-login`` ohne
    abgeschlossene Anmeldung) - dann baut ``--player`` keinen eigenen Anmeldelauf auf,
    er ueberspringt einfach. Liest nur die Temp-Kopie von ``secrets.json``
    (``_DATA_DIR``, siehe Modul-Docstring) - nie das echte Verzeichnis.
    """
    from soundboard import store as store_mod
    tokens = spotify.TokenStore(store_mod.Store(None, data=config._default_config()))
    if not tokens.load():
        return None
    return spotify.SpotifyApi(client_id, tokens)


def probe_player(api) -> bool:
    """``--player``: echte Geraete, echter Titel, ein echtes Pause/Play/Volume.

    Jeder Schritt meldet sich einzeln (``OK ...`` / ``FEHLER ...: <exc.text>``, nie ein
    Token). Rueckgabe True nur, wenn alle vier Schritte liefen.
    """
    from soundboard import spotify_player as sp

    print()
    print("=" * 78)
    print("Spieler (--player): Geraete, aktueller Titel, Pause/Play, Lautstaerke")
    print("=" * 78)
    ok = True

    # 1) Geraete
    current_volume: int | None = None
    device_supports_volume = False
    was_playing = False
    try:
        payload = api.get("/me/player/devices")
        devices = [sp.map_device(d) for d in (payload.get("devices") or [])]
        if devices:
            for d in devices:
                print(f"                  {d['name']!r} ({d['type']}) "
                      f"{'aktiv' if d['active'] else 'inaktiv'}, "
                      f"Lautstaerke={d['volume_percent']}")
        else:
            print("                  keine Geraete gemeldet")
        print("OK Geraete gelesen")
    except spotify.SpotifyError as exc:
        print(f"FEHLER Geraete gelesen: {exc.text}  [Detail: {exc.detail or "-"}]")
        return False

    # 2) Aktueller Titel
    try:
        raw = api.get("/me/player")
        state = sp.map_player(raw, time.time())
        track = state["track"]
        was_playing = bool(state["is_playing"])
        if track:
            print(f"                  laeuft: {track['title']!r} von {track['artist']!r}")
        else:
            print("                  nichts laeuft")
        if state["device"]:
            current_volume = state["device"]["volume_percent"]
            device_supports_volume = state["device"]["supports_volume"]
        print("OK aktueller Titel gelesen")
    except spotify.SpotifyError as exc:
        print(f"FEHLER aktueller Titel gelesen: {exc.text}  [Detail: {exc.detail or "-"}]")
        return False

    # 3) Umschalten und zurueck (kein Body): endet im vorgefundenen Zustand, damit ein
    #    Prueflauf nie Musik startet, die vorher aus war.
    first, second = ("pause", "play") if was_playing else ("play", "pause")
    label = f"{first.capitalize()}/{second.capitalize()}"
    print(f"                  {'laeuft' if was_playing else 'laeuft nicht'} -> {label}, "
          f"danach wieder {'an' if was_playing else 'aus'}")
    flipped = False
    try:
        api.send("PUT", f"/me/player/{first}")
        flipped = True
        time.sleep(2.0)
        api.send("PUT", f"/me/player/{second}")
        flipped = False
        print(f"OK {label}")
    except spotify.SpotifyError as exc:
        print(f"FEHLER {label}: {exc.text}  [Detail: {exc.detail or "-"}]")
        ok = False
    finally:
        if flipped:  # the first call went through: put the state back, best effort
            try:
                api.send("PUT", f"/me/player/{second}")
                print(f"                  zurueckgesetzt ({second})")
            except spotify.SpotifyError as exc:
                print(f"FEHLER zuruecksetzen ({second}): {exc.text}")

    # 4) Lautstaerke zurueck auf den gemerkten Wert - nur wenn das Geraet sie unterstuetzt
    if current_volume is None or not device_supports_volume:
        print("                  Lautstaerke uebersprungen - Geraet unterstuetzt sie nicht "
              "oder unbekannt")
    else:
        try:
            api.send("PUT", "/me/player/volume", params={"volume_percent": current_volume})
            print(f"OK Lautstaerke zurueck auf {current_volume}")
        except spotify.SpotifyError as exc:
            print(f"FEHLER Lautstaerke zurueck auf {current_volume}: {exc.text}  [Detail: {exc.detail or "-"}]")
            ok = False

    return ok


def probe_f3(api) -> bool:
    """``--f3``: Scopes, "Zuletzt gespielt", Alben und "Gefaellt mir" am echten Konto.

    "Gefaellt mir" schaltet den Stand eines Titels um und stellt sofort wieder her, was es
    vorfand. Rueckgabe True nur, wenn alle Schritte liefen.
    """
    from soundboard import spotify_player as sp

    print()
    print("=" * 78)
    print("F3 (--f3): Scopes, Zuletzt gespielt, Alben, Gefaellt mir")
    print("=" * 78)
    ok = True

    # 1) Scopes des gespeicherten Tokens (nur Namen, nie der Token)
    stored = api._tokens.load() or {}
    missing = spotify.missing_scopes(stored.get("scope") or "")
    if missing:
        print(f"FEHLER Scopes: es fehlen {', '.join(missing)} - einmal neu verbinden")
        return False
    print("OK Scopes: beide neuen Scopes erteilt")

    # 2) Zuletzt gespielt
    uri = None
    try:
        payload = api.get("/me/player/recently-played", {"limit": 50})
        items = payload.get("items") or []
        print(f"                  {len(items)} Eintraege")
        for entry in items[:3]:
            t = entry.get("track") or {}
            print(f"                  {t.get('name')!r}")
        if items:
            uri = (items[0].get("track") or {}).get("uri")
        print("OK Zuletzt gespielt gelesen")
    except spotify.SpotifyError as exc:
        print(f"FEHLER Zuletzt gespielt: {exc.text}  [Detail: {exc.detail or '-'}]")
        ok = False

    # 3) Alben + E5-Folgefrage: liefert /albums/{id}/tracks Inhalt?
    try:
        payload = api.get("/me/albums", {"limit": 5})
        albums = [(e.get("album") or {}) for e in payload.get("items") or []]
        print(f"                  {len(albums)} gespeicherte Alben (erste Seite)")
        if albums and albums[0].get("id"):
            tracks = api.get(f"/albums/{albums[0]['id']}/tracks", {"limit": 5})
            print(f"                  /albums/{{id}}/tracks: {len(tracks.get('items') or [])} Titel "
                  f"fuer {albums[0].get('name')!r}")
        print("OK Alben gelesen")
    except spotify.SpotifyError as exc:
        print(f"FEHLER Alben: {exc.text}  [Detail: {exc.detail or '-'}]")
        ok = False

    # 4) Gefaellt mir: aktueller Titel, sonst der zuletzt gespielte
    try:
        state = sp.map_player(api.get("/me/player"), time.time())
        if state["track"]:
            uri = state["track"]["uri"]
    except spotify.SpotifyError:
        pass
    if not uri:
        print("                  Gefaellt mir uebersprungen - kein Titel gefunden")
        return ok
    try:
        before = bool((api.get("/me/library/contains", {"uris": uri}) or [False])[0])
        print(f"                  {uri}: vorher {'gespeichert' if before else 'nicht gespeichert'}")
        api.send("DELETE" if before else "PUT", "/me/library", {"uris": uri})
        flipped = bool((api.get("/me/library/contains", {"uris": uri}) or [False])[0])
        api.send("PUT" if before else "DELETE", "/me/library", {"uris": uri})
        after = bool((api.get("/me/library/contains", {"uris": uri}) or [False])[0])
        if flipped == before or after != before:
            print(f"FEHLER Gefaellt mir: umgeschaltet={flipped}, zurueck={after}, vorher={before}")
            ok = False
        else:
            print("OK Gefaellt mir umgeschaltet und zurueckgesetzt")
    except spotify.SpotifyError as exc:
        print(f"FEHLER Gefaellt mir: {exc.text}  [Detail: {exc.detail or '-'}]")
        ok = False
    return ok


def main() -> int:
    # Spotify's own reason ("invalid_client", "redirect_uri mismatch") is logged, not
    # shown in the interface - so show the log here.
    logging.basicConfig(level=logging.INFO, format="log: %(levelname)s %(message)s")
    fresh_login = "--fresh-login" in sys.argv[1:]
    print("=" * 78)
    print("Spotify live pruefen - Musik-Tab (F1)")
    print("=" * 78)
    print("Anmeldung:        " + ("erzwungen (--fresh-login) - der Browser oeffnet sich"
                                  if fresh_login else
                                  "uebersprungen, wenn ein Token gespeichert ist "
                                  "(--fresh-login erzwingt sie)"))

    if not probe_callback_channel():
        return 1

    client_id, origin = read_client_id()
    if not client_id:
        print()
        print("Keine Client-ID hinterlegt - der echte Anmeldelauf entfaellt.")
        print("So hinterlegst du sie:")
        print("  1. https://developer.spotify.com/dashboard -> App anlegen")
        print(f"     - Redirect URI {registered_uri()} -> Add -> danach Save")
        print("     - 'Which API/SDKs' -> nur 'Web API' ankreuzen")
        print("     - Premium-Konto des Besitzers ist Pflicht (Development Mode seit 02/2026)")
        print("  2. Client ID kopieren, dann entweder")
        print(f"     - $env:{spotify.CLIENT_ID_ENV}=\"<id>\"  (Windows-Benutzervariable), oder")
        print(f"     - {_REAL_CONFIG} -> \"spotify_client_id\": \"<id>\"")
        print("     (Ruckus dabei schliessen: eine laufende Instanz schreibt config.json")
        print("      beim Speichern neu und wuerde den Schluessel wieder verlieren.)")
        print("  3. Dieses Skript erneut starten.")
        return 2

    exit_code = 0 if login_and_probe(client_id, origin, fresh_login) else 1

    if "--player" in sys.argv[1:]:
        api = build_player_api(client_id)
        if api is None:
            print()
            print("Spieler (--player): uebersprungen - kein gespeicherter Spotify-Login "
                  "in der Temp-Kopie.")
        elif not probe_player(api):
            exit_code = 1

    if "--f3" in sys.argv[1:]:
        api = build_player_api(client_id)
        if api is None:
            print()
            print("F3 (--f3): uebersprungen - kein gespeicherter Spotify-Login in der Temp-Kopie.")
        elif not probe_f3(api):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
