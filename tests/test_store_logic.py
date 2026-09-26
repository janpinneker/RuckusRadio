"""Speicher: entprelltes Speichern, sofortiges Speichern, Fehlerhaken, Backup."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-store-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, executors, store  # noqa: E402
import logging  # noqa: E402

# The failure test logs on purpose; keep the test output free of tracebacks.
logging.getLogger("soundboard").addHandler(logging.NullHandler())


def on_disk():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_loads_the_config_when_no_data_is_given():
    s = store.Store(executors.InlineExecutor())
    assert s.data["version"] == config.CONFIG_VERSION
    assert s.data_dir == Path(_TMP)
    assert s.was_reset is False
    print("store loads config.json by itself: OK")


def test_save_soon_is_debounced():
    ex = executors.InlineExecutor()
    s = store.Store(ex, data=config._default_config())
    s.save_now()
    s.data["autostart"] = True
    s.save_soon()
    s.data["stop_all_hotkey"] = "f9"
    s.save_soon()
    assert on_disk()["autostart"] is False, "nothing is written before the debounce"
    ex.advance(store.SAVE_DEBOUNCE_S - 0.01)
    assert on_disk()["autostart"] is False
    ex.advance(0.02)
    assert on_disk()["autostart"] is True and on_disk()["stop_all_hotkey"] == "f9"
    assert ex.pending_timers() == 0, "two save_soon calls leave one write"
    print("save_soon writes once after the debounce: OK")


def test_save_now_and_flush_cancel_the_pending_write():
    ex = executors.InlineExecutor()
    s = store.Store(ex, data=config._default_config())
    s.data["autostart"] = True
    s.save_soon()
    assert s.save_now() is True
    assert on_disk()["autostart"] is True
    assert ex.pending_timers() == 0
    s.data["autostart"] = False
    s.save_soon()
    s.flush()
    assert on_disk()["autostart"] is False and ex.pending_timers() == 0
    s.flush()  # nothing pending: no error, no write needed
    print("save_now and flush write immediately and cancel the timer: OK")


def test_a_failed_save_calls_the_hook_and_keeps_the_file():
    s = store.Store(executors.InlineExecutor(), data=config._default_config())
    s.save_now()
    failures = []
    s.on_save_failed = failures.append
    original = config.save_config

    def broken(_data):
        raise OSError("disk full")

    config.save_config = broken
    try:
        s.data["autostart"] = True
        assert s.save_now() is False
    finally:
        config.save_config = original
    assert len(failures) == 1 and "disk full" in str(failures[0])
    assert on_disk()["autostart"] is False, "the old file stays"
    print("a failed save reports and leaves the old file: OK")


def test_backup_copies_the_config_aside():
    s = store.Store(executors.InlineExecutor(), data=config._default_config())
    s.save_now()
    dest = s.backup("v3")
    assert dest is not None and dest.exists() and dest.name.startswith("config.json.v3-")
    assert json.loads(dest.read_text(encoding="utf-8")) == on_disk()
    config.config_path().unlink()
    assert s.backup("v3") is None, "no config.json: nothing to back up"
    s.save_now()
    print("backup copies config.json aside: OK")


def main():
    test_loads_the_config_when_no_data_is_given()
    test_save_soon_is_debounced()
    test_save_now_and_flush_cancel_the_pending_write()
    test_a_failed_save_calls_the_hook_and_keeps_the_file()
    test_backup_copies_the_config_aside()
    print("\nALL STORE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
