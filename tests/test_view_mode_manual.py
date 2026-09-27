"""Manueller Test der Ansichtsmodus-Sperre: beide Rollen im Browser, gegen die GEBAUTE Seite.

    venv\\Scripts\\python.exe tests\\test_view_mode_manual.py
    venv\\Scripts\\python.exe tests\\test_view_mode_manual.py --urls-only   # nur die beiden Links

Warum manuell: der Lauf braucht echte Audiogeraete (der Kern wird normal gestartet) und
einen Browser-Treiber - dieselbe Klasse wie die uebrigen ``*_manual.py``, deshalb steht
dieser Test nicht in der gruenen Schleife.

Er ist die **Browser-Haelfte** der Sperre „Verwaltung nur im Fenster": der Server-Teil
laeuft erschoepfend in der gruenen Schleife
(``tests/test_webserver_logic.py::test_no_command_beyond_playback_and_music_reaches_the_view``
faehrt jeden Befehl aus ``access.COMMAND_CAPABILITY``), aber was der Server nicht sehen
kann, ist die **Darstellung** - genau dort fielen „Zu Favoriten" und der Stern auf der
Karte durch. Deshalb faehrt dieses Skript den echten Kern und den echten Server gegen
``webui/dist`` und vergleicht in beiden Rollen elf Zeilen (Rolle, Hinweis, Kartensterne,
Dock, Einstellungen, Kontextmenue, Musik-Bus).

Vorher die Web-Oberflaeche bauen (``cd webui && npm run build``) und Ruckus Radio
schliessen. Daten sind eine Kopie in ``%TEMP%``; ``%APPDATA%\\Soundboard`` bleibt
unberuehrt. Exit-Code: 0 = wie erwartet, 1 = Abweichung, 2 = kein Browser-Treiber.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.check_view_mode import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
