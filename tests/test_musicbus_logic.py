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
import threading
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
from soundboard import dynamics  # noqa: E402
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


def test_plain_ieee_float_reads_as_32_bit_float():
    """Regression: eine nicht-extensible WAVEFORMATEX mit Tag WAVE_FORMAT_IEEE_FLOAT
    wurde faelschlich abgewiesen (nur PCM/EXTENSIBLE waren erlaubt), obwohl
    `is_float` schon richtig berechnet war."""
    fmt = musicbus.read_waveformat(waveformat_bytes(musicbus.WAVE_FORMAT_IEEE_FLOAT, 2, 48000, 32, 8))
    assert fmt.is_float is True and fmt.channels == 2 and fmt.rate == 48000 and fmt.bits == 32
    print("plaine WAVE_FORMAT_IEEE_FLOAT: OK")


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


class BlockingOpenCapture:
    """LoopbackCapture-Auftritt, dessen `open()` haengt, bis ein Event freigegeben
    wird - simuliert eine langsame COM-Aktivierung fuer M3 (zwei Bus-Faeden)."""

    def __init__(self, event, rate=48000):
        self._event = event
        self.format = musicbus.AudioFormat(rate, 2, 32, True, 8)
        self.buffer_ms = 100
        self.closed = False

    def open(self):
        self._event.wait()
        return self.format

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def read_blocks(self):
        return np.zeros((0, 2), dtype=np.float32)


class Finder:
    """target_finder-Attrappe: liefert die aktuelle PID, umschaltbar aus dem Test."""

    def __init__(self, pid=None):
        self.pid = pid

    def __call__(self):
        return self.pid


def wait_until(cond, seconds=2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_the_bus_waits_for_the_target_and_attaches_when_it_appears():
    finder = Finder(None)
    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 50)

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: bus.waiting), "ohne Spotify: waiting"
    assert opened == [] and bus.running, "der Faden lebt und wartet"
    finder.pid = 77
    assert wait_until(lambda: not bus.waiting and opened == [77]), (bus.waiting, opened)
    assert wait_until(lambda: bus.pull(480).any()), "nach dem Anhaengen fliesst Ton"
    bus.close()
    print("Bus wartet auf Spotify und haengt sich an: OK")


def test_the_bus_detaches_when_the_target_dies_and_reattaches_on_restart():
    finder = Finder(10)
    captures = []

    def factory(pid):
        cap = FakeCapture([])
        cap.pid = pid
        captures.append(cap)
        return cap

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: len(captures) == 1)
    finder.pid = None
    assert wait_until(lambda: bus.waiting and captures[0].closed), "Spotify weg: schliessen + warten"
    finder.pid = 11
    assert wait_until(lambda: len(captures) == 2 and not bus.waiting)
    assert captures[1].pid == 11, "neue PID nach dem Neustart"
    assert bus.error is None
    bus.close()
    assert captures[1].closed and not bus.waiting
    print("Bus loest sich bei Spotify-Ende und haengt sich nach Neustart wieder an: OK")


def test_a_capture_error_while_the_target_lives_is_reported():
    finder = Finder(5)

    class Broken(FakeCapture):
        def read_blocks(self):
            raise OSError("capture kaputt")

    failures = []
    bus = musicbus.MusicBus(capture_factory=lambda pid: Broken([]), target_finder=finder,
                            poll_s=0.02)
    bus.on_error = failures.append
    bus.start()
    assert wait_until(lambda: not bus.running), "echter Fehler beendet den Faden"
    assert isinstance(bus.error, OSError) and failures, "nie still sterben"
    print("Aufnahmefehler bei lebendem Spotify wird gemeldet: OK")


def test_a_capture_error_after_the_target_died_just_waits():
    finder = Finder(5)

    class DiesWithSpotify(FakeCapture):
        def read_blocks(self):
            finder.pid = None
            raise OSError("Prozess weg")

    bus = musicbus.MusicBus(capture_factory=lambda pid: DiesWithSpotify([]),
                            target_finder=finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: bus.waiting), "Spotify beendet: warten, kein Fehler"
    assert bus.error is None and bus.running
    bus.close()
    print("Fehler nach Spotify-Ende fuehrt nur zu waiting: OK")


def test_the_service_reports_waiting_and_not_running_while_spotify_is_missing():
    c, _events = core_fakes.make_core()
    c.send(p.SetMusicBus(True))
    c.musicbus.bus.waiting = True
    state = c.state()["musicbus"]
    assert state["waiting"] is True and state["running"] is False, state
    c.musicbus.bus.waiting = False
    state = c.state()["musicbus"]
    assert state["waiting"] is False and state["running"] is True, state
    print("Zustand waiting/running: OK")


def test_the_spotify_bus_levels_itself_before_the_gain():
    # Auto-Pegel (Jan 2026-09-30): the leveler sits before the user's gain, so the
    # slider keeps its meaning and the Spotify volume only matters for his own ears.
    bus = musicbus.spotify_bus(gain=0.5)
    assert isinstance(bus.leveler, dynamics.MusicLeveler)
    quiet = np.full((480, 2), 0.02, dtype=np.float32)  # -34 dBFS: +20 dB to the reference
    for _ in range(2000):  # 20 s
        bus._offer(quiet)
    out = bus.pull(480)
    level = dynamics.gain_to_db(float(np.sqrt(np.mean(out ** 2))))
    expect = dynamics.MUSIC_REFERENCE_DB + dynamics.gain_to_db(0.5)
    assert abs(level - expect) < 1.0, (level, expect)
    assert musicbus.MusicBus(capture_factory=lambda: FakeCapture([])).leveler is None, "plain bus stays exact"
    print("Spotify-Bus pegelt vor dem Gain: OK")


def test_the_default_bus_follows_spotify():
    bus = musicbus.spotify_bus(gain=0.5)
    from soundboard import processloopback
    assert bus._target_finder is processloopback.find_spotify_pid
    assert bus._capture_factory is processloopback.ProcessLoopbackCapture
    assert bus.gain == 0.5 and not bus.running, "gebaut, nicht gestartet"
    print("Standard-Bus folgt Spotify: OK")


def test_a_capture_error_during_shutdown_gets_a_grace_period_before_raising():
    """Review Fix 1: der Finder meldet die PID noch einen Moment, waehrend Spotify
    schon schliesst - erst wenn die Gnadenfrist verstreicht UND das Ziel immer noch
    lebt, ist der Fehler echt."""
    finder = Finder(5)

    class RaisesOnce(FakeCapture):
        def read_blocks(self):
            raise OSError("Prozess schliesst gerade")

    calls = []

    def flaky_finder():
        calls.append(1)
        return 5 if len(calls) <= 2 else None

    bus = musicbus.MusicBus(capture_factory=lambda pid: RaisesOnce([]),
                            target_finder=flaky_finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: bus.waiting), "Gnadenfrist: kein Fehler, nur warten"
    assert bus.error is None and bus.running
    bus.close()
    print("Fehler waehrend Spotify schliesst: Gnadenfrist statt sofortigem Ende: OK")


def test_a_finder_error_is_swallowed_and_the_bus_still_attaches():
    """Review Fix 2: eine Toolhelp-Stoerung im target_finder darf den Bus nie
    beenden - sie zaehlt wie "kein Ziel gefunden"."""
    calls = []

    def flaky_finder():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("Toolhelp kaputt")
        return 5

    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 50)

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=flaky_finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: opened == [5]), opened
    assert bus.error is None and bus.running
    bus.close()
    print("Fehler bei der Prozesssuche wird verschluckt: OK")


def test_four_consecutive_lookup_failures_then_success_attaches_without_error():
    """Final-Fix F3: eine dauerhaft scheiternde Prozesssuche darf den Bus nicht bei
    jedem Fehlschlag mit Traceback loggen, und solange es unter der Schwelle bleibt,
    darf sie den Faden nicht beenden."""
    calls = []

    def flaky_finder():
        calls.append(1)
        if len(calls) <= 4:
            raise OSError("Toolhelp kaputt")
        return 5

    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 50)

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=flaky_finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: opened == [5]), opened
    assert bus.error is None and bus.running, "unter der Schwelle: kein Fehler"
    bus.close()
    print("vier Fehlschlaege in Folge, dann Erfolg: haengt an, kein Fehler: OK")


def test_five_consecutive_lookup_failures_raises_the_last_error():
    def always_fails():
        raise OSError("Toolhelp dauerhaft kaputt")

    failures = []
    bus = musicbus.MusicBus(capture_factory=lambda pid: FakeCapture([]),
                            target_finder=always_fails, poll_s=0.01)
    bus.on_error = failures.append
    bus.start()
    assert wait_until(lambda: not bus.running), "5 Fehler in Folge: der Faden endet"
    assert isinstance(bus.error, OSError) and failures, "nie still sterben"
    bus.close()
    print("fuenf Fehlschlaege in Folge: der letzte Fehler wird geworfen: OK")


def test_a_success_after_failures_resets_the_counter():
    """Ein Erfolg zwischendrin muss den Zaehler zuruecksetzen - sonst wuerde ein
    spaeterer, unabhaengiger Fehlschlag faelschlich als "der fuenfte in Folge" zaehlen."""
    calls = []

    def finder():
        calls.append(1)
        # 3 Fehler, 1 Erfolg (setzt zurueck), dann 3 weitere Fehler - nie 5 in Folge.
        n = len(calls)
        if n in (1, 2, 3, 5, 6, 7):
            raise OSError("Toolhelp kaputt")
        return None  # Erfolg der Suche selbst = "kein Spotify", kein Fehler

    bus = musicbus.MusicBus(capture_factory=lambda pid: FakeCapture([]), target_finder=finder,
                            poll_s=0.01)
    bus.start()
    assert wait_until(lambda: len(calls) >= 7), calls
    time.sleep(0.05)
    assert bus.error is None and bus.running, "der Zaehler wurde durch den Erfolg zurueckgesetzt"
    bus.close()
    print("ein Erfolg zwischendrin setzt den Fehlerzaehler zurueck: OK")


def test_stop_does_not_let_a_hung_thread_leak_into_a_second_one():
    """M3: haengt der alte Faden noch in `open()`, darf `start()` keinen zweiten
    Faden aufmachen, der sich `_capture` mit dem ersten teilt - stattdessen ein
    Fehler, der zur Notice wird."""
    event = threading.Event()
    bus = musicbus.MusicBus(capture_factory=lambda: BlockingOpenCapture(event),
                            stop_timeout_s=0.05)
    bus.start()
    assert wait_until(lambda: bus.running), "der erste Faden muss erst richtig laufen"
    time.sleep(0.02)  # er haengt jetzt in open()
    bus.stop()  # das join-Timeout laeuft ab, der Faden lebt noch
    assert bus.running, "running zeigt in dieser Zeit weiter den alten Faden"

    errors = []
    bus.on_error = errors.append
    bus.start()  # darf keinen zweiten Faden aufmachen

    named = [t for t in threading.enumerate() if t.name == "ruckus-musicbus"]
    assert len(named) == 1, f"es darf nur ein Musik-Bus-Faden existieren: {named}"
    assert isinstance(bus.error, RuntimeError), bus.error
    assert errors and isinstance(errors[0], RuntimeError), errors

    event.set()  # jetzt darf der alte Faden fertig werden
    assert wait_until(lambda: not bus.running)
    bus.close()
    print("stop() laesst keinen zweiten Faden neben dem haengenden entstehen: OK")


def test_a_periodic_lookup_error_counts_as_unchanged_and_does_not_reopen_the_capture():
    """M2: ein einzelner Fehler der Prozesssuche waehrend des Periodenchecks in
    laufender Aufnahme darf nicht wie eine echte PID-Aenderung behandelt werden -
    die Aufnahme bleibt offen, dasselbe Capture-Objekt, `factory` nur einmal
    gerufen."""
    calls = []

    def finder():
        calls.append(1)
        n = len(calls)
        if n == 2:  # der erste Periodencheck nach dem Anhaengen
            raise OSError("Toolhelp kurz gestoert")
        return 5

    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 500)

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=finder, poll_s=0.02)
    bus.start()
    assert wait_until(lambda: opened == [5]), opened
    assert wait_until(lambda: len(calls) >= 3), calls  # mind. ein Periodencheck geschah
    time.sleep(0.05)  # Zeit fuer weitere Periodenchecks, waehrend angehaengt
    assert opened == [5], f"die Aufnahme darf nach dem Fehler nicht neu geoeffnet werden: {opened}"
    assert bus.error is None and bus.running
    bus.close()
    print("ein Fehler im Periodencheck zaehlt als unveraendert, keine neue Aufnahme: OK")


def test_start_resets_the_lookup_failure_counter():
    """M1: nach 5 Fehlern in Folge stirbt der Bus; nach erneutem Einschalten darf ein
    einzelner Fehler ihn nicht sofort wieder beenden - der Zaehler muss in start()
    zurueckgesetzt werden, nicht erst nach dem naechsten Erfolg."""
    calls = []

    def finder():
        calls.append(1)
        n = len(calls)
        if n <= 5:
            raise OSError("Toolhelp dauerhaft kaputt")
        if n == 6:
            raise OSError("nur einer, direkt nach dem Neustart")
        return 9

    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 50)

    bus = musicbus.MusicBus(capture_factory=factory, target_finder=finder, poll_s=0.01)
    bus.start()
    assert wait_until(lambda: not bus.running), "5 Fehler in Folge: der Faden endet"
    bus.start()
    assert wait_until(lambda: opened == [9]), opened
    assert bus.error is None and bus.running, (
        "ein einzelner Fehler nach dem Neustart darf den Bus nicht wieder beenden")
    bus.close()
    print("start() setzt den Lookup-Fehlerzaehler zurueck: OK")


def test_on_state_fires_when_entering_and_leaving_waiting_not_every_poll():
    """Review Fix 3: der Haken feuert nur beim Wechsel, nicht bei jedem Prueftakt."""
    finder = Finder(None)
    opened = []

    def factory(pid):
        opened.append(pid)
        return FakeCapture([ones(480)] * 50)

    seen = []
    bus = musicbus.MusicBus(capture_factory=factory, target_finder=finder, poll_s=0.02)
    bus.on_state = lambda: seen.append(bus.waiting)
    bus.start()
    assert wait_until(lambda: bus.waiting)
    finder.pid = 77
    assert wait_until(lambda: not bus.waiting and opened == [77])
    time.sleep(0.06)  # ein paar weitere Pruefungen, waehrend angehaengt - kein Feuern
    bus.close()
    assert seen == [True, False], seen
    print("on_state feuert nur beim Wechsel: OK")


def test_the_bus_starts_waiting_with_a_target_finder_before_anything_attaches():
    """Review Fix 4: mit target_finder faengt der Bus im Warten an, `running` zeigt
    nie "an", bevor ueberhaupt etwas anhaengt."""
    bus = musicbus.MusicBus(capture_factory=lambda pid: FakeCapture([]),
                            target_finder=Finder(None), poll_s=0.5)
    bus.start()
    assert bus.waiting, "sofort nach start(): waiting, nicht erst nach dem ersten Prueftakt"
    bus.close()
    print("Bus faengt mit target_finder im Warten an: OK")


def test_closing_the_bus_while_it_is_waiting_returns_promptly():
    """Review Fix 6: `close()` soll den Wartezyklus nicht aussitzen muessen."""
    finder = Finder(None)
    bus = musicbus.MusicBus(capture_factory=lambda pid: FakeCapture([]), target_finder=finder,
                            poll_s=5.0)
    bus.start()
    assert wait_until(lambda: bus.waiting)
    started = time.time()
    bus.close()
    elapsed = time.time() - started
    assert elapsed < 1.0, f"close() dauerte {elapsed:.2f}s"
    assert not bus.running and not bus.waiting
    print("Schliessen waehrend des Wartens ist zuegig: OK")


def test_the_service_starts_and_stops_the_bus_and_keeps_the_state():
    """Handler setzen Config und Zustand, an ist an und aus ist aus (Spec §3)."""
    c, events = core_fakes.make_core()
    bus = c.musicbus.bus
    assert isinstance(bus, core_fakes.FakeMusicBus), "kein echtes WASAPI im Test"
    assert c.state()["musicbus"] == {"enabled": False, "running": False, "waiting": False,
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


def test_set_music_bus_reaches_set_music_enabled_on_the_current_sink():
    """Final-Fix F2: SetMusicBus(True/False) muss den aktuellen Sink erreichen, auf
    dem Geraete-Thread (`core.devices.submit`) - sonst bleibt ein reines Musik-Kabel
    zu, obwohl der Bus laengst laeuft."""
    backend = core_fakes.FakeBackend()
    c, _events = core_fakes.make_core(backend=backend)
    c.start()
    sink = backend.sinks[0]
    c.send(p.SetMusicBus(True))
    assert sink.music_enabled == [True], sink.music_enabled
    c.send(p.SetMusicBus(False))
    assert sink.music_enabled == [True, False], sink.music_enabled
    print("SetMusicBus erreicht set_music_enabled am aktuellen Sink: OK")


def test_set_music_bus_is_a_noop_on_the_sink_when_no_sink_is_attached():
    """Kein Sink (Geraete fehlen, Kabel deinstalliert): darf nicht knallen."""
    c, _events = core_fakes.make_core()
    c.send(p.SetMusicBus(True))  # kein core.start(): kein Sink gebaut
    c.send(p.SetMusicBus(False))
    print("SetMusicBus ohne Sink ist ein Noop: OK")


def test_the_services_state_changes_when_the_bus_signals_waiting():
    """Review Fix 3: `on_state` stoesst - ueber den Executor, wie `_bus_failed` -
    ein StateChanged an, damit die Oberflaeche das Warten sofort sieht."""
    c, events = core_fakes.make_core()
    c.send(p.SetMusicBus(True))
    events.clear()
    c.musicbus.bus.on_state()
    assert core_fakes.of_type(events, p.StateChanged), events
    print("on_state fuehrt zu StateChanged: OK")


def main():
    test_float_extensible_survives_the_padding_trap()
    test_plain_pcm_is_integer_and_short_buffers_are_refused()
    test_plain_ieee_float_reads_as_32_bit_float()
    test_decode_frames_scales_every_width()
    test_resample_keeps_the_ratio_and_is_identity_at_equal_rates()
    test_offer_and_pull_roundtrip_with_gain()
    test_pull_pads_with_silence_and_joins_partial_blocks()
    test_the_ring_drops_the_oldest_when_the_bus_falls_behind()
    test_pump_once_normalizes_and_resamples()
    test_the_thread_pumps_until_stopped_and_closes_its_capture()
    test_on_block_gets_every_offered_block_with_the_gain()
    test_the_bus_waits_for_the_target_and_attaches_when_it_appears()
    test_the_bus_detaches_when_the_target_dies_and_reattaches_on_restart()
    test_a_capture_error_while_the_target_lives_is_reported()
    test_a_capture_error_after_the_target_died_just_waits()
    test_the_service_reports_waiting_and_not_running_while_spotify_is_missing()
    test_the_default_bus_follows_spotify()
    test_the_spotify_bus_levels_itself_before_the_gain()
    test_a_capture_error_during_shutdown_gets_a_grace_period_before_raising()
    test_a_finder_error_is_swallowed_and_the_bus_still_attaches()
    test_four_consecutive_lookup_failures_then_success_attaches_without_error()
    test_five_consecutive_lookup_failures_raises_the_last_error()
    test_a_success_after_failures_resets_the_counter()
    test_stop_does_not_let_a_hung_thread_leak_into_a_second_one()
    test_a_periodic_lookup_error_counts_as_unchanged_and_does_not_reopen_the_capture()
    test_start_resets_the_lookup_failure_counter()
    test_on_state_fires_when_entering_and_leaving_waiting_not_every_poll()
    test_the_bus_starts_waiting_with_a_target_finder_before_anything_attaches()
    test_closing_the_bus_while_it_is_waiting_returns_promptly()
    test_the_service_starts_and_stops_the_bus_and_keeps_the_state()
    test_the_service_starts_from_the_config_on_start()
    test_the_gain_is_clamped_before_it_reaches_bus_and_state()
    test_a_bus_failure_becomes_a_notice_and_stays_in_the_state()
    test_set_music_bus_reaches_set_music_enabled_on_the_current_sink()
    test_set_music_bus_is_a_noop_on_the_sink_when_no_sink_is_attached()
    test_the_services_state_changes_when_the_bus_signals_waiting()
    print("\nALL MUSICBUS CHECKS PASSED")


if __name__ == "__main__":
    main()
