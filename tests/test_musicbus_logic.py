"""Musik-Bus ohne Geraete: Format-Lesen, Dekodieren, Nachtaasten, Pufferlogik.

Der wichtigste Test ist `test_float_extensible_survives_the_padding_trap`: er
sichert die Falle aus `tests/test_loopback_manual.py`, die schon einen Messlauf
gekostet hat. Eine `ctypes.Structure` fuer WAVEFORMATEXTENSIBLE richtet aus und
schiebt hinter den 18 Byte zwei Byte Padding ein; dann liest `SubFormat.Data1`
den Kanal-Mask (3 fuer Stereo) statt des Formats - und weil 3 zufaellig
`WAVE_FORMAT_IEEE_FLOAT` ist, wird PCM still als Float gedeutet. Gelesen wird
deshalb nur ueber feste Offsets.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-musicbus-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.getLogger("soundboard").addHandler(logging.NullHandler())

import struct  # noqa: E402

import numpy as np  # noqa: E402

import core_fakes  # noqa: E402
from soundboard import config  # noqa: E402
from soundboard import musicbus  # noqa: E402
from soundboard import protocol as p  # noqa: E402
from soundboard import virtualmic  # noqa: E402


def waveformat_bytes(tag, channels, rate, bits, align, *, valid_bits=None,
                     mask=None, subformat=None):
    """Ein WAVEFORMATEX(-EXTENSIBLE) so auf dem Draht, wie Windows ihn liefert."""
    buf = bytearray(40)
    struct.pack_into("<HHIIHHH", buf, 0, tag, channels, rate, rate * align, align, bits, 22)
    if tag == musicbus.WAVE_FORMAT_EXTENSIBLE:
        struct.pack_into("<H", buf, musicbus._OFF_VALID_BITS,
                         valid_bits if valid_bits is not None else bits)
        struct.pack_into("<I", buf, musicbus._OFF_CHANNEL_MASK, mask if mask is not None else 3)
        struct.pack_into("<I", buf, musicbus._OFF_SUBFORMAT,
                         subformat if subformat is not None else 3)
    return bytes(buf)


class FakeCapture:
    """LoopbackCapture-Auftritt ohne COM: liefert vorbereitete Bloecke."""

    def __init__(self, blocks, rate=48000):
        self._blocks = list(blocks)
        self.format = musicbus.AudioFormat(rate, 2, 32, True, 8)
        self.buffer_ms = 100
        self.closed = False

    def open(self):
        return self.format

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def read_blocks(self):
        if self._blocks:
            return self._blocks.pop(0)
        return np.zeros((0, 2), dtype=np.float32)


def ones(frames, value=1.0):
    return np.full((frames, virtualmic.TARGET_CHANNELS), value, dtype=np.float32)


def test_float_extensible_survives_the_padding_trap():
    """Kanal-Mask 3 (Stereo) darf nicht als Format gelesen werden.

    PCM-Extensible mit Stereomask: eine ausgerichtete ctypes-Struktur laese
    `SubFormat.Data1 = 3` (= IEEE float) und wuerde Integer-Stills als Float
    deuten. Die festen Offsets sagen: Integer.
    """
    pcm = waveformat_bytes(musicbus.WAVE_FORMAT_EXTENSIBLE, 2, 48000, 16, 4,
                           valid_bits=16, mask=3, subformat=musicbus.WAVE_FORMAT_PCM)
    fmt = musicbus.read_waveformat(pcm)
    assert fmt.is_float is False, "Kanal-Mask wurde als Format gelesen"
    assert (fmt.rate, fmt.channels, fmt.bits, fmt.block_align) == (48000, 2, 16, 4)

    flt = waveformat_bytes(musicbus.WAVE_FORMAT_EXTENSIBLE, 2, 48000, 32, 8,
                           valid_bits=32, mask=3, subformat=musicbus.WAVE_FORMAT_IEEE_FLOAT)
    fmt = musicbus.read_waveformat(flt)
    assert fmt.is_float is True
    assert (fmt.rate, fmt.channels, fmt.bits, fmt.block_align) == (48000, 2, 32, 8)
    print("WAVEFORMATEXTENSIBLE ueber feste Offsets: OK")


def test_plain_pcm_is_integer_and_short_buffers_are_refused():
    fmt = musicbus.read_waveformat(waveformat_bytes(musicbus.WAVE_FORMAT_PCM, 1, 44100, 16, 2))
    assert fmt.is_float is False and fmt.channels == 1 and fmt.rate == 44100
    try:
        musicbus.read_waveformat(b"\x01\x00")
    except ValueError:
        pass
    else:
        raise AssertionError("zu kurzer Puffer musste abgewiesen werden")
    print("PCM-Tag und kurze Puffer: OK")


def test_decode_frames_scales_every_width():
    flt = musicbus.AudioFormat(48000, 2, 32, True, 8)
    raw = struct.pack("<4f", 0.0, 0.5, -0.5, 1.0)
    out = musicbus.decode_frames(raw, flt)
    assert out.shape == (2, 2)
    assert np.allclose(out, [[0.0, 0.5], [-0.5, 1.0]])

    i16 = musicbus.AudioFormat(48000, 2, 16, False, 4)
    raw = struct.pack("<4h", 32767, -32768, 0, 16384)
    out = musicbus.decode_frames(raw, i16)
    assert np.allclose(out, [[1.0, -1.0], [0.0, 0.5]], atol=1e-4)

    i32 = musicbus.AudioFormat(48000, 1, 32, False, 4)
    raw = struct.pack("<2i", 2147483647, -2147483648)
    out = musicbus.decode_frames(raw, i32)
    assert out.shape == (2, 1)
    assert np.allclose(out, [[1.0], [-1.0]], atol=1e-6)
    print("Dekodieren 32f/16i/32i: OK")


def test_resample_keeps_the_ratio_and_is_identity_at_equal_rates():
    block = ones(441)
    assert musicbus.resample_linear(block, 48000, 48000) is block
    out = musicbus.resample_linear(block, 44100, 48000)
    assert len(out) == 480, len(out)
    assert out.shape[1] == virtualmic.TARGET_CHANNELS
    empty = np.zeros((0, 2), dtype=np.float32)
    assert len(musicbus.resample_linear(empty, 44100, 48000)) == 0
    print("Nachtaasten 44.1k -> 48k: OK")


def test_offer_and_pull_roundtrip_with_gain():
    bus = musicbus.MusicBus(gain=0.5, capture_factory=lambda: FakeCapture([]))
    bus._offer(ones(480))
    out = bus.pull(480)
    assert out.shape == (480, virtualmic.TARGET_CHANNELS)
    assert np.allclose(out, 0.5), out[:2]
    print("Einreihen/Ziehen mit Gain: OK")


def test_pull_pads_with_silence_and_joins_partial_blocks():
    bus = musicbus.MusicBus(capture_factory=lambda: FakeCapture([]))
    assert not bus.pull(480).any(), "leere Puffer muessen Stille liefern"
    bus._offer(ones(200))
    bus._offer(ones(200))
    out = bus.pull(480)
    assert out[:400].all() and not out[400:].any(), "Bloecke reihen sich, Rest ist Stille"
    print("Stille bei Leere, Teilbloecke fuegen sich: OK")


def test_the_ring_drops_the_oldest_when_the_bus_falls_behind():
    bus = musicbus.MusicBus(max_blocks=3, capture_factory=lambda: FakeCapture([]))
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        bus._offer(ones(480, value))
    out = bus.pull(480)
    assert np.allclose(out, 3.0), out[:2]  # die aeltesten (1, 2) sind gefallen
    out = bus.pull(480)
    assert np.allclose(out, 4.0), out[:2]
    print("Rueckstand wirft die aeltesten Bloecke: OK")


def test_pump_once_normalizes_and_resamples():
    capture = FakeCapture([ones(441)], rate=44100)
    bus = musicbus.MusicBus(capture_factory=lambda: capture)
    bus._capture = capture
    bus._pump_once()
    out = bus.pull(480)
    assert out.any(), "der gepumpte Block kam an"
    assert np.allclose(out[:480], 1.0, atol=1e-3), "441 Frames @44.1k werden zu 480 @48k"
    print("Pumpen normalisiert auf 48k/Stereo: OK")


def test_the_thread_pumps_until_stopped_and_closes_its_capture():
    capture = FakeCapture([ones(480), ones(480)])
    bus = musicbus.MusicBus(capture_factory=lambda: capture)
    bus.start()
    got = False
    deadline = time.time() + 2.0
    while time.time() < deadline and not got:
        got = bool(bus.pull(480).any())
        time.sleep(0.01)
    assert got, f"der Thread hat nichts gepumpt (error={bus.error!r})"
    assert bus.running, "der Thread laeuft, solange er nicht gestoppt wird"
    bus.close()
    assert not bus.running
    assert capture.closed, "close() laesst die Aufnahme los"
    print("Thread pumpt, stoppt und schliesst: OK")


def test_on_block_gets_every_offered_block_with_the_gain():
    """Der Mischpfad-Haken (Spec §4) bekommt jeden Block, Gain drin - und der
    Ringpuffer fuer `pull` bleibt daneben erhalten."""
    seen = []
    bus = musicbus.MusicBus(gain=0.5, capture_factory=lambda: FakeCapture([]))
    bus.on_block = seen.append
    bus._offer(ones(480))
    bus._offer(ones(480))
    assert len(seen) == 2, f"je angebotenem Block ein Aufruf, kamen {len(seen)}"
    assert np.allclose(seen[0], 0.5), "der Gain-Block kommt an"
    assert bus.pull(480).any(), "der Ringpuffer fuer pull bleibt gefuellt"
    print("on_block wird je Block mit dem Gain-Block gerufen: OK")


def test_the_service_starts_and_stops_the_bus_and_keeps_the_state():
    """Handler setzen Config und Zustand, an ist an und aus ist aus (Spec §3)."""
    c, events = core_fakes.make_core()
    bus = c.musicbus.bus
    assert isinstance(bus, core_fakes.FakeMusicBus), "kein echtes WASAPI im Test"
    assert c.state()["musicbus"] == {"enabled": False, "running": False,
                                    "error": "", "gain": 1.0}
    c.send(p.SetMusicBus(True))
    assert bus.started == 1 and bus.running
    assert c.store.data["musicbus_enabled"] is True
    state = c.state()["musicbus"]
    assert state["enabled"] is True and state["running"] is True
    assert core_fakes.of_type(events, p.StateChanged), "die Seite sieht den Schalter"
    c.send(p.SetMusicBus(False))
    assert bus.stopped == 1 and not bus.running
    assert c.store.data["musicbus_enabled"] is False
    assert c.shutdown() is True
    assert bus.closed == 1, "beim Beenden wird der Bus geschlossen"
    print("SetMusicBus startet/stoppt den Bus und haelt Config und Zustand: OK")


def test_the_service_starts_from_the_config_on_start():
    data = config._default_config()
    data["musicbus_enabled"] = True
    data["musicbus_gain"] = 0.5
    c, _events = core_fakes.make_core(store_data=data)
    c.start()
    bus = c.musicbus.bus
    assert bus.started == 1 and bus.running, "der Bus startet mit der Config"
    assert bus.gain == 0.5, "der gespeicherte Pegel kommt beim Start an"
    c.shutdown()
    print("on_start folgt musicbus_enabled: OK")


def test_the_gain_is_clamped_before_it_reaches_bus_and_state():
    c, _events = core_fakes.make_core()
    c.send(p.SetMusicBusGain(-1.0))
    assert c.store.data["musicbus_gain"] == 0.0
    assert c.musicbus.bus.gain == 0.0
    c.send(p.SetMusicBusGain(9.0))
    assert c.store.data["musicbus_gain"] == 2.0
    assert c.musicbus.bus.gain == 2.0
    assert c.state()["musicbus"]["gain"] == 2.0
    c.send(p.SetMusicBusGain(0.25))
    assert c.state()["musicbus"]["gain"] == 0.25
    print("Gain wird geclampt (-1 -> 0.0, 9 -> 2.0): OK")


def test_a_bus_failure_becomes_a_notice_and_stays_in_the_state():
    """Nie still sterben: der Fehler wird einmal gemeldet und bleibt sichtbar (§2/§3)."""
    c, events = core_fakes.make_core()
    c.send(p.SetMusicBus(True))
    c.musicbus.bus.fail(OSError("WASAPI weg"))
    notices = core_fakes.of_type(events, p.Notice)
    assert p.Notice(musicbus.MUSICBUS_ERROR, "error") in notices, notices
    assert notices.count(p.Notice(musicbus.MUSICBUS_ERROR, "error")) == 1, "genau einmal"
    state = c.state()["musicbus"]
    assert "WASAPI weg" in state["error"], state
    assert state["running"] is False, "running bleibt die Wahrheit aus bus.running"
    print("Bus-Fehler wird als Notice gemeldet und steht in error: OK")


def main():
    test_float_extensible_survives_the_padding_trap()
    test_plain_pcm_is_integer_and_short_buffers_are_refused()
    test_decode_frames_scales_every_width()
    test_resample_keeps_the_ratio_and_is_identity_at_equal_rates()
    test_offer_and_pull_roundtrip_with_gain()
    test_pull_pads_with_silence_and_joins_partial_blocks()
    test_the_ring_drops_the_oldest_when_the_bus_falls_behind()
    test_pump_once_normalizes_and_resamples()
    test_the_thread_pumps_until_stopped_and_closes_its_capture()
    test_on_block_gets_every_offered_block_with_the_gain()
    test_the_service_starts_and_stops_the_bus_and_keeps_the_state()
    test_the_service_starts_from_the_config_on_start()
    test_the_gain_is_clamped_before_it_reaches_bus_and_state()
    test_a_bus_failure_becomes_a_notice_and_stays_in_the_state()
    print("\nALL MUSICBUS CHECKS PASSED")


if __name__ == "__main__":
    main()
