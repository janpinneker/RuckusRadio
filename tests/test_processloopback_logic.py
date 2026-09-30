"""Process-Loopback ohne Geraet: Bytes, Strukturgroessen, Prozesswahl, Handler.

Die Aktivierung selbst (ActivateAudioInterfaceAsync) prueft nur die Handprobe
`tests/test_process_loopback_manual.py` - hier steht alles, was sich ohne Ton
beweisen laesst."""

import ctypes
import os
import struct
import sys
import tempfile
from ctypes import POINTER, byref, c_void_p
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-procloop-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import musicbus, processloopback as pl  # noqa: E402


def test_activation_params_are_type_pid_and_tree_mode():
    raw = pl.activation_params(4242)
    assert len(raw) == 12, len(raw)
    assert struct.unpack("<iIi", raw) == (1, 4242, 0), "PROCESS_LOOPBACK, PID, INCLUDE_TREE"
    print("activation params: OK")


def test_the_fixed_format_is_48k_stereo_float_and_reads_back():
    raw = pl.float_waveformat()
    assert len(raw) == 18
    assert musicbus.read_waveformat(raw + bytes(10)) == pl.FIXED_FORMAT
    assert pl.FIXED_FORMAT.rate == 48000 and pl.FIXED_FORMAT.channels == 2
    assert pl.FIXED_FORMAT.is_float and pl.FIXED_FORMAT.bits == 32
    print("festes Format 48 kHz / Stereo / Float: OK")


def test_struct_sizes_match_windows_x64():
    assert ctypes.sizeof(pl._PROPVARIANT) == 8 + 2 * ctypes.sizeof(c_void_p)
    assert ctypes.sizeof(pl._PROCESSENTRY32W) == 568, ctypes.sizeof(pl._PROCESSENTRY32W)
    print("Strukturgroessen: OK")


def test_the_root_spotify_process_is_picked():
    entries = [
        (4, 0, "System"),
        (100, 50, "explorer.exe"),
        (200, 100, "Spotify.exe"),   # oberster
        (210, 200, "Spotify.exe"),   # Kind (Ton)
        (220, 200, "spotify.exe"),   # Kind, andere Schreibweise
        (300, 100, "cs2.exe"),
    ]
    assert pl.pick_root_pid(entries) == 200
    assert pl.pick_root_pid([(1, 0, "cs2.exe")]) is None, "ohne Spotify: None"
    # zwei getrennte Baeume (z. B. Neustart im Gange): der kleinste Root gewinnt, stabil
    assert pl.pick_root_pid([(900, 1, "Spotify.exe"), (800, 1, "Spotify.exe")]) == 800
    print("oberster Spotify-Prozess wird gewaehlt: OK")


def test_the_root_with_more_spotify_children_wins_over_a_smaller_orphan_root():
    """Final-Fix F4: mehrere Wurzeln (z. B. ein verwaister Rest-Prozess vom letzten
    Neustart) - die mit den meisten Spotify.exe-Kindern gewinnt, nicht die kleinste
    PID. Der Ton kommt aus einem Kind, also zeigt die Kinderzahl die echte Wurzel."""
    entries = [
        (100, 1, "Spotify.exe"),  # verwaiste Wurzel, kleine PID, keine Kinder
        (500, 1, "Spotify.exe"),  # die echte, laufende Wurzel
        (501, 500, "Spotify.exe"),
        (502, 500, "Spotify.exe"),
        (503, 500, "Spotify.exe"),
    ]
    assert pl.pick_root_pid(entries) == 500, "die Wurzel mit den 3 Kindern gewinnt"
    # Gleichstand bei den Kindern: die kleinste PID gewinnt weiterhin, stabil.
    tied = [
        (900, 1, "Spotify.exe"), (901, 900, "Spotify.exe"),
        (800, 1, "Spotify.exe"), (801, 800, "Spotify.exe"),
    ]
    assert pl.pick_root_pid(tied) == 800
    print("die Wurzel mit den meisten Spotify-Kindern gewinnt: OK")


def test_process_entries_sees_this_python():
    entries = pl.process_entries()
    assert any(pid == os.getpid() for pid, _parent, _exe in entries), "eigener Prozess fehlt"
    print("Prozessliste per Toolhelp: OK")


def _qi(handler, iid_text):
    vtable = ctypes.cast(handler.pointer, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(musicbus._GUID),
                            POINTER(c_void_p))(vtable[0])
    out = c_void_p()
    hr = fn(handler.pointer, byref(musicbus._GUID.parse(iid_text)), byref(out))
    return hr, out.value


def test_the_completion_handler_answers_like_a_com_object():
    handler = pl._CompletionHandler()
    for iid in (pl.IID_IUNKNOWN, pl.IID_IACTIVATE_COMPLETION_HANDLER, pl.IID_IAGILE_OBJECT):
        hr, ptr = _qi(handler, iid)
        assert hr == 0 and ptr == handler.pointer.value, iid
    hr, ptr = _qi(handler, "00000000-1111-2222-3333-444444444444")
    assert hr == pl.E_NOINTERFACE and ptr is None
    vtable = ctypes.cast(handler.pointer, POINTER(POINTER(c_void_p))).contents
    done = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_void_p)(vtable[3])
    assert not handler.done.is_set()
    assert done(handler.pointer, None) == 0
    assert handler.done.is_set(), "ActivateCompleted setzt das Ereignis"
    print("Completion-Handler (QI + ActivateCompleted): OK")


class _CountingRelease:
    """COM-Attrappe wie `_CompletionHandler` (QueryInterface/AddRef/Release als
    echtes vtable), aber `Release` zaehlt echte Aufrufe - fuer M6 (Test, ob
    `close()` zweimal hintereinander jedes Objekt nur einmal freigibt)."""

    _qi_t = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(musicbus._GUID), POINTER(c_void_p))
    _ref_t = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)

    def __init__(self):
        self.release_count = 0

        def _query(_this, _riid, ppv):
            ppv[0] = None
            return pl.E_NOINTERFACE

        def _release(_this):
            self.release_count += 1
            return 0

        class _Vtbl(ctypes.Structure):
            _fields_ = [("QueryInterface", _CountingRelease._qi_t),
                       ("AddRef", _CountingRelease._ref_t),
                       ("Release", _CountingRelease._ref_t)]

        class _Obj(ctypes.Structure):
            _fields_ = [("lpVtbl", POINTER(_Vtbl))]

        self._callbacks = (_CountingRelease._qi_t(_query), _CountingRelease._ref_t(lambda _this: 1),
                           _CountingRelease._ref_t(_release))
        self._vtbl = _Vtbl(*self._callbacks)
        self._obj = _Obj(ctypes.pointer(self._vtbl))
        self.pointer = c_void_p(ctypes.addressof(self._obj))


def test_close_twice_releases_each_com_object_exactly_once():
    """M6: echter Test statt nur "knallt nicht" - zwei echte COM-Attrappen als
    client/capture, `close()` zweimal, jede darf nur einmal `Release` sehen."""
    client_obj = _CountingRelease()
    capture_obj = _CountingRelease()
    cap = pl.ProcessLoopbackCapture(123)
    cap.opened = False  # stop() wird dadurch nicht gerufen
    cap.client = client_obj.pointer
    cap.capture = capture_obj.pointer

    cap.close()
    cap.close()

    assert client_obj.release_count == 1, client_obj.release_count
    assert capture_obj.release_count == 1, capture_obj.release_count
    print("Doppel-close gibt jedes COM-Objekt genau einmal frei: OK")


def test_a_never_opened_capture_can_be_closed_twice_without_error():
    """Doppel-`close()` ohne vorheriges `open()` darf nicht knallen und client/
    capture bleiben null (kein Aufruf auf eine tote Referenz)."""
    cap = pl.ProcessLoopbackCapture(123)
    cap.close()
    cap.close()
    assert cap.client.value is None
    assert cap.capture.value is None
    print("Doppel-close ohne open: OK")


def main():
    test_activation_params_are_type_pid_and_tree_mode()
    test_the_fixed_format_is_48k_stereo_float_and_reads_back()
    test_struct_sizes_match_windows_x64()
    test_the_root_spotify_process_is_picked()
    test_the_root_with_more_spotify_children_wins_over_a_smaller_orphan_root()
    test_process_entries_sees_this_python()
    test_the_completion_handler_answers_like_a_com_object()
    test_close_twice_releases_each_com_object_exactly_once()
    test_a_never_opened_capture_can_be_closed_twice_without_error()
    print("\nALL PROCESSLOOPBACK CHECKS PASSED")


if __name__ == "__main__":
    main()
