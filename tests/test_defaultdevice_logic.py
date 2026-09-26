"""Windows-Standardgeraet: Wechsel-Erkennung rein logisch, COM-Abfrage nur lesend."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-defaultdevice-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import defaultdevice  # noqa: E402


def test_watcher_reports_a_change_until_done():
    values = iter(["A", "A", "B", "B", "B"])
    watcher = defaultdevice.DefaultDeviceWatcher(lambda: next(values))
    assert watcher.poll() is False, "same device: nothing to do"
    assert watcher.poll() is True, "changed to B"
    assert watcher.poll() is True, "still waiting to be taken over"
    watcher.done()
    assert watcher.poll() is False, "B is the known device now"
    print("watcher reports a change until done(): OK")


def test_watcher_ignores_failed_reads():
    values = iter(["A", None, "A"])
    watcher = defaultdevice.DefaultDeviceWatcher(lambda: next(values))
    assert watcher.poll() is False, "a failed COM read is not a device change"
    assert watcher.poll() is False
    print("watcher ignores failed reads: OK")


def test_real_default_render_id_is_readable():
    value = defaultdevice.default_render_id()
    assert value is None or value.startswith("{"), value
    print("default render endpoint id:", value)


def test_null_device_is_none_not_a_crash():
    """GetDefaultAudioEndpoint leaving device NULL must return None, not raise ValueError."""
    original_method = defaultdevice._method

    def patched_method(obj, index, *argtypes):
        # Return a no-op callable for GetDefaultAudioEndpoint (index 4)
        # which leaves device NULL, causing the subsequent _method call to fail
        if index == defaultdevice.VTBL_GET_DEFAULT_AUDIO_ENDPOINT:
            def noop(*args, **kwargs):
                return 0  # S_OK, but device stays NULL
            return noop
        return original_method(obj, index, *argtypes)

    try:
        defaultdevice._method = patched_method
        # This should return None instead of raising ValueError
        result = defaultdevice.default_render_id()
        assert result is None, f"Expected None, got {result}"
        print("a NULL default device returns None: OK")
    finally:
        defaultdevice._method = original_method


def test_observe_detects_changes_without_reading():
    watcher = defaultdevice.DefaultDeviceWatcher(initial="A")
    assert watcher.observe("A") is False
    assert watcher.observe(None) is False, "a failed read is not a change"
    assert watcher.observe("B") is True
    assert watcher.observe("B") is True, "stays pending until done()"
    watcher.done()
    assert watcher.observe("B") is False
    print("observe() detects a change without reading itself: OK")


def main():
    test_watcher_reports_a_change_until_done()
    test_watcher_ignores_failed_reads()
    test_real_default_render_id_is_readable()
    test_null_device_is_none_not_a_crash()
    test_observe_detects_changes_without_reading()
    print("\nALL DEFAULTDEVICE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
