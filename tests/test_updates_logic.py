"""Updates über GitHub Releases: Kern-Dienst gegen eine Attrappe, ohne Netz."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-updates-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soundboard import protocol as p, updates  # noqa: E402
from soundboard.version import __version__  # noqa: E402
import core_fakes  # noqa: E402


def notices(events, level=None):
    return [n.text for n in core_fakes.of_type(events, p.Notice) if level in (None, n.level)]


def make(release=None, files=None, fail=None):
    source = core_fakes.FakeReleaseSource(release, files, fail)
    c, events = core_fakes.make_core(updates=source)
    c.start()
    return c, events, source


def test_version_compare():
    assert updates.parse_version("v1.2.3") == (1, 2, 3)
    assert updates.parse_version("1.10.0") == (1, 10, 0)
    assert updates.parse_version("1.2") is None and updates.parse_version("vX") is None
    assert updates.is_newer("1.10.0", "1.9.3")
    assert not updates.is_newer("1.1.0", "1.1.0")
    assert not updates.is_newer("garbage", "1.0.0")
    print("versions compare as numbers, unparsable is never newer: OK")


def test_nothing_happens_without_the_button():
    c, events, source = make()
    assert source.latest_calls == 0, "no automatic check at start"
    assert c.state()["updates"] == {"current": __version__, "status": "idle", "latest": None,
                                    "notes": None, "progress": None}
    print("no check until the button is pressed: OK")


def test_current_version_is_reported():
    release, files = core_fakes.fake_release(version=__version__)
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())
    assert updates.UPDATE_CHECKING in notices(events, "hint")
    assert updates.UPDATE_CURRENT.format(version=__version__) in notices(events, "info")
    assert c.state()["updates"]["status"] == "current"
    assert core_fakes.of_type(events, p.UpdateAvailable) == []
    print("same version: 'ist aktuell': OK")


def test_newer_version_asks_then_downloads_and_verifies():
    release, files = core_fakes.fake_release(version="9.0.0", notes="x" * 3000)
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())
    offer = core_fakes.of_type(events, p.UpdateAvailable)
    assert len(offer) == 1 and offer[0].version == "9.0.0"
    assert len(offer[0].notes) == updates.NOTES_MAX, "notes are cut"
    assert c.state()["updates"]["status"] == "available"
    assert source.downloads == [], "nothing downloaded before the user says yes"
    c.send(p.InstallUpdate())
    ready = core_fakes.of_type(events, p.UpdateReady)
    assert len(ready) == 1
    path = Path(ready[0].installer_path)
    assert path.name == "RuckusRadioSetup-9.0.0.exe" and path.read_bytes() == b"setup-bytes"
    assert c.state()["updates"]["status"] == "ready"
    assert any(t.startswith("Update wird geladen") for t in notices(events, "hint"))
    print("newer version: offered, downloaded only after yes, checksum verified: OK")


def test_wrong_checksum_throws_the_file_away():
    release, files = core_fakes.fake_release(version="9.0.0")
    files[core_fakes.RELEASE_BASE + "RuckusRadioSetup.exe"] = b"tampered"
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())
    c.send(p.InstallUpdate())
    assert updates.UPDATE_BROKEN in notices(events, "error")
    assert core_fakes.of_type(events, p.UpdateReady) == []
    assert not list(Path(c.updates.download_dir).glob("RuckusRadioSetup-*.exe"))
    assert c.state()["updates"]["status"] == "error"
    print("a wrong checksum discards the download: OK")


def test_network_and_incomplete_release_errors():
    c, events, source = make(fail=updates.NetworkError("offline"))
    c.send(p.CheckForUpdates())
    assert updates.UPDATE_NO_NETWORK in notices(events, "error")
    assert c.state()["updates"]["status"] == "error"
    release, files = core_fakes.fake_release(version="9.0.0", assets=("RuckusRadioSetup.exe",))
    c2, events2, _ = make(release, files)
    c2.send(p.CheckForUpdates())
    assert updates.UPDATE_INCOMPLETE in notices(events2, "error")
    print("offline and incomplete releases become clear errors: OK")


def test_install_only_after_an_offer_and_only_once():
    c, events, source = make()
    c.send(p.InstallUpdate())
    assert source.downloads == [], "no offer, no download"
    release, files = core_fakes.fake_release(version="9.0.0")
    c2, events2, source2 = make(release, files)
    c2.updates.status = "checking"  # a check is still running
    c2.send(p.CheckForUpdates())
    assert source2.latest_calls == 0, "one operation at a time"
    print("install needs an offer, only one operation at a time: OK")


def test_shutdown_cancels_a_download():
    # Set the stop flag only once the installer download has actually written its
    # first half, so this exercises real mid-download cancellation (not a download
    # that never started).
    release, files = core_fakes.fake_release(version="9.0.0", installer=b"a" * 10)
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())

    def stop_after_first_chunk(done, total):
        c.updates._stop.set()
    c.updates._on_progress = stop_after_first_chunk

    c.send(p.InstallUpdate())
    assert core_fakes.of_type(events, p.UpdateReady) == []
    assert not list(Path(c.updates.download_dir).glob("RuckusRadioSetup-*.exe"))
    print("shutting down cancels a download and removes the part file: OK")


def test_unexpected_error_during_check_frees_the_button():
    c, events, source = make(fail=RuntimeError("boom"))
    c.send(p.CheckForUpdates())
    assert c.state()["updates"]["status"] == "error"
    assert notices(events, "error"), "an error notice was shown"
    source.fail = None
    c.send(p.CheckForUpdates())  # the button must not still be stuck in "checking"
    assert source.latest_calls == 2, "a following check actually ran"
    assert c.state()["updates"]["status"] in ("current", "available")
    print("an unexpected error while checking still frees the button: OK")


def test_unexpected_error_during_install_leaves_no_part_file():
    release, files = core_fakes.fake_release(version="9.0.0")
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())
    original_download = source.download

    def flaky(url, dest, on_progress, should_stop):
        if url.endswith(".sha256"):
            return original_download(url, dest, on_progress, should_stop)
        with open(dest, "wb") as out:  # write a part file, then blow up mid-installer
            out.write(b"partial")
        raise RuntimeError("disk exploded mid-installer")
    source.download = flaky

    c.send(p.InstallUpdate())
    assert core_fakes.of_type(events, p.UpdateReady) == []
    assert c.state()["updates"]["status"] == "error"
    assert not list(Path(c.updates.download_dir).glob("RuckusRadioSetup-*.exe"))
    print("an unexpected error while installing leaves no part file and frees the button: OK")


def test_leftover_installer_from_a_killed_worker_is_swept():
    release, files = core_fakes.fake_release(version="9.0.0")
    c, events, source = make(release, files)
    c.send(p.CheckForUpdates())
    leftover = Path(c.updates.download_dir) / "RuckusRadioSetup-8.0.0.exe"
    leftover.write_bytes(b"stale from a killed worker")

    c.send(p.InstallUpdate())
    assert core_fakes.of_type(events, p.UpdateReady)
    assert not leftover.exists()
    print("a leftover installer from a killed worker is swept before a new install: OK")


def test_non_https_asset_url_is_rejected():
    release, _files = core_fakes.fake_release(version="9.0.0")
    release["assets"][0]["browser_download_url"] = \
        release["assets"][0]["browser_download_url"].replace("https://", "http://")
    try:
        updates.release_info(release)
        assert False, "should have raised IncompleteRelease"
    except updates.IncompleteRelease:
        pass
    print("a non-https asset url is rejected: OK")


def test_github_answer_is_read_correctly():
    release, _files = core_fakes.fake_release(version="2.0.1", notes="Hallo")
    info = updates.release_info(release)
    assert info["version"] == "2.0.1" and info["notes"] == "Hallo"
    assert info["installer_url"].endswith("/RuckusRadioSetup.exe")
    assert info["checksum_url"].endswith("/RuckusRadioSetup.exe.sha256")
    print("GitHub release JSON is parsed: OK")


def main():
    test_version_compare()
    test_nothing_happens_without_the_button()
    test_current_version_is_reported()
    test_newer_version_asks_then_downloads_and_verifies()
    test_wrong_checksum_throws_the_file_away()
    test_network_and_incomplete_release_errors()
    test_install_only_after_an_offer_and_only_once()
    test_shutdown_cancels_a_download()
    test_unexpected_error_during_check_frees_the_button()
    test_unexpected_error_during_install_leaves_no_part_file()
    test_leftover_installer_from_a_killed_worker_is_swept()
    test_non_https_asset_url_is_rejected()
    test_github_answer_is_read_correctly()
    print("\nALL UPDATES LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
