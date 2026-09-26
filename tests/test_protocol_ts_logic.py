"""Die Nachrichtenliste der Web-Oberflaeche (protocol.gen.json) passt zu protocol.py."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-protocol-ts-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import gen_protocol_ts as gen  # noqa: E402
from soundboard import protocol as p  # noqa: E402


def test_file_is_up_to_date():
    on_disk = gen.TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert on_disk == gen.render(), "protocol.gen.json is stale: run venv/Scripts/python.exe tools/gen_protocol_ts.py"
    print("protocol.gen.json matches protocol.py: OK")


def test_every_message_is_listed_once_in_the_right_group():
    data = json.loads(gen.render())
    commands, events = set(data["commands"]), set(data["events"])
    assert not commands & events
    assert commands | events == set(p._REGISTRY)
    assert "Play" in commands and "StopAll" in commands
    assert "Notice" in events and "StateChanged" in events
    print("every message listed once, commands and events split: OK")


def test_fields_carry_type_and_required():
    data = json.loads(gen.render())
    play = data["commands"]["Play"]
    assert play["sound_id"] == {"required": True, "type": "str"}
    assert play["volume"] == {"required": False, "type": "float | None"}
    assert play["preview"] == {"required": False, "type": "bool"}
    assert data["commands"]["StopAll"] == {}
    assert data["commands"]["ExportSounds"]["sound_ids"]["type"] == "tuple[str, ...] | None"
    assert data["events"]["Notice"]["level"] == {"required": False, "type": "str"}
    print("field types and required flags: OK")


def test_header():
    data = json.loads(gen.render())
    assert data["version"] == p.PROTOCOL_VERSION
    assert data["notice_levels"] == list(p.NOTICE_LEVELS)
    assert gen.render().endswith("}\n") and "\r" not in gen.render()
    print("version, notice levels, line endings: OK")


def test_check_mode_detects_drift():
    tmp = Path(_TMP) / "stale.json"
    tmp.write_text("{}\n", encoding="utf-8")
    assert gen.main(["--check", "--target", str(tmp)]) == 1
    assert gen.main(["--target", str(tmp)]) == 0
    assert gen.main(["--check", "--target", str(tmp)]) == 0
    print("--check reports drift, writing fixes it: OK")


def main():
    test_every_message_is_listed_once_in_the_right_group()
    test_fields_carry_type_and_required()
    test_header()
    test_check_mode_detects_drift()
    test_file_is_up_to_date()
    print("\nALL PROTOCOL TS LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
