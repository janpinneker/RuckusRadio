"""Dienst-Geheimnisse an einem Ort: nie in der Config, nie im Snapshot, nie im Log."""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-secrets-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402

SECRET = "spotify-refresh-token-abc123"


def test_a_secret_round_trips_and_survives_a_restart():
    c, _ = core_fakes.bare_core()
    assert c.store.secret("spotify") is None
    c.store.set_secret("spotify", SECRET)
    assert c.store.secret("spotify") == SECRET
    again, _ = core_fakes.bare_core()
    # dasselbe Datenverzeichnis: derselbe Store
    assert again.store.secret("spotify") == SECRET
    print("ein Geheimnis ueberlebt den Neustart: OK")


def test_a_secret_is_not_part_of_the_config_or_the_snapshot():
    c, _ = core_fakes.bare_core()
    c.add_state("sounds", lambda: [{"id": "s1"}], group="library")
    c.store.set_secret("spotify", SECRET)
    assert SECRET not in json.dumps(c.store.data, ensure_ascii=False)
    assert SECRET not in json.dumps(c.state(), ensure_ascii=False)
    assert SECRET not in json.dumps(c.get_state(), ensure_ascii=False)
    print("ein Geheimnis steht weder in der Config noch im Snapshot: OK")


def test_a_secret_is_never_logged():
    c, _ = core_fakes.bare_core()
    messages: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage() % record.args if record.args
                            else record.getMessage())

    handler = Keep(level=logging.DEBUG)
    logging.getLogger("soundboard").addHandler(handler)
    try:
        c.store.set_secret("spotify", SECRET)
        c.store.secret("spotify")
        c.store.forget_secret("spotify")
    finally:
        logging.getLogger("soundboard").removeHandler(handler)
    assert all(SECRET not in m for m in messages), messages
    print("ein Geheimnis erscheint in keiner Logzeile: OK")


def test_forgetting_removes_it():
    c, _ = core_fakes.bare_core()
    c.store.set_secret("spotify", SECRET)
    c.store.forget_secret("spotify")
    assert c.store.secret("spotify") is None
    assert SECRET not in c.store.secrets_path.read_text(encoding="utf-8")
    print("vergessen loescht es: OK")


def test_a_broken_file_does_not_break_the_store():
    c, _ = core_fakes.bare_core()
    c.store.secrets_path.write_text("{kein json", encoding="utf-8")
    assert c.store.secret("spotify") is None  # gelesen, geloggt, kein Absturz
    c.store.set_secret("spotify", SECRET)
    assert c.store.secret("spotify") == SECRET
    print("eine kaputte Datei bricht nichts: OK")


def main():
    test_a_secret_round_trips_and_survives_a_restart()
    test_a_secret_is_not_part_of_the_config_or_the_snapshot()
    test_a_secret_is_never_logged()
    test_forgetting_removes_it()
    test_a_broken_file_does_not_break_the_store()
    print("\nALL SECRETS CHECKS PASSED")


if __name__ == "__main__":
    main()
