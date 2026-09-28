# Ruckus Radio

Ruckus Radio ist ein Soundboard für Windows: Es spielt deine MP3/MP4-Sounds per Klick oder globalem Hotkey so ab, dass Discord, Steam und Spiele sie wie ein Mikrofon-Signal hören — dabei kannst du normal weiterreden, beides wird gemischt. Windows kann Mikrofon + App-Sound nicht selbst mischen, deshalb braucht es ein **virtuelles Mikrofon** als Zwischenschicht. Zwei Wege:

| | **VB-CABLE** (empfohlen) | **VoiceMeeter** |
|---|---|---|
| Mischer | Ruckus Radio selbst | VoiceMeeter |
| Einrichtung | Treiber installieren, in Discord ein Gerät wählen | Treiber installieren, Routing in VoiceMeeter von Hand setzen |
| Mikro in Discord | nur solange Ruckus Radio läuft | immer |
| Latenz auf der eigenen Stimme | ~20–40 ms mehr | praktisch keine |

Ruckus Radio selbst ist eine einzelne `RuckusRadio.exe`, kein Python nötig zur Laufzeit. ffmpeg zum Dekodieren von MP3/MP4 ist eingebettet.

## VB-CABLE einrichten (empfohlen, einmalig)

Ruckus Radio nimmt dabei dein Mikrofon selbst auf, mischt die Sounds dazu und schickt beides ins virtuelle Kabel. In VoiceMeeter muss nichts geroutet werden — es gibt kein VoiceMeeter.

1. VB-CABLE von [vb-audio.com/Cable](https://vb-audio.com/Cable/) installieren, PC neu starten.
2. Ruckus Radio starten. Die Statusleiste zeigt **„VB-CABLE verbunden"**.
3. Im Assistenten (linke Seitenleiste → „Assistent“, Schritt 3) **„Discord-Gerät kopieren“** klicken — der exakte Gerätename liegt in der Zwischenablage.
4. Discord → Einstellungen → Sprache & Video → Eingabegerät = **CABLE Output (VB-Audio Virtual Cable)**. Steam genauso.
5. Discord: **Rauschunterdrückung und Echounterdrückung ausschalten** — Krisp filtert Soundboard-Sounds sonst als Störgeräusch weg.
6. Unten rechts **„Prüfen"** klicken: Ruckus Radio spielt einen Ton ins Kabel und nimmt ihn am anderen Ende wieder auf. Grün = die anderen hören dich.

**Wichtig:** In diesem Modus trägt Ruckus Radio dein Mikrofon. Beendest du die App, hat Discord kein Mikro mehr. Der Assistent bietet darum **„Mit Windows starten"** an, und beim Schließen fragt die App nach, ob sie stattdessen nur minimieren soll.

## VoiceMeeter einrichten (Alternative)

Wenn du VoiceMeeter lieber behältst — dann mischt VoiceMeeter, und Ruckus Radio spielt nur hinein.

1. VoiceMeeter von [vb-audio.com](https://vb-audio.com/Voicemeeter/) installieren, PC neu starten.
2. **Hardware Input 1** → dein echtes Mikrofon, Fader auf 0 dB, nicht gemutet.
3. **A1** → deine Kopfhörer. Ohne ein Gerät auf A1 läuft VoiceMeeters Audio-Engine nicht, und dann bleibt auch der B-Bus stumm — das ist der häufigste Grund für „Discord hört nichts".
4. **Hardware Input 1** UND **Virtual Input** beide auf **BUS B** routen (die „B"-Kachel bei beiden Kanälen).
5. Audio-Engine auf **48000 Hz** (Ruckus Radio dekodiert fest auf 48 kHz).
6. Discord und Steam → Eingabegerät = **Voicemeeter Out B1**. Achtung: Das früher übliche „VoiceMeeter Output" legen aktuelle Treiber nicht mehr an, und „Voicemeeter Out B2/B3" füttert nur die Potato-Edition. Der Assistent (Schritt 3, „Discord-Gerät kopieren“) nennt dir den richtigen Namen für deine Installation.
7. **VoiceMeeter vor Ruckus Radio starten** — die App sucht das virtuelle Gerät beim Start.

Findet Ruckus Radio gar kein virtuelles Mikrofon, startet es trotzdem: Die Statusleiste zeigt „Kein virtuelles Mikrofon — nur Mithören aktiv", du hörst die Sounds lokal, bei Discord kommt nichts an.

## CS und Discord getrennt bedienen

Standardmäßig hört jedes Programm, das an einem virtuellen Kabel hängt, dieselbe Mischung aus Stimme und Sounds. Willst du stattdessen, dass CS deine Sounds hört, während Discord nur deine Stimme bekommt, brauchst du ein **zweites** virtuelles Kabel:

1. Zusätzlich zu VB-CABLE ein zweites Kabel installieren, z. B. das kostenlose [Hi-Fi Cable von VB-Audio](https://vb-audio.com/Cable/) — beide können parallel installiert sein.
2. In CS als Mikrofon `CABLE Output (VB-Audio Virtual Cable)` einstellen, in Discord `Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)` (oder umgekehrt) — jedes Programm bekommt sein eigenes Kabel als Eingabegerät.
3. Auf der Seite **Einstellungen** in Ruckus Radio zeigt jedes gefundene Kabel seine eigene Zeile mit den Schaltern **Mikro** und **Sounds**. Beim Kabel, das Discord hört, **Sounds** ausschalten — es kommt dann nur noch deine Stimme an. Beim Kabel für CS bleiben beide Schalter an.
4. In den **Einstellungen → Verbindung** bei **„Discord hört über“** das Kabel wählen, das Discord als Eingabegerät nutzt. Ruckus kann Discords Einstellung nicht auslesen; ohne diese Wahl nimmt es das Haupt-Kabel an, und der Dock-Knopf „Discord: Sounds an/aus“ schaltet dann das falsche Kabel.
5. Die **Kopfhörer**-Zeile ganz unten regelt nur dein eigenes Mithören, unabhängig von den Kabeln — schaltest du dort Sounds aus, hörst du deine eigenen Sounds nicht mehr mit, ohne dass CS oder Discord etwas davon merken.

## Mischpult: Stimme, Sounds und Lautstärke

Auf der Seite **Einstellungen** steht oben das **Mischpult**. Es gilt für alle Kabel, die Kopfhörer regelst du getrennt in ihrer eigenen Zeile.

- **Mikrofon:** Hier wählst du dein Mikro. Empfohlen: **NVIDIA Broadcast** (kostenlos, braucht eine RTX-Grafikkarte). Es entfernt Tastatur, Lüfter und Hintergrund **nur aus deiner Stimme**, die Sounds bleiben unberührt. Einrichtung: NVIDIA Broadcast installieren → Reiter Mikrofon → Quelle = dein echtes Mikro → Effekt „Rauschentfernung" an → in Ruckus Radio „Mikrofon (NVIDIA Broadcast)" wählen.
- **Stimme:** Ruckus Radio gleicht deine Stimme automatisch an (leise Stellen lauter, laute Stellen leiser, Spitzen begrenzt). Du musst nichts einstellen.
- **Sounds unter Stimme:** Jeder Sound wird beim Hinzufügen gemessen und automatisch auf die Lautheit deiner Stimme gebracht, auch Songs, die sofort voll einsetzen. Der Regler legt fest, wie weit die Sounds darunter liegen. Faustregel: **−6 dB** = deutlich leiser, **−10 dB** = etwa halb so laut, **−20 dB** = leise im Hintergrund.
- **Ducking:** Während du sprichst, werden die Sounds zusätzlich leiser und danach sanft wieder lauter.
- **Lautstärke pro Sound** (Rechtsklick → Lautstärke…): kommt obendrauf auf die automatische Angleichung, von −30 dB bis +3,5 dB.

Unter jedem Sound steht, was die Automatik gemacht hat, z. B. „auto −9 dB". „misst …" heißt, die Messung läuft noch.

Empfohlene Windows-Einstellung für dein Mikro und die Kabel: **48000 Hz, 24 Bit** (Systemsteuerung → Sound → Aufnahme → Eigenschaften → Erweitert). Ruckus Radio und Discord arbeiten intern mit 48 kHz, 96 kHz bringt nichts.

In Discord am Kabel, das Discord hört: **Krisp/Rauschunterdrückung aus, Echounterdrückung aus, automatische Verstärkungsregelung aus, „Eingangsempfindlichkeit automatisch bestimmen“ aus und den Regler ganz nach links**. Sonst filtert Discord die Sounds mit, und seine Sprachaktivierung schneidet leise Anfänge ab (Wortanfänge, ruhige Song-Intros). Die Stille in Pausen übernehmen NVIDIA Broadcast und Ruckus Radio.

**Abgehackte Stimme mit NVIDIA Broadcast:** Ruckus reicht genau weiter, was NVIDIA Broadcast liefert (gemessen 2026-09-27). Zwei Dinge schneiden dort ab: ein **zu leiser Mikrofonpegel** (Windows → Sound → Eingabe → dein Mikro auf 90–100; bei −45 dB Sprachpegel hielt die Geräuschunterdrückung die Stimme für Rauschen) und die **Geräuschunterdrückung auf hoher Stärke**, die einen gleichmäßig gehaltenen Ton nach etwa 1,5–2 Sekunden hart ausblendet (lang gezogene Vokale). Normales Sprechen ist davon kaum betroffen; wer lange Töne braucht, senkt die Stärke etwas.

## Erster Start

Beim allerersten Start öffnet sich automatisch ein kurzer Einrichtungsassistent (Mikrofon wählen, virtuelles Mikrofon prüfen, ersten Sound + Hotkey anlegen). Schritt 3 prüft nicht nur, ob das Gerät existiert, sondern schickt einen Ton hindurch und misst, ob er ankommt — und nennt dir das Gerät, das du in Discord einstellen musst. Du kannst jeden Schritt überspringen und später nachholen. Über **„Assistent"** in der linken Seitenleiste lässt er sich jederzeit erneut öffnen.

## Bedienung

**Sound hinzufügen**: Einen „Freier Slot" (gestrichelter Kreis mit „+", „Klicken zum Füllen") anklicken. Das Grid zeigt immer mindestens drei Reihen und nach dem letzten Sound noch eine volle Reihe freier Slots, es ist also immer einer frei. Datei wählen (`*.mp3` oder `*.mp4` — bei MP4 wird nur die Tonspur übernommen, kein Video), optional ein Icon-Bild, Name eintragen. Der Sound erscheint sofort als rundes Tile im Grid.

**Abspielen**: Linksklick auf ein Tile spielt den Sound (sehr lange Dateien über 15 MB werden erst beim ersten Klick entpackt, der dauert dann ein paar Sekunden); ein Hotkey (falls vergeben) tut dasselbe, auch wenn ein Spiel im Vordergrund ist. Mehrere Sounds dürfen gleichzeitig/überlappend laufen.

**Rechtsklick-Menü** auf einem Sound-Tile:
- **Icon ändern…** — neues Bild wählen, wird rund zugeschnitten.
- **Umbenennen…**
- **Hotkey neu belegen…** — Tastenkombination aufnehmen. Jede Kombination ist erlaubt, auch AltGr+Zahl oder eine einzelne Taste. Tippt die Kombination beim Chatten nebenbei ein Zeichen (z. B. AltGr+2 = ², AltGr+7 = {) oder feuert sie beim Tippen, zeigt der Dialog nur einen Hinweis. Abgelehnt wird nur eine Kombination, die schon einem anderen Sound oder „Alle stoppen" gehört. Die Fn-Taste sieht Windows nicht (die Tastatur verarbeitet sie selbst); wer Fn+Zahl nutzen will, legt sie in der Tastatur-Software auf F13–F24.
- **Lautstärke…** — Regler in dB (−30 bis +3,5), kommt obendrauf auf die automatische Angleichung. „Probehören“ spielt nur in deinen Kopfhörern, Discord hört die Probe nicht.
- **Exportieren…** — diesen einen Sound als `.ruckuspack` speichern.
- **Löschen**

**Suche**: Das Suchfeld oben links filtert Tiles live nach Name (auch per `Strg+F` fokussierbar, `Esc` leert die Suche).

**Alle stoppen**: Der Button unten rechts bricht sofort jede laufende Wiedergabe ab — für Panik-Momente. Ein globaler Hotkey (Standard `Strg+ß`) macht dasselbe, auch außerhalb der App. Bis 1.1.3 war der Standard `Strg+Alt+Rücktaste` (auf deutschen Tastaturen AltGr+Rücktaste); wer ihn nie geändert hat, bekommt einmalig `Strg+ß`, außer ein Sound nutzt diese Kombination schon. `Strg+Alt+Entf` gehört Windows und lässt sich nirgends vergeben. In der Web-Oberfläche ist der Hotkey unter Einstellungen → „Tastenkürzel“ änderbar.

**Mithören**: Wie laut du deine eigenen Sounds selbst hörst, stellst du auf der Seite **Einstellungen** in der **Kopfhörer**-Zeile ein (unabhängig von der Lautstärke, die bei Discord/Steam ankommt — Standard 50 %).

**Statusleiste unten**: Links steht, welches virtuelle Mikrofon läuft. Rechts daneben:

- **Discord: Sounds an/aus** — schaltet nur die Sounds im Discord-Kabel stumm; dein Mikro bleibt in Discord hörbar, CS/Steam hören die Sounds weiter.
- **Prüfen** — schickt einen Ton durch das virtuelle Mikrofon und misst am anderen Ende, ob er ankommt. Das ist der einzige Test, der ohne Mitspieler beweist, dass die Gegenstelle dich hört. Schlägt er fehl, zeigt die Statusleiste „gefunden, aber kein Signal — prüfen" statt grün.
- **Mikro an/aus** — nur im VB-CABLE-Modus: schaltet dein Mikrofon stumm, die Sounds laufen weiter.

## Spotify im Musik-Tab

Im Bereich **Musik** kann Ruckus deine Spotify-Bibliothek durchsuchen, gespeicherte Titel und eigene Playlists ansehen und die Wiedergabe der Spotify-App fernsteuern. Ruckus spricht direkt mit der **Spotify Web API**; es braucht kein Zusatzprogramm.

**Wiedergabe steuern (seit F2).** Ruckus ist eine **Fernbedienung** für die Spotify-App — der Ton kommt aus der App, nicht aus Ruckus. Voraussetzungen: **Spotify Premium** (sonst lehnt Spotify jeden Steuerbefehl ab, Ruckus sagt „Steuern braucht Spotify Premium.“) und die **Spotify-App läuft** auf diesem Rechner (sonst: „Öffne die Spotify-App auf diesem Rechner.“).

- **Dock-Leiste** (auf jeder Seite): Titel · Künstler mit Cover, Zurück/Play-Pause/Weiter, Zeitleiste und Lautstärke. Zeitleiste und Lautstärke senden erst beim Loslassen. Meldet das Gerät keine Lautstärke, ist der Regler gesperrt.
- **Musik-Seite**, Zeile „Wiedergabe“: Geräte-Pille (öffnen lädt die Geräte, Wahl schiebt die Wiedergabe dorthin; Pfeiltasten und Escape gehen auch), Zufallswiedergabe, Wiederholen (aus → Playlist → Titel). Titel per Klick, Enter oder Leertaste abspielen, „+“ hängt an die Warteschlange, Play auf Playlist-Karten spielt die Playlist.
- **Browser-Ansicht** (Link aus Einstellungen → Zugang, z. B. im Steam-Overlay): darf ebenfalls steuern; Anmelden/Abmelden bleibt dem Fenster vorbehalten.
- Ohne aktives Gerät nimmt Play diesen PC (oder das zuletzt gewählte Gerät). Ruckus fragt den Zustand nur ab, solange eine Oberfläche offen ist (alle 5 s bei laufender Musik, sonst alle 20 s).

**Einmalig einrichten** — auf https://developer.spotify.com/dashboard eine App anlegen (kostenlos, Spotify-Konto genügt). Das Formular so ausfüllen:

| Feld | Eingabe |
|---|---|
| App name | z. B. `Ruckus Radio` |
| App description | z. B. `Steuert die Spotify-Wiedergabe und zeigt die eigene Bibliothek.` |
| Website | darf **leer bleiben** (sonst z. B. `https://github.com/janpinneker/RuckusRadio`) |
| **Redirect URI** | `http://127.0.0.1:47800/callback` → **Add** → danach unten **Save** (ohne Save ist er nicht gespeichert) |
| Which API/SDKs are you planning to use? | **nur „Web API“** ankreuzen |

1. Danach unter *Settings* die **Client ID** kopieren und in `%APPDATA%\Soundboard\config.json` als `spotify_client_id` eintragen — oder als Windows-Benutzervariable `RUCKUS_SPOTIFY_CLIENT_ID` setzen.
2. Unter *Settings → User Management* die Spotify-Konten eintragen, die sich anmelden dürfen (Name **und** E-Mail des Spotify-Kontos). Die App läuft im **Development mode** und lässt nur diese Konten zu — seit Februar 2026 sind das **5** (früher 25).
3. Ruckus neu starten, im Musik-Tab auf **„Mit Spotify verbinden“** klicken und im Browser bestätigen. Die Weiterleitung landet auf der Route `/callback` des lokalen Servers (`127.0.0.1:47800`) — es gibt keinen zweiten Listener; die Route lebt nur während der Anmeldung und wird danach wieder entfernt.

**Ruckus beim Eintragen der Client ID schließen.** Eine laufende Ruckus-Instanz schreibt `config.json` beim Speichern selbst neu, und ein **älterer** Build kennt `spotify_client_id` noch nicht — er würde den Eintrag wieder entfernen. Wer das ganz umgehen will, nimmt die Windows-Benutzervariable `RUCKUS_SPOTIFY_CLIENT_ID`; die kann kein Programm überschreiben.

**Kein SDK, kein Client-Secret.** Ruckus nutzt ausschließlich die **Spotify Web API** (REST über HTTPS) und den **Authorization Code Flow mit PKCE** — ein eigenes SDK gibt es dafür nicht, Python ruft die API direkt auf (`urllib`, schon im Projekt). Das **Web Playback SDK** wird bewusst nicht verwendet: Es braucht Widevine, das WebView2 nicht mitbringt (deshalb Variante A, Ton aus der Spotify-Desktop-App). Ein Client-Secret brauchen öffentliche PKCE-Clients nicht — und es gehört ohnehin nie ins Repo.

**Wichtig:**

- **Premium ist Pflicht, nicht nur fürs Abspielen.** Seit der Spotify-Umstellung vom **11.02.2026** (für bestehende Apps ab 09.03.2026) muss der **App-Besitzer** ein **aktives** Premium-Abo haben — läuft es aus, stellt Spotify die App komplett ab, auch Suche und Bibliothek. Wiedergabe-Steuerung (F2) braucht Premium ohnehin. Dazu: **1 Client-ID je Entwickler** (Juli 2026 auf 25 erhöht) und **5 autorisierte Nutzer** je Client-ID.
- **Spotify hat die Web API im Februar 2026 zusammengestrichen.** Ruckus ist darauf eingestellt: Eine Suche liefert höchstens **10** Treffer pro Anfrage (Ruckus blättert nach), Playlists tragen intern `items` statt `tracks`, und **Playlist-Inhalte kommen nur noch von eigenen oder mitbearbeiteten Playlists** — fremde Playlists erscheinen nur mit Name und Cover. Bibliothek (gespeicherte Titel/Alben/Playlists), Suche und die Player-Befehle für F2 bleiben verfügbar.
- **Der Ton kommt aus der Spotify-App**, nicht aus Ruckus: Ruckus ist nur Fernbedienung. Auf dem PC die Spotify-Desktop-App öffnen, dann erscheint sie als Gerät.
- **Musik ins virtuelle Mikrofon (Musik-Bus): der Kern ist verdrahtet** (Stand 2026-09-27, Spec `docs/superpowers/specs/2026-09-27-musik-bus-kern-design.md`): Befehle `SetMusicBus`/`SetMusicBusGain`, Zustandsteil `musicbus`, Config `musicbus_enabled`/`musicbus_gain`, und der Ton läuft per WASAPI-Loopback **wie ein Sound** in die Kabel — mit „Sounds unter Stimme“, Ducking und Limiter, die Kopfhörer bleiben unreguliert. Schalter und Regler stehen auf der **Musik-Seite** („Musik ins Mikrofon“, auch im Steam-Overlay-Browser). **Offen:** der Hörtest mit echtem Ton. Bis dahin bleibt der Musik-Tab reines Mithören. Der Sofort-Weg ohne Ruckus: Windows → Einstellungen → System → Sound → Lautstärkemixer → Spotify auf `CABLE Input` stellen — dann hören Discord/Steam die Musik, aber ohne Ducking, ohne Ruckus-Regler und „Alle stoppen“ stoppt sie nicht.
- Der **Anmeldeschlüssel** (Refresh-Token) liegt im gemeinsamen Secrets-Speicher `%APPDATA%\Soundboard\secrets.json` (Eintrag `spotify_token`) und wird nie an die Oberfläche oder ins Log gegeben — geloggt werden nur Name und Länge. Abmelden über den Musik-Tab löscht ihn. Wer noch die alte `spotify_token.json` hat: sie wird beim Start einmalig übernommen und danach entfernt.
- **Die Oberfläche muss laufen.** Die Anmeldung braucht den lokalen Server; im reinen Tk-Fenster meldet Ruckus das, statt auf einem zweiten Port zu lauschen, den Spotify nicht kennt. Ist der Serverport belegt und die Oberfläche weicht aus, sagt Ruckus das ebenfalls im Klartext — Spotify erlaubt nur die eingetragene Adresse, ein Ausweichport würde erst später als `redirect_uri mismatch` auffallen.
- Die Redirect-Adresse muss **genau** dieser Loopback-IP entsprechen. `http://localhost:…` ist bei Spotify seit November 2025 **nicht mehr erlaubt** (HTTP nur noch für Loopback-IP-Literale wie `127.0.0.1`); die Portangabe darf bei Loopback-IP auch weggelassen werden, dann sind dynamische Ports erlaubt. Ruckus nutzt den festen Port 47800 (dieselbe Adresse wie die Oberfläche).
- Die Client ID ist kein Geheimnis (PKCE ohne Client-Secret); im Repo steht sie nicht.

**Anmeldung selbst prüfen** (braucht die Client ID, öffnet den echten Browser):

```bash
venv/Scripts/python.exe tests/test_spotify_manual.py
```

Mit `--player` prüft es zusätzlich die Fernbedienung am echten Konto (Premium, Spotify-App offen): Geräte, laufender Titel, einmal Pause/Play — in der Reihenfolge, die den vorgefundenen Zustand wiederherstellt (läuft nichts, bleibt es danach aus) — und die Lautstärke.

Das Skript prüft zuerst den Rückkanal auf `127.0.0.1:47800` (geht auch ohne Client ID), führt dann die echte Anmeldung durch und ruft Bibliothek, Suche und eine Playlist ab — es meldet die Stelle, an der es hakt. Zum Schluss hält es jeden Cover-Host aus dem Zustand gegen die `img-src`-Liste aus `webui/vite.config.ts`: ein Host, den die CSP nicht erlaubt, lässt es scheitern. Das ist die einzige Stelle, an der ein **neuer** Spotify-Bildhost sonst still ausfiele — der Browser blockiert ein Bild ohne Fehlermeldung, und die Oberfläche zeigt einfach ihren Ersatz-Cover.

Scheitert die Anmeldung **sporadisch** mit einer Netz-Meldung, ist das kein Fehler in Ruckus: Spotifys Anmelde-Server schickt je nach Edge-Knoten ein unvollständiges Zertifikat, und Windows-Programme merken das nur nicht (sie haben das fehlende Zwischenzertifikat gespeichert). Die technische Ursache steht im Log; ein zweiter Versuch klappt.

## Sounds exportieren/importieren (`.ruckuspack`)

Sound-Sammlungen lassen sich als `.ruckuspack`-Datei (technisch ein ZIP mit Audio, Icon und Metadaten pro Sound) weitergeben:

- **Exportieren**: Rechtsklick → „Exportieren…" für einen einzelnen Sound, oder der Toolbar-Button „Exportieren…" über dem Grid für das ganze Board.
- **Importieren**: Toolbar-Button „Importieren…", `.ruckuspack`-Datei wählen. Jeder importierte Sound bekommt eine neue lokale ID; bei Namenskollision wird „(2)" angehängt.
- **Hotkeys werden nie mit-exportiert** (personenbezogen/kollisionsanfällig) — nach dem Import vergibst du sie neu per Rechtsklick → „Hotkey neu belegen…".

## Hotkeys, Administratorrechte und Anti-Cheat

Globale Hotkeys laufen über einen systemweiten Low-Level-Tastatur-Hook (`keyboard`-Bibliothek) und funktionieren normalerweise auch, wenn ein Spiel im Vordergrund ist. Falls ein Hotkey im Spiel nicht feuert: **Ruckus Radio als Administrator starten** — manche Spiele/Anti-Cheat-Prozesse laufen selbst mit höheren Rechten, und ein Tastatur-Hook mit niedrigeren Rechten sieht dann keine Tastendrücke mehr, solange das Spiel den Fokus hat.

Low-Level-Hooks werden von manchen Anti-Cheat-Systemen (z. B. VAC, FACEIT) beobachtet. Ruckus Radio liest nur Tastenkombinationen aus, greift nicht auf Spiel-Speicher zu und manipuliert nichts im Spiel — trotzdem: Einsatz in Competitive-Matches auf eigenes Risiko.

## Datenspeicherort

Konfiguration, Sounds und Icons liegen unter `%APPDATA%\Soundboard\`:
- `config.json` — Einstellungen, Geräte, Hotkeys, Sound-Liste
- `sounds\` — dekodierte Audiodateien (MP3)
- `icons\` — rund zugeschnittene Icon-PNGs
- `ruckus.log` — Fehlerprotokoll (max. 1 MB, plus drei ältere Dateien); bei Problemen hier nachsehen
- `config.json.bak` — nur vorhanden, wenn `config.json` beschädigt war: die kaputte Datei, die App startet dann mit Standardeinstellungen

Das bleibt über Neustarts und exe-Updates hinweg erhalten (im Gegensatz zum PyInstaller-Extraktionsordner, der bei jedem Start neu angelegt und wieder gelöscht wird). Für Tests/Entwicklung lässt sich der Ort per Umgebungsvariable `RUCKUS_DATA_DIR` umbiegen.

## Selbst bauen

Voraussetzung: Python-venv unter `venv\` mit den Paketen aus `requirements.txt` (inkl. `pyinstaller`).

ffmpeg wird nicht mitcommittet (`assets/*.exe` ist in `.gitignore`, zu groß fürs Repo). `build.ps1` besorgt es automatisch — lädt Gyan's "essentials"-Build (`ffmpeg-release-essentials.zip`, ~98 MB je Binary statt ~212 MB beim "full"-Build; enthält alles, was Ruckus Radio braucht: MP3/MP4-Demuxing, MP3/AAC-Decoding, MP3-Encoding via libmp3lame) von [gyan.dev](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip) herunter, entpackt nur `ffmpeg.exe` + `ffprobe.exe` nach `assets\` und löscht den Rest wieder. Kein manueller Schritt nötig — einfach:

```powershell
.\build.ps1
```

Falls der Download fehlschlägt (kein Internet, URL down), fällt das Skript auf eine vorhandene `winget install Gyan.FFmpeg`-Installation zurück (deutlich größer, jeder Codec inklusive). Alternativ lassen sich `ffmpeg.exe` + `ffprobe.exe` auch von Hand nach `assets\` kopieren — von der [essentials-Build-Seite](https://www.gyan.dev/ffmpeg/builds/) oder aus einer winget-Installation.

Das Skript legt bei Bedarf das venv an, installiert die Abhängigkeiten, holt `ffmpeg.exe`/`ffprobe.exe` wie oben beschrieben nach `assets\` (falls noch nicht vorhanden), erzeugt `assets\icon.ico` (falls fehlend, via `tools\make_icon.py`) und baut anschließend über `pyinstaller build.spec --noconfirm` die fertige `dist\RuckusRadio.exe` — eine einzelne, windowed Onefile-Datei inkl. gebündeltem ffmpeg, PortAudio und customtkinter-Theme-Daten.

Hinweis: Die ffmpeg-Binaries werden bewusst nicht mit UPX komprimiert (PyInstaller-Option), obwohl das die exe weiter verkleinern würde — UPX-komprimierte Binaries lösen bei mehreren Virenscannern False-Positives aus.

Das App-Icon (`assets\icon.ico`) wird aus den Design-Tokens in `soundboard\theme.py` generiert und lässt sich jederzeit neu erzeugen: `venv\Scripts\python.exe tools\make_icon.py`.

Web-Oberfläche (Probe B0, bis zur Umsetzung von „eine Oberfläche überall“): `build.ps1` baut `webui/` mit Node.js (npm) mit, wenn der Ordner existiert. Start mit `RuckusRadio.exe --webui`; ohne Schalter startet die gewohnte Oberfläche. Im Quelltext (Branch `feat/one-ui`) ist es bereits umgekehrt: ohne Schalter die Web-Oberfläche über den lokalen Server, `--tk` für die gewohnte, `--serve` startet nur den Server ohne Fenster. Zum Ausprobieren ohne echte Geräte: `cd webui && npm install && npm run dev`, dann http://localhost:5173 (Attrappe mit Beispiel-Sounds). Die Schrift Inter ist mitgeliefert (SIL OFL, siehe `THIRD-PARTY-LICENSES.md`); die Gestaltungsregeln stehen in `DESIGN.md`. Stand der Web-Oberfläche: Startseite mit Favoriten (eigene Seite „Favoriten“), Playlists und den meistgespielten Sounds (fest pro Sitzung; mehrfaches Klicken innerhalb von 2 Sekunden zählt als eine Benutzung); „Einstellungen“ sitzt unten links im Dock, so breit wie die Seitenleiste, mit einem Statuspunkt für das virtuelle Mikro (grün, gelb, rot – mit eigener Form je Zustand); „Prüfen“ liegt in Einstellungen → „Verbindung“; spielt ein Sound, zeigt das Dock eine Sound-Leiste mit Fortschritt und Stopp; Raster lassen sich mit den Pfeiltasten bedienen. Im Browser-Prototyp greifen die Hotkeys (Standard „Alle stoppen“: Strg+ß) nur, solange die Seite den Fokus hat; global hört sie erst die App.

## Weiterentwicklung (Stand 2026-09-27)

Für Agents, die am Projekt arbeiten: Arbeitsregeln, Lesereihenfolge und Skill-Lade-Wege stehen in [`AGENTS.md`](AGENTS.md).

Geplant sind drei Bereiche: **Soundboard**, **Musik** (Bibliothek, Playlists, Wiedergabe in den Mixer) und **Blackbox** (Sprach-Studio über das separat installierte [Voicebox](https://github.com/jamiepine/voicebox)), dazu ein **Zuschnitt-Editor** für Sounds und Musik. Die Programmlogik liegt seit 2026-09-25 in einem eigenen App-Kern; die heutige Oberfläche zeigt nur an und schickt Befehle. Ruckus startet höchstens einmal und kann sich selbst aktualisieren (Abschnitt „Update“). Die neue Web-Oberfläche (React, dunkle Wise-Welt mit Lime-Akzent, Regeln in `DESIGN.md`) liegt im Ordner `webui/` und **ist die Oberfläche**: der Messlauf am 2026-09-27 hat sie bestätigt (Fenster-Anpassen ≈ 1 ms statt ≈ 240 ms, Ton am Kabel und Hotkeys unverändert; Werte in `docs/superpowers/b0-messwerte.md`). **Seit 2026-09-27 kann sie alles, was die gewohnte Tk-Oberfläche kann** — auch Sounds hinzufügen, Packs importieren/exportieren, Icon ändern, Mikrofon wählen, Mischpult, Geräte neu suchen, Autostart, Updates und den Assistenten (Dateidialoge öffnet weiterhin Python, die Seite übergibt nie Pfade). Die gewohnte Oberfläche bleibt nur noch als Rückfall (`RuckusRadio.exe --tk`). **Stand und Nächstes (Spec und Plan stehen; das Fundament, die Umstellung auf den Server und die Lücke zur gewohnten Oberfläche sind umgesetzt):** Die Web-Oberfläche wird Standard und lässt sich zusätzlich im Steam-Overlay-Browser bedienen, ohne das Spiel zu verlassen. Dafür bekommt Ruckus einen **lokalen Server auf `127.0.0.1`**: das Fenster lädt dieselbe Adresse wie der Browser, und beide sprechen denselben Weg (HTTP + Server-Sent-Events), also gibt es nur eine Codeschiene. Der Zugang ist über zwei Schlüssel getrennt — das Fenster darf alles, ein Browser-Link nur abspielen und zusehen. Die Adresse steht in den Einstellungen und funktioniert als Lesezeichen im Steam-Overlay. Die gewohnte Oberfläche bleibt als Rückfall (`--tk`), und `--serve` startet nur den Server ohne Fenster. Entwurf: `docs/superpowers/specs/2026-09-27-eine-oberflaeche-design.md`, Umsetzung: `docs/superpowers/plans/2026-09-27-eine-oberflaeche-kern-server.md`. Die Vorarbeit legt die Bausteine an, die Musik und Stimme später ohne Umbau brauchen. Der Plan, der die Lücke geschlossen hat (`docs/superpowers/plans/2026-09-27-eine-oberflaeche-oberflaeche.md`, 11 Tasks), ist bis auf die Handprüfung mit echten Geräten abgearbeitet; Fortschritt und Belege: `docs/superpowers/eine-oberflaeche-log.md`. Offen bleiben Ordner, Playlists und Stichwörter im Kern (Bibliothek 2.0, heute nur in der Attrappe).

Die **Spotify-Anbindung im Musik-Tab** ist in der ersten Stufe fertig (F1: Anmeldung per OAuth-PKCE, Token-Speicher, Suche, gespeicherte Titel und Playlists, echte Cover-Bilder; Spec `docs/superpowers/specs/2026-09-27-spotify-musik-tab-design.md`, Plan `docs/superpowers/plans/2026-09-27-spotify-musik-tab.md`). Die Wiedergabe-Steuerung (F2) ist seit 2026-09-28 fertig und mit echtem Konto geprüft (offen: Seek und Browser-Ansicht von Hand bestätigen). Der Feinschliff (F3: Ergebnis-Gruppen, Playlist-Detail, „Gefällt mir“, „Zuletzt gespielt“) ist als Entwurf mit offenen Entscheidungen beschrieben (Spec §14). Der Ton kommt über den **Musik-Bus** in den Mixer: die Technik steht (WASAPI-Loopback per ctypes/COM, `soundboard/musicbus.py`) und ist an den Kern verdrahtet (klingt für andere wie ein Sound). Die Schalter und der Pegelregler sitzen seit 2026-09-27 auf der Musik-Seite (`f15ac17`); offen ist nur der Hörtest mit echtem Ton (Ducking, Exclusive Mode). Bis dahin bedient der Musik-Tab nur und spielt über die Spotify-App ab.

Danach geplant: Zuschneiden, Ordner und Stichwörter im Kern, Wiedergabe-Steuerung über die Spotify Web API, die Stimme als Kern-Modul über das separat installierte [Voicebox](https://github.com/jamiepine/voicebox), Nutzerkonten (Anmeldung, Profil auf jedem PC gleich), ein Downloader, m4a und ein Assistent, den man nicht erneut durchklicken muss.

Tests sind eigenständige Skripte und laufen mit einem temporären Datenordner, nie gegen die echten Nutzerdaten. Die komplette Testschleife ist jedes `tests\test_*.py` außer `*_manual.py` (die brauchen echte Audio-Hardware) plus `venv\Scripts\python.exe run.py --selftest`; einzeln z. B.:

```powershell
venv\Scripts\python.exe tests\test_core_logic.py
```

Die manuellen Tests stehen unter `tests\*_manual.py`. Die **Ansichtsmodus-Sperre** („Verwaltung nur im Fenster") muss nicht mehr von Hand geklickt werden: `tests\test_view_mode_manual.py` startet Kern und Server gegen die gebaute Oberfläche und vergleicht beide Rollen (Ansichtslink und Fensterlink) im Browser — die Serverhälfte läuft mit der Schleife.

## Installer bauen

Für die Weitergabe an andere gibt es zusätzlich zur nackten `.exe` einen richtigen Windows-Installer (`RuckusRadioSetup.exe`), gebaut mit [Inno Setup](https://jrsoftware.org/isinfo.php). Er installiert pro Benutzer (kein Admin nötig), legt Start-Menü-/optional Desktop-Verknüpfung an, prüft ob schon ein virtuelles Mikrofon installiert ist — VB-CABLE oder VoiceMeeter (sonst Hinweisseite mit Link zur offiziellen Download-Seite, da beide Downloads ZIPs sind und nicht automatisch entpackt werden können) — und zeigt danach die Einrichtungsschritte aus diesem README noch einmal im Wizard. Die Treiber selbst werden bewusst nicht mitgeliefert: Weitergabe von VB-CABLE und VoiceMeeter braucht eine Lizenzvereinbarung mit VB-Audio.

Voraussetzung: [Inno Setup 6](https://jrsoftware.org/isdl.php) ist installiert (`winget install --id JRSoftware.InnoSetup -e`). Danach:

```powershell
.\build.ps1
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\setup.iss
```

`build.ps1` muss zuerst laufen, damit `dist\RuckusRadio.exe` existiert — der Installer bündelt nur diese eine Datei (ffmpeg steckt bereits im Onefile-Build) plus `assets\icon.ico`. Ergebnis: `installer\Output\RuckusRadioSetup.exe` (nicht eingecheckt, siehe `.gitignore`).

## Update

In den Einstellungen (unten) und im Assistenten gibt es „Nach Updates suchen“. Ruckus Radio fragt dann bei GitHub (`janpinneker/RuckusRadio`) nach der neuesten Version. Ist eine neuere da, fragt es nach, lädt den Installer, prüft seine SHA-256-Prüfsumme, schließt sich, installiert still und startet neu. Ohne Klick prüft Ruckus nie von selbst.

Neue Versionen veröffentlicht `release.ps1 -Version X.Y.Z [-Notes "…"] [-Publish]`: ohne `-Publish` nur bauen, testen und die öffentliche Momentaufnahme vorbereiten; mit `-Publish` hochladen. Das Skript braucht eine lokale, nicht eingecheckte Datei `release-private.txt` (ein privater Suchbegriff pro Zeile) – vor dem Veröffentlichen bricht es ab, wenn einer davon in der Momentaufnahme steht.
