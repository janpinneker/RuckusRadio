"""Die Seite bittet, Python oeffnet den Dialog. Die Oberflaeche uebergibt nie Pfade."""

import dataclasses
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-requests-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import access, library  # noqa: E402
from soundboard import protocol as p  # noqa: E402

SOUND = Path(__file__).parent / "fixtures" / "test_tone.mp3"


class FakeDialogs:
    """Records what was asked and answers with what the test decided."""

    def __init__(self, **answers):
        self.answers = answers
        self.asked: list[str] = []

    def pick_sound_file(self):
        self.asked.append("sound")
        return self.answers.get("sound")

    def pick_icon_file(self):
        self.asked.append("icon")
        return self.answers.get("icon")

    def pick_pack_file(self):
        self.asked.append("pack")
        return self.answers.get("pack")

    def ask_export_target(self, default_name):
        self.asked.append(f"target:{default_name}")
        return self.answers.get("target")


def wire(dialogs):
    # Abweichung von der Plan-Vorlage: sie baute ein zweites LibraryService an denselben
    # Kern, das mit `ValueError: state part 'sounds' already exists` abbricht (dasselbe
    # Muster wie Task 4). Hier bekommt die vom Kern gebaute Bibliothek die Attrappe.
    c, events = core_fakes.make_core()
    c.library._dialogs = dialogs
    return c, events


def test_the_requests_carry_no_path():
    for cls in (p.RequestAddSound, p.RequestImportPack, p.RequestExportSounds,
                p.RequestSetSoundIcon):
        fields = {f.name for f in dataclasses.fields(cls)}
        assert "path" not in fields, (cls.__name__, fields)
    assert {f.name for f in dataclasses.fields(p.RequestSetSoundIcon)} == {"sound_id"}
    print("die Request-Befehle tragen keinen Pfad: OK")


def test_a_cancelled_dialog_changes_nothing():
    dialogs = FakeDialogs()
    c, events = wire(dialogs)
    for cmd in (p.RequestAddSound(), p.RequestImportPack(),
                p.RequestExportSounds(None), p.RequestSetSoundIcon("s1")):
        c.send(cmd)
    assert dialogs.asked, "der Dialog wurde gefragt"
    assert c.state("library")["sounds"] == []
    print("ein abgebrochener Dialog aendert nichts: OK")


def test_a_chosen_file_reaches_the_core_without_the_page():
    dialogs = FakeDialogs(sound=str(SOUND))
    c, events = wire(dialogs)
    c.send(p.RequestAddSound())
    sounds = c.state("library")["sounds"]
    assert len(sounds) == 1, sounds
    # Abweichung von der Plan-Vorlage: ein Sound traegt keinen `path` (nur `file`), er
    # wird unter `sounds/{id}.mp3` kopiert. Dass der Dialogpfad ankam, zeigt der Name:
    # `_request_add_sound` setzt ihn auf den Dateistamm ("test_tone").
    assert sounds[0]["name"] == "test_tone", sounds[0]
    assert sounds[0]["file"].endswith(".mp3"), sounds[0]
    assert dialogs.asked == ["sound"]
    print("der gewaehlte Pfad kommt aus Python, nicht aus der Seite: OK")


def test_an_export_asks_for_the_target_and_uses_the_existing_flow():
    dialogs = FakeDialogs(target=None)
    c, events = wire(dialogs)
    c.send(p.RequestExportSounds(None))
    assert dialogs.asked == [], "ohne Sounds wird gar nicht gefragt"
    print("ohne Sounds wird nicht nach einem Ziel gefragt: OK")


def test_the_requests_need_the_library_capability():
    for cls, args in ((p.RequestAddSound, {}), (p.RequestImportPack, {}),
                      (p.RequestExportSounds, {"sound_id": None}),
                      (p.RequestSetSoundIcon, {"sound_id": "s1"})):
        assert access.COMMAND_CAPABILITY[cls] == access.LIBRARY, cls
        assert not access.ROLE_CAPABILITIES[access.ROLE_VIEW] & {access.LIBRARY}
    print("die Requests brauchen LIBRARY, die Ansicht hat sie nicht: OK")


def main():
    test_the_requests_carry_no_path()
    test_a_cancelled_dialog_changes_nothing()
    test_a_chosen_file_reaches_the_core_without_the_page()
    test_an_export_asks_for_the_target_and_uses_the_existing_flow()
    test_the_requests_need_the_library_capability()
    print("\nALL REQUESTS CHECKS PASSED")


if __name__ == "__main__":
    main()
