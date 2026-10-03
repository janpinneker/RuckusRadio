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
    assert SECRET.encode("utf-8") not in c.store.secrets_path.read_bytes()
    print("vergessen loescht es: OK")


def test_a_broken_file_does_not_break_the_store():
    c, _ = core_fakes.bare_core()
    c.store.secrets_path.write_text("{kein json", encoding="utf-8")
    assert c.store.secret("spotify") is None  # gelesen, geloggt, kein Absturz
    c.store.set_secret("spotify", SECRET)
    assert c.store.secret("spotify") == SECRET
    print("eine kaputte Datei bricht nichts: OK")


def test_the_file_on_disk_is_encrypted():
    """Eng review 2026-10-03, finding 4: the refresh token stood in clear text in
    secrets.json, readable by any program of the user. Windows DPAPI binds it to the
    Windows account; no new dependency."""
    c, _ = core_fakes.bare_core()
    c.store.set_secret("spotify", SECRET)
    raw = c.store.secrets_path.read_bytes()
    assert SECRET.encode("utf-8") not in raw, "the token stands in clear text"
    assert b"spotify" not in raw, "even the names stay hidden"
    assert not c.store.legacy_secrets_path.exists()
    print("die Datei auf der Platte ist verschluesselt: OK")


def test_an_old_clear_text_file_is_taken_over_and_removed():
    """A user updating from 1.4 has secrets.json in clear text: the login must survive,
    and the clear-text file must go."""
    c, _ = core_fakes.bare_core()
    c.store.forget_secret("spotify")
    if c.store.secrets_path.exists():
        c.store.secrets_path.unlink()
    c.store.legacy_secrets_path.write_text(json.dumps({"spotify": SECRET}), encoding="utf-8")
    again, _ = core_fakes.bare_core()
    assert again.store.secret("spotify") == SECRET
    assert not again.store.legacy_secrets_path.exists(), "clear text must not stay behind"
    assert SECRET.encode("utf-8") not in again.store.secrets_path.read_bytes()
    third, _ = core_fakes.bare_core()
    assert third.store.secret("spotify") == SECRET
    print("eine alte Klartext-Datei wird uebernommen und geloescht: OK")


def test_a_clear_text_file_written_later_by_an_old_build_wins():
    """An older Ruckus still running next to the new files writes secrets.json again
    (a refreshed token). That file is newer than the encrypted one, so it wins."""
    c, _ = core_fakes.bare_core()
    c.store.set_secret("spotify", "alt")
    c.store.legacy_secrets_path.write_text(json.dumps({"spotify": SECRET}), encoding="utf-8")
    again, _ = core_fakes.bare_core()
    assert again.store.secret("spotify") == SECRET
    assert not again.store.legacy_secrets_path.exists()
    print("eine spaeter geschriebene Klartext-Datei gewinnt: OK")


def test_the_clear_text_file_stays_when_the_takeover_cannot_write():
    """Nachtlauf B1: an older secrets.dat exists, writing the taken-over secrets fails -
    the old secrets.dat is still there, so the takeover counted as done and deleted
    secrets.json. The next start then read the stale token. The clear-text file must
    stay until its content is really stored."""
    from soundboard import dpapi
    c, _ = core_fakes.bare_core()
    c.store.set_secret("spotify", "alt")
    c.store.legacy_secrets_path.write_text(json.dumps({"spotify": SECRET}), encoding="utf-8")
    real = dpapi.protect

    def broken(_data):
        raise OSError("disk full")

    dpapi.protect = broken
    try:
        again, _ = core_fakes.bare_core()
        assert again.store.secret("spotify") == SECRET
    finally:
        dpapi.protect = real
    assert again.store.legacy_secrets_path.exists(), "nothing stored yet: keep the clear text"
    third, _ = core_fakes.bare_core()
    assert third.store.secret("spotify") == SECRET
    assert not third.store.legacy_secrets_path.exists()
    print("die Klartext-Datei bleibt, wenn die Uebernahme nicht schreiben kann: OK")


def main():
    test_a_secret_round_trips_and_survives_a_restart()
    test_a_secret_is_not_part_of_the_config_or_the_snapshot()
    test_a_secret_is_never_logged()
    test_forgetting_removes_it()
    test_a_broken_file_does_not_break_the_store()
    test_the_file_on_disk_is_encrypted()
    test_an_old_clear_text_file_is_taken_over_and_removed()
    test_a_clear_text_file_written_later_by_an_old_build_wins()
    test_the_clear_text_file_stays_when_the_takeover_cannot_write()
    print("\nALL SECRETS CHECKS PASSED")


if __name__ == "__main__":
    main()
