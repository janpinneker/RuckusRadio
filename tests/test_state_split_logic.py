"""Gruppen statt eines großen Snapshots; Zusammenfassen wird deklariert."""

import os
import socket
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-statesplit-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import core_fakes  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def test_groups_are_read_apart():
    c, _ = core_fakes.bare_core()
    c.add_state("klein", lambda: {"n": 1})
    c.add_state("gross", lambda: {"sounds": [1, 2]}, group="library")
    assert set(c.state()) == {"protocol", "klein", "gross"}
    assert c.state("volatile") == {"protocol": p.PROTOCOL_VERSION, "klein": {"n": 1}}
    assert c.state("library") == {"protocol": p.PROTOCOL_VERSION, "gross": {"sounds": [1, 2]}}
    print("Gruppen lassen sich einzeln lesen: OK")


def test_an_empty_group_is_refused():
    """Gruppennamen sind frei (Musik und Stimme legen später eigene an), aber nicht leer:
    ein leerer Name wäre kein Name. `changed("music")` darf dagegen eine Gruppe nennen,
    die noch keine Teile hat."""
    c, _ = core_fakes.bare_core()
    try:
        c.add_state("x", lambda: 1, group="")
    except ValueError:
        pass
    else:
        raise AssertionError("eine leere Gruppe muss abgelehnt werden")
    print("leere Gruppe wird abgelehnt: OK")


def test_changed_names_the_group():
    c, events = core_fakes.bare_core()
    c.add_state("gross", lambda: {"sounds": [1]}, group="library")
    c.changed("library")
    c.changed("music")  # eine Gruppe ohne Teile ist erlaubt und schickt eine leere Menge
    found = core_fakes.of_type(events, p.PartChanged)
    assert [e.group for e in found] == ["library", "music"], found
    assert found[0].part == {"protocol": p.PROTOCOL_VERSION, "gross": {"sounds": [1]}}
    assert found[1].part == {"protocol": p.PROTOCOL_VERSION}
    print("changed(group) nennt die Gruppe: OK")


def test_coalescing_is_declared_on_the_event():
    assert p.coalesce_key({"type": "StateChanged", "data": {"state": {}}}) == "StateChanged"
    assert p.coalesce_key({"type": "PartChanged", "data": {"group": "library", "part": {}}}) \
        == "PartChanged:library"
    assert p.coalesce_key({"type": "PartChanged", "data": {"group": "music", "part": {}}}) \
        == "PartChanged:music"
    assert p.coalesce_key({"type": "SoundAdded", "data": {"sound_id": "a"}}) is None
    assert p.coalesce_key({"type": "Nope", "data": {}}) is None
    print("Zusammenfassen wird je Ereignis deklariert: OK")


def test_coalescing_can_look_inside_a_payload():
    """`JobChanged.coalesce_by = ("id",)` meint das Feld im Job, nicht auf oberster Ebene:
    sonst wuerde jeder Jobfortschritt denselben Schluessel treffen und fremde Jobs loeschen."""
    key = p.coalesce_key({"type": "JobChanged", "data": {"job": {"id": "a"}}})
    assert key == "JobChanged:a", key
    other = p.coalesce_key({"type": "JobChanged", "data": {"job": {"id": "b"}}})
    assert other == "JobChanged:b", other
    assert p.coalesce_key({"type": "JobChanged", "data": {}}) == "JobChanged:None"
    # Ein Feld oben hat weiter Vorrang (PartChanged) und darf nicht durch "job" ersetzt werden.
    assert p.coalesce_key({"type": "PartChanged", "data": {"group": "library"}}) \
        == "PartChanged:library"
    print("Zusammenfassen findet ein Feld auch im Inneren: OK")


def test_a_state_part_must_not_do_io():
    """Zustandsteile laufen auf dem Kern-Thread und dürfen dort nicht blockieren."""
    c, _ = core_fakes.bare_core()
    c.add_state("gross", lambda: {"sounds": [1]}, group="library")

    class NoSockets:
        def __getattr__(self, name):
            raise AssertionError("ein Zustandsteil darf keinen Socket öffnen")

    original = socket.socket
    socket.socket = NoSockets()  # type: ignore[assignment]
    try:
        assert c.state()["gross"] == {"sounds": [1]}
    finally:
        socket.socket = original
    print("state() kommt ohne Netz aus: OK")


def main():
    test_groups_are_read_apart()
    test_an_empty_group_is_refused()
    test_changed_names_the_group()
    test_coalescing_is_declared_on_the_event()
    test_coalescing_can_look_inside_a_payload()
    test_a_state_part_must_not_do_io()
    print("\nALL STATE SPLIT CHECKS PASSED")


if __name__ == "__main__":
    main()
