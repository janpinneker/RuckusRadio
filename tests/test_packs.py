"""soundboard.packs: export/import .ruckuspack — pure logic, no Tk.
Plain script (not pytest): each check prints OK or raises. Uses a temp
RUCKUS_DATA_DIR, never the real %APPDATA%\\Soundboard."""

import json
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-test-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config  # noqa: E402
from soundboard.packs import PackError, export_pack, import_pack, sanitize_filename  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def new_data_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ruckus-data-"))
    (d / "sounds").mkdir()
    (d / "icons").mkdir()
    return d


def make_sound(data_dir: Path, config_data: dict, name: str, with_icon: bool = True,
               volume: float = 1.0) -> dict:
    sound_id = config.new_sound_id()
    audio_dest = data_dir / "sounds" / f"{sound_id}.mp3"
    audio_dest.write_bytes((FIXTURES / "test_tone.mp3").read_bytes())
    icon_dest = data_dir / "icons" / f"{sound_id}.png"
    if with_icon:
        icon_dest.write_bytes((FIXTURES / "test_image.png").read_bytes())
    sound = {
        "id": sound_id,
        "name": name,
        "file": f"sounds/{sound_id}.mp3",
        "icon": f"icons/{sound_id}.png",
        "hotkey": "ctrl+1",  # must NOT survive export/import
        "volume": volume,
    }
    config_data["sounds"].append(sound)
    return sound


def blank_config() -> dict:
    return {"sounds": []}


def _corrupt_crc(zip_path: Path, member_name: str) -> None:
    """Flip a byte inside a member's stored data (zip is opened ZIP_STORED, so
    this directly desyncs the bytes from the member's recorded CRC-32)."""
    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(member_name)
        header_offset = info.header_offset
    with open(zip_path, "r+b") as f:
        f.seek(header_offset)
        header = f.read(30)
        fname_len, extra_len = struct.unpack("<HH", header[26:30])
        data_offset = header_offset + 30 + fname_len + extra_len
        f.seek(data_offset)
        b = f.read(1)
        f.seek(data_offset)
        f.write(bytes([b[0] ^ 0xFF]))


def test_round_trip_export_import():
    src_dir = new_data_dir()
    src_config = blank_config()
    sound = make_sound(src_dir, src_config, "Klaxon", volume=0.7)

    dest_zip = Path(tempfile.mkdtemp()) / "Klaxon.ruckuspack"
    result = export_pack(src_config, src_dir, [sound["id"]], dest_zip)
    assert result == dest_zip
    assert dest_zip.exists()

    dst_dir = new_data_dir()
    imported = import_pack(dest_zip, set(), dst_dir)

    assert len(imported) == 1
    new_sound = imported[0]
    assert new_sound["id"] != sound["id"], "must get a new id"
    assert new_sound["name"] == "Klaxon"
    assert new_sound["volume"] == 0.7
    assert new_sound["hotkey"] is None, "hotkeys are not exported"

    audio_path = dst_dir / new_sound["file"]
    icon_path = dst_dir / new_sound["icon"]
    assert audio_path.exists() and audio_path.read_bytes() == (FIXTURES / "test_tone.mp3").read_bytes()
    assert icon_path.exists()
    print("round trip export/import: OK")


def test_import_pack_does_not_mutate_config_data_or_snapshot():
    """Controller decision: import_pack no longer takes/mutates config_data at
    all — it takes a plain `existing_names` snapshot and must not mutate it
    either, since the caller (GUI) took it on the Tk thread and the worker
    thread must not touch anything live."""
    src_dir = new_data_dir()
    src_config = blank_config()
    sound = make_sound(src_dir, src_config, "Untouched")
    dest_zip = Path(tempfile.mkdtemp()) / "Untouched.ruckuspack"
    export_pack(src_config, src_dir, [sound["id"]], dest_zip)

    dst_dir = new_data_dir()
    snapshot = {"Some Other Sound"}
    snapshot_copy = set(snapshot)
    import_pack(dest_zip, snapshot, dst_dir)
    assert snapshot == snapshot_copy, "must not mutate the caller's existing_names snapshot"
    print("import_pack does not mutate the existing_names snapshot: OK")


def test_import_name_collision_gets_suffix():
    src_dir = new_data_dir()
    src_config = blank_config()
    sound = make_sound(src_dir, src_config, "Airhorn")
    dest_zip = Path(tempfile.mkdtemp()) / "Airhorn.ruckuspack"
    export_pack(src_config, src_dir, [sound["id"]], dest_zip)

    dst_dir = new_data_dir()
    first = import_pack(dest_zip, set(), dst_dir)[0]
    # simulate the caller re-snapshotting names after appending `first` to its live config
    second = import_pack(dest_zip, {first["name"]}, dst_dir)[0]

    assert first["name"] == "Airhorn"
    assert second["name"] == "Airhorn (2)", second["name"]
    print("name collision -> (2): OK")


def test_import_dedups_multiple_new_sounds_against_each_other():
    src_dir = new_data_dir()
    src_config = blank_config()
    s1 = make_sound(src_dir, src_config, "Same Name")
    s2 = make_sound(src_dir, src_config, "Same Name")  # two sounds, same name, on purpose
    # can't export two sounds with the same name via meta.json name collisions in one
    # pack normally (names aren't unique in config either) — build the pack directly
    dest_zip = Path(tempfile.mkdtemp()) / "SameName.ruckuspack"
    export_pack(src_config, src_dir, [s1["id"], s2["id"]], dest_zip)

    dst_dir = new_data_dir()
    imported = import_pack(dest_zip, set(), dst_dir)
    names = sorted(s["name"] for s in imported)
    assert names == ["Same Name", "Same Name (2)"], names
    print("dedup within the same import call: OK")


def test_export_whole_board_multiple_sounds():
    src_dir = new_data_dir()
    src_config = blank_config()
    s1 = make_sound(src_dir, src_config, "Sound A")
    s2 = make_sound(src_dir, src_config, "Sound B")
    dest_zip = Path(tempfile.mkdtemp()) / "Board.ruckuspack"
    export_pack(src_config, src_dir, [s1["id"], s2["id"]], dest_zip)

    dst_dir = new_data_dir()
    imported = import_pack(dest_zip, set(), dst_dir)
    names = sorted(s["name"] for s in imported)
    assert names == ["Sound A", "Sound B"], names
    print("export whole board: OK")


def test_missing_icon_generates_placeholder():
    src_dir = new_data_dir()
    src_config = blank_config()
    sound = make_sound(src_dir, src_config, "No Icon Sound", with_icon=False)
    dest_zip = Path(tempfile.mkdtemp()) / "NoIcon.ruckuspack"
    export_pack(src_config, src_dir, [sound["id"]], dest_zip)

    # confirm no icon.png was written into the pack for this sound's folder
    with zipfile.ZipFile(dest_zip) as zf:
        names = zf.namelist()
        assert not any(n.endswith("icon.png") for n in names), names

    dst_dir = new_data_dir()
    imported = import_pack(dest_zip, set(), dst_dir)
    new_sound = imported[0]
    icon_path = dst_dir / new_sound["icon"]
    assert icon_path.exists(), "placeholder icon must still be generated"
    print("missing icon -> placeholder: OK")


def test_non_zip_file_raises_pack_error():
    bogus = Path(tempfile.mkdtemp()) / "not-a-pack.ruckuspack"
    bogus.write_text("this is definitely not a zip file")
    dst_dir = new_data_dir()
    try:
        import_pack(bogus, set(), dst_dir)
        raise AssertionError("expected PackError")
    except PackError as exc:
        assert "kein Ruckus-Radio-Paket" in str(exc), str(exc)
    assert list((dst_dir / "sounds").iterdir()) == []
    print("non-zip file -> PackError: OK")


def test_zip_without_manifest_raises_pack_error():
    bad_zip = Path(tempfile.mkdtemp()) / "no-manifest.ruckuspack"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("some_folder/meta.json", json.dumps({"name": "x"}))
    dst_dir = new_data_dir()
    try:
        import_pack(bad_zip, set(), dst_dir)
        raise AssertionError("expected PackError")
    except PackError as exc:
        assert "kein Ruckus-Radio-Paket" in str(exc), str(exc)
    print("zip without manifest -> PackError: OK")


def test_zip_slip_entry_raises_and_writes_nothing_outside():
    evil_zip = Path(tempfile.mkdtemp()) / "evil.ruckuspack"
    manifest = {"format": "ruckuspack", "version": 1, "sounds": ["s1"]}
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("s1/meta.json", json.dumps({"name": "Evil", "volume": 1.0, "audio": "../../evil.mp3"}))
        zf.writestr("../evil.mp3", b"payload")

    dst_dir = new_data_dir()
    try:
        import_pack(evil_zip, set(), dst_dir)
        raise AssertionError("expected PackError")
    except PackError as exc:
        assert "beschädigt" in str(exc), str(exc)

    # nothing must have been written outside the data dir
    escaped = dst_dir.parent / "evil.mp3"
    assert not escaped.exists()
    assert list((dst_dir / "sounds").iterdir()) == []
    print("zip-slip entry -> PackError, nothing escapes: OK")


def test_zip_bomb_uncompressed_size_capped():
    bomb_zip = Path(tempfile.mkdtemp()) / "bomb.ruckuspack"
    manifest = {"format": "ruckuspack", "version": 1, "sounds": ["s1"]}
    with zipfile.ZipFile(bomb_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("s1/meta.json", json.dumps({"name": "Bomb", "volume": 1.0, "audio": "sound.mp3"}))
        # highly compressible "fake" huge payload
        zf.writestr("s1/sound.mp3", b"\x00" * (10 * 1024 * 1024), zipfile.ZIP_DEFLATED)

    dst_dir = new_data_dir()
    # sanity check the cap actually triggers by using a tiny cap via monkeypatch
    import soundboard.packs as packs_mod
    original_cap = packs_mod.MAX_UNCOMPRESSED_BYTES
    packs_mod.MAX_UNCOMPRESSED_BYTES = 1024
    try:
        try:
            import_pack(bomb_zip, set(), dst_dir)
            raise AssertionError("expected PackError for oversized pack")
        except PackError as exc:
            assert "beschädigt" in str(exc), str(exc)
    finally:
        packs_mod.MAX_UNCOMPRESSED_BYTES = original_cap
    print("zip bomb size cap: OK")


def test_wrong_audio_extension_rejected():
    bad_zip = Path(tempfile.mkdtemp()) / "wav.ruckuspack"
    manifest = {"format": "ruckuspack", "version": 1, "sounds": ["s1"]}
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("s1/meta.json", json.dumps({"name": "Wav", "volume": 1.0, "audio": "sound.wav"}))
        zf.writestr("s1/sound.wav", b"not really audio")

    dst_dir = new_data_dir()
    try:
        import_pack(bad_zip, set(), dst_dir)
        raise AssertionError("expected PackError")
    except PackError as exc:
        assert "beschädigt" in str(exc), str(exc)
    print("non-mp3 audio extension -> PackError: OK")


def test_corrupt_crc_on_second_of_three_sounds_rolls_back_nothing_written():
    """A bad CRC must be caught in the validation pass (before any writes), so
    a pack where the 2nd of 3 sounds is corrupted must fail cleanly and leave
    zero files behind in sounds/ and icons/."""
    pack = Path(tempfile.mkdtemp()) / "corrupt.ruckuspack"
    manifest = {"format": "ruckuspack", "version": 1, "sounds": ["s1", "s2", "s3"]}
    audio_bytes = (FIXTURES / "test_tone.mp3").read_bytes()
    with zipfile.ZipFile(pack, "w") as zf:  # ZIP_STORED (default) so corruption is deterministic
        zf.writestr("manifest.json", json.dumps(manifest))
        for i, folder in enumerate(["s1", "s2", "s3"], start=1):
            zf.writestr(f"{folder}/meta.json",
                        json.dumps({"name": f"Sound {i}", "volume": 1.0, "audio": "sound.mp3"}))
            zf.writestr(f"{folder}/sound.mp3", audio_bytes)

    _corrupt_crc(pack, "s2/sound.mp3")

    dst_dir = new_data_dir()
    try:
        import_pack(pack, set(), dst_dir)
        raise AssertionError("expected PackError")
    except PackError as exc:
        assert "beschädigt" in str(exc), str(exc)

    assert list((dst_dir / "sounds").iterdir()) == [], "no audio files may remain"
    assert list((dst_dir / "icons").iterdir()) == [], "no icon files may remain"
    print("corrupt CRC on 2nd of 3 sounds -> PackError, no files left: OK")


def test_sanitize_filename_strips_windows_illegal_characters():
    assert sanitize_filename("Was?!") == "Was!"
    assert sanitize_filename('Say "hi" <now>') == "Say hi now"
    assert sanitize_filename("a/b\\c:d*e?f\"g<h>i|j") == "abcdefghij"
    print("sanitize_filename strips illegal characters: OK")


def test_sanitize_filename_collapses_whitespace_and_strips_trailing_dots():
    assert sanitize_filename("Alarm:  Warnung!") == "Alarm Warnung!"
    assert sanitize_filename("Trailing dots...") == "Trailing dots"
    assert sanitize_filename("  padded  ") == "padded"
    print("sanitize_filename collapses whitespace / strips trailing dots: OK")


def test_sanitize_filename_falls_back_to_sound_when_empty():
    assert sanitize_filename("???") == "Sound"
    assert sanitize_filename("") == "Sound"
    assert sanitize_filename("   ") == "Sound"
    print("sanitize_filename falls back to 'Sound': OK")


def _pack_with_metas(metas: list[dict], icon_bytes: bytes | None = None) -> Path:
    """Hand-built pack: one folder per meta dict (audio = the test tone)."""
    pack = Path(tempfile.mkdtemp(prefix="ruckus-pack-")) / "hand.ruckuspack"
    tone = (FIXTURES / "test_tone.mp3").read_bytes()
    folders = [f"f{i}" for i in range(len(metas))]
    with zipfile.ZipFile(pack, "w") as zf:
        for folder, meta in zip(folders, metas):
            zf.writestr(f"{folder}/sound.mp3", tone)
            zf.writestr(f"{folder}/meta.json", json.dumps({"audio": "sound.mp3", **meta}))
            if icon_bytes is not None:
                zf.writestr(f"{folder}/icon.png", icon_bytes)
        zf.writestr("manifest.json", json.dumps({"format": "ruckuspack", "version": 1, "sounds": folders}))
    return pack


def test_clamp_volume():
    assert config.MAX_SOUND_VOLUME == 1.5
    assert config.clamp_volume(1e9) == 1.5
    assert config.clamp_volume(float("nan")) == 1.0
    assert config.clamp_volume(float("inf")) == 1.0
    assert config.clamp_volume(-5) == 0.0
    assert config.clamp_volume("abc") == 1.0
    assert config.clamp_volume(None) == 1.0
    assert config.clamp_volume("0.7") == 0.7
    assert config.clamp_volume(1.2) == 1.2
    print("clamp_volume: OK")


def test_import_clamps_volume():
    metas = [{"name": "Laut", "volume": 1e9}, {"name": "Nan", "volume": float("nan")},
             {"name": "Neg", "volume": -5}, {"name": "Text", "volume": "abc"}]
    new = import_pack(_pack_with_metas(metas), set(), new_data_dir())
    assert [s["volume"] for s in new] == [1.5, 1.0, 0.0, 1.0], [s["volume"] for s in new]
    print("import clamps volume (1e9, NaN, -5, 'abc'): OK")


def test_import_icon_saved_at_icon_size():
    import io

    from PIL import Image

    from soundboard.icons import ICON_SIZE

    buf = io.BytesIO()
    Image.new("RGB", (900, 600), "#22D3EE").save(buf, format="PNG")
    data_dir = new_data_dir()
    new = import_pack(_pack_with_metas([{"name": "Riesig"}], icon_bytes=buf.getvalue()), set(), data_dir)
    with Image.open(data_dir / new[0]["icon"]) as img:
        assert img.size == (ICON_SIZE, ICON_SIZE), img.size
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0, "corner must be transparent (circle crop)"
    print("imported icon cropped to ICON_SIZE circle: OK")


def test_export_missing_audio_removes_partial_pack():
    src_dir = new_data_dir()
    src_config = blank_config()
    ok = make_sound(src_dir, src_config, "Da")
    gone = make_sound(src_dir, src_config, "Weg")
    (src_dir / gone["file"]).unlink()
    dest = Path(tempfile.mkdtemp()) / "partial.ruckuspack"
    try:
        export_pack(src_config, src_dir, [ok["id"], gone["id"]], dest)
    except PackError as exc:
        assert str(exc) == "Die Audiodatei von „Weg“ fehlt.", str(exc)
    else:
        raise AssertionError("expected PackError for missing audio")
    assert not dest.exists(), "partial .ruckuspack must be deleted"
    print("export with missing audio -> PackError + no partial file: OK")


if __name__ == "__main__":
    test_clamp_volume()
    test_import_clamps_volume()
    test_import_icon_saved_at_icon_size()
    test_export_missing_audio_removes_partial_pack()
    test_round_trip_export_import()
    test_import_pack_does_not_mutate_config_data_or_snapshot()
    test_import_name_collision_gets_suffix()
    test_import_dedups_multiple_new_sounds_against_each_other()
    test_export_whole_board_multiple_sounds()
    test_missing_icon_generates_placeholder()
    test_non_zip_file_raises_pack_error()
    test_zip_without_manifest_raises_pack_error()
    test_zip_slip_entry_raises_and_writes_nothing_outside()
    test_zip_bomb_uncompressed_size_capped()
    test_wrong_audio_extension_rejected()
    test_corrupt_crc_on_second_of_three_sounds_rolls_back_nothing_written()
    test_sanitize_filename_strips_windows_illegal_characters()
    test_sanitize_filename_collapses_whitespace_and_strips_trailing_dots()
    test_sanitize_filename_falls_back_to_sound_when_empty()
    print("ALL PACKS TESTS PASSED")
