"""webmain's UpdateReady handling: starts the installer, then closes the host - the
same flow as the Tk window's launch_update/UpdateReady branch, but without pywebview
(make_update_handler is factored exactly so this test does not need it)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-webmain-update-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soundboard import protocol as p, updates, webmain  # noqa: E402
import core_fakes  # noqa: E402


def notices(events, level=None):
    return [n.text for n in core_fakes.of_type(events, p.Notice) if level in (None, n.level)]


def test_update_ready_launches_the_installer_and_closes_the_host():
    c, events = core_fakes.make_core()
    closed: list[bool] = []
    launched: list[str] = []
    handler = webmain.make_update_handler(c, lambda path: launched.append(path) or True, lambda: closed.append(True))
    c.subscribe(handler)
    c.emit(p.UpdateReady("C:/tmp/RuckusRadioSetup-9.0.0.exe"))
    assert launched == ["C:/tmp/RuckusRadioSetup-9.0.0.exe"]
    assert closed == [True]
    assert notices(events, "error") == []
    print("UpdateReady starts the installer and closes the host on success: OK")


def test_update_ready_notices_on_a_failed_launch_and_stays_open():
    c, events = core_fakes.make_core()
    closed: list[bool] = []
    handler = webmain.make_update_handler(c, lambda path: False, lambda: closed.append(True))
    c.subscribe(handler)
    c.emit(p.UpdateReady("C:/tmp/x.exe"))
    assert closed == [], "a failed launch must not close the host"
    assert notices(events, "error") == [updates.UPDATE_LAUNCH_FAILED.format(path="C:/tmp/x.exe")]
    print("a failed launch notices the error and keeps the host open: OK")


def test_other_events_are_ignored():
    c, events = core_fakes.make_core()
    calls: list[str] = []
    handler = webmain.make_update_handler(c, lambda path: calls.append(path) or True, lambda: calls.append("closed"))
    c.subscribe(handler)
    c.emit(p.Notice("just a hint", "hint"))
    c.emit(p.UpdateAvailable("9.0.0", "notes"))
    assert calls == [], "only UpdateReady triggers the launcher"
    print("every other event is ignored: OK")


def main():
    test_update_ready_launches_the_installer_and_closes_the_host()
    test_update_ready_notices_on_a_failed_launch_and_stays_open()
    test_other_events_are_ignored()
    print("\nALL WEBMAIN UPDATE HANDLING CHECKS PASSED")


if __name__ == "__main__":
    main()
