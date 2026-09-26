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
4. Die **Kopfhörer**-Zeile ganz unten regelt nur dein eigenes Mithören, unabhängig von den Kabeln — schaltest du dort Sounds aus, hörst du deine eigenen Sounds nicht mehr mit, ohne dass CS oder Discord etwas davon merken.

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

**Alle stoppen**: Der orange Button unten rechts bricht sofort jede laufende Wiedergabe ab — für Panik-Momente. Ein globaler Hotkey (Standard `Strg+Alt+Rücktaste`) macht dasselbe, auch außerhalb der App.

**Mithören**: Wie laut du deine eigenen Sounds selbst hörst, stellst du auf der Seite **Einstellungen** in der **Kopfhörer**-Zeile ein (unabhängig von der Lautstärke, die bei Discord/Steam ankommt — Standard 50 %).

**Statusleiste unten**: Links steht, welches virtuelle Mikrofon läuft. Rechts daneben:

- **Discord: Sounds an/aus** — schaltet nur die Sounds im Discord-Kabel stumm; dein Mikro bleibt in Discord hörbar, CS/Steam hören die Sounds weiter.
- **Prüfen** — schickt einen Ton durch das virtuelle Mikrofon und misst am anderen Ende, ob er ankommt. Das ist der einzige Test, der ohne Mitspieler beweist, dass die Gegenstelle dich hört. Schlägt er fehl, zeigt die Statusleiste „gefunden, aber kein Signal — prüfen" statt grün.
- **Mikro an/aus** — nur im VB-CABLE-Modus: schaltet dein Mikrofon stumm, die Sounds laufen weiter.

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

## Weiterentwicklung (Stand 2026-09-25)

Geplant sind drei Bereiche in einer neuen, Spotify-artigen Oberfläche: **Soundboard** (wie heute), **Musik** (Bibliothek, Playlists, Wiedergabe in den Mixer) und **Blackbox** (Sprach-Studio über das separat installierte [Voicebox](https://github.com/jamiepine/voicebox)), dazu ein **Zuschnitt-Editor** für Sounds und Musik. Dafür liegt die Programmlogik seit 2026-09-25 in einem eigenen App-Kern ohne Oberfläche; die heutige Oberfläche zeigt nur noch an und schickt Befehle – an der Bedienung ändert sich dadurch nichts. Neu ist nur: Ruckus Radio startet höchstens einmal (ein zweiter Start meldet „läuft schon“), und es kann sich selbst aktualisieren (Abschnitt „Update“).

Tests sind eigenständige Skripte und laufen mit einem temporären Datenordner, nie gegen die echten Nutzerdaten. Die komplette Testschleife ist jedes `tests\test_*.py` außer `*_manual.py` (die brauchen echte Audio-Hardware) plus `venv\Scripts\python.exe run.py --selftest`; einzeln z. B.:

```powershell
venv\Scripts\python.exe tests\test_core_logic.py
```

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
