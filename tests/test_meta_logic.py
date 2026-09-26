"""Logic tests for soundboard/meta.py — no real audio, no real config, just a temp
RUCKUS_DATA_DIR so the meta directory lives somewhere disposable.

Run from the project root with:
    venv/Scripts/python.exe tests/test_meta_logic.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

# Make the package importable when the venv is missing from sys.path (e.g. when the
# test runner is invoked directly instead of through run.py helpers).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from soundboard import meta


def _setup() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ruckus-meta-test-"))
    os.environ["RUCKUS_DATA_DIR"] = str(tmp)
    return tmp


def _write_sound(data_dir: Path, sound_id: str, gain: float, name_override: str | None = None) -> dict:
    meta.save_meta(data_dir, sound_id, {"gain": gain, "name_override": name_override})
    return {"id": sound_id, "gain": gain, "name_override": name_override}


def test_load_meta_unknown_sound_returns_defaults():
    data_dir = _setup()
    sound_id = "missing-sound"

    loaded = meta.load_meta(data_dir, sound_id)

    assert loaded == meta._defaults()
    assert loaded["gain"] == meta.DEFAULT_GAIN
    assert loaded["name_override"] is None


def test_load_meta_existing():
    data_dir = _setup()
    sound_id = "existing-sound"
    _write_sound(data_dir, sound_id, gain=0.9, name_override="Bongos")

    loaded = meta.load_meta(data_dir, sound_id)

    assert loaded["gain"] == 0.9
    assert loaded["name_override"] == "Bongos"


def test_save_meta_is_atomicish_and_writable():
    data_dir = _setup()
    sound_id = "atomic-sound"

    meta.save_meta(data_dir, sound_id, {"gain": 1.4})

    content = (data_dir / "meta" / f"{sound_id}.json").read_text(encoding="utf-8")
    assert '"gain": 1.4' in content
    assert meta.load_meta(data_dir, sound_id)["gain"] == 1.4


def test_save_meta_merges_defaults_with_partial_payload():
    data_dir = _setup()
    sound_id = "partial-sound"

    meta.save_meta(data_dir, sound_id, {"gain": 0.6})

    loaded = meta.load_meta(data_dir, sound_id)
    assert loaded["gain"] == 0.6
    assert "name_override" in loaded


def test_gain_returns_default_for_missing():
    data_dir = _setup()
    sound_id = "missing-gain"

    assert meta.gain(data_dir, sound_id) == meta.DEFAULT_GAIN


def test_gain_returns_persisted_value():
    data_dir = _setup()
    sound_id = "persisted-gain"
    _write_sound(data_dir, sound_id, gain=1.6)

    assert meta.gain(data_dir, sound_id) == 1.6


def test_set_gain_clamps_low():
    data_dir = _setup()
    sound_id = "low-clamp"

    meta.set_gain(data_dir, sound_id, -0.5)

    assert meta.gain(data_dir, sound_id) >= 0.0
    assert meta.gain(data_dir, sound_id) == 0.0


def test_set_gain_clamps_high():
    data_dir = _setup()
    sound_id = "high-clamp"

    meta.set_gain(data_dir, sound_id, 5.0)

    assert meta.gain(data_dir, sound_id) <= meta.GAMEPAD_MAX_GAIN
    assert meta.gain(data_dir, sound_id) == meta.GAMEPAD_MAX_GAIN


def test_set_gain_persists():
    data_dir = _setup()
    sound_id = "persist-gain"

    meta.set_gain(data_dir, sound_id, 1.2)

    assert meta.gain(data_dir, sound_id) == 1.2


def test_set_name_override():
    data_dir = _setup()
    sound_id = "rename-sound"

    meta.set_name_override(data_dir, sound_id, "Snare Hit")

    assert meta.load_meta(data_dir, sound_id)["name_override"] == "Snare Hit"


def test_set_name_override_writes_none_on_empty_string():
    data_dir = _setup()
    sound_id = "unset-name"

    meta.set_name_override(data_dir, sound_id, "   ")

    assert meta.load_meta(data_dir, sound_id)["name_override"] is None


def test_delete_meta_removes_file():
    data_dir = _setup()
    sound_id = "delete-me"
    _write_sound(data_dir, sound_id, gain=1.0)

    assert (data_dir / "meta" / f"{sound_id}.json").exists()
    meta.delete_meta(data_dir, sound_id)

    assert not (data_dir / "meta" / f"{sound_id}.json").exists()
    # After deletion, loading falls back to defaults.
    assert meta.load_meta(data_dir, sound_id) == meta._defaults()


def test_effective_gain_multiplies_base_by_per_sound_gain():
    data_dir = _setup()
    sound_id = "effective-sound"
    _write_sound(data_dir, sound_id, gain=0.75)

    effective = meta.effective_gain(data_dir, sound_id, 0.8)

    # 0.8 * 0.75 = 0.6
    assert math.isclose(effective, 0.6)


def test_effective_gain_clamps_high():
    data_dir = _setup()
    sound_id = "clip-sound"
    _write_sound(data_dir, sound_id, gain=2.0)

    effective = meta.effective_gain(data_dir, sound_id, 1.0)

    assert effective <= 1.0
    assert math.isclose(effective, 1.0)


def test_version_is_semver():
    import re
    from soundboard import version
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__), version.__version__
    print("version is MAJOR.MINOR.PATCH: OK")


def main() -> int:
    try:
        import pytest  # noqa: F401
    except ModuleNotFoundError:
        print("pytest missing — install it in the project venv", file=sys.stderr)
        return 2

    exit_code = pytest.main([__file__, "-q", "--tb=short"])
    if exit_code == 0:
        print("ALL META TESTS PASSED")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
