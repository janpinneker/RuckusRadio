"""Virtual-mic resolution against an injected device list.

The list below is the real one from a Windows machine that has the VB-Audio driver
pack installed and VoiceMeeter Standard running - including the trap this whole
feature exists for: the recording device is called "Voicemeeter Out B1", while the
Windows default recording device is "Voicemeeter Out B3", which Standard never feeds.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-devices-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import config, devices  # noqa: E402

# Host API ids as this machine numbers them, so "prefer WASAPI" is tested for real
# instead of against a guessed ordering.
MME = devices.hostapi_index("MME") or 0
DSOUND = devices.hostapi_index("Windows DirectSound") or 1
WASAPI = devices.hostapi_index("Windows WASAPI") or 2

OUTPUTS = [
    {"index": 12, "name": "Kopfhörer (KT USB Audio)", "channels": 2, "hostapi": MME},
    {"index": 19, "name": "Voicemeeter Input (VB-Audio Voi", "channels": 8, "hostapi": MME},
    {"index": 44, "name": "Voicemeeter Input (VB-Audio Voicemeeter VAIO)", "channels": 8,
     "hostapi": DSOUND},
    {"index": 56, "name": "Voicemeeter Input (VB-Audio Voicemeeter VAIO)", "channels": 2,
     "hostapi": WASAPI},
]

INPUTS = [
    {"index": 1, "name": "Voicemeeter Out B3 (VB-Audio Vo", "channels": 8, "hostapi": MME},
    {"index": 2, "name": "Mikrofon (Endorfy Solum Voice S", "channels": 1, "hostapi": MME},
    {"index": 30, "name": "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)", "channels": 8,
     "hostapi": DSOUND},
    {"index": 63, "name": "Mikrofon (Endorfy Solum Voice S Mic)", "channels": 1, "hostapi": WASAPI},
    {"index": 67, "name": "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)", "channels": 8,
     "hostapi": WASAPI},
]

CABLE_OUT = {"index": 70, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2,
             "hostapi": WASAPI}
CABLE_IN = {"index": 71, "name": "CABLE Output (VB-Audio Virtual Cable)", "channels": 2,
            "hostapi": WASAPI}


def cfg(**kw):
    base = dict(config.DEFAULT_CONFIG)
    base["microphone_name"] = "Mikrofon (Endorfy Solum Voice S Mic)"
    base.update(kw)
    return base


def test_voicemeeter_only_names_the_b1_device():
    resolved = devices.resolve_virtual_mic(OUTPUTS, INPUTS, cfg(), 12)
    assert resolved["mode"] == "voicemeeter", resolved
    assert resolved["out_index"] == 56, "WASAPI preferred over the DirectSound twin"
    # The point of the whole feature: name the device the voice chat must record from.
    assert resolved["discord_device_name"] == "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)"
    assert resolved["in_index"] == 67, "WASAPI preferred over the DirectSound twin"
    assert resolved["connected"] is True
    print("voicemeeter mode names Out B1:", resolved["discord_device_name"])


def test_cable_wins_in_auto_mode():
    outputs = OUTPUTS + [CABLE_OUT]
    inputs = INPUTS + [CABLE_IN]
    resolved = devices.resolve_virtual_mic(outputs, inputs, cfg(), 12)
    assert resolved["mode"] == "cable" and resolved["out_index"] == 70, resolved
    assert resolved["discord_device_name"] == "CABLE Output (VB-Audio Virtual Cable)"
    assert resolved["label"] == "VB-CABLE"
    print("auto mode prefers VB-CABLE: OK")


def test_mme_truncation_still_resolves():
    """Only the MME entries exist (names cut to 31 chars): the short needle must match."""
    outputs = [OUTPUTS[0], OUTPUTS[1]]
    resolved = devices.resolve_virtual_mic(outputs, INPUTS, cfg(), 12)
    assert resolved["out_index"] == 19, resolved
    assert resolved["discord_device_name"] == "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)"
    print("MME truncation: OK")


def test_monitor_never_equals_the_virtual_mic():
    only_vm = [OUTPUTS[1], OUTPUTS[2]]
    resolved = devices.resolve_devices(only_vm, cfg(), 44)
    assert resolved["voicemeeter"] == 44 and resolved["monitor"] is None

    with_cable = [CABLE_OUT, OUTPUTS[0]]
    cable = devices.resolve_devices(with_cable, cfg(), 70)
    assert cable["voicemeeter"] == 70, cable
    assert cable["monitor"] is None, "the cable must never double as the monitor"
    print("monitor never equals the virtual mic: OK")


def test_nothing_found():
    plain = [OUTPUTS[0]]
    resolved = devices.resolve_virtual_mic(plain, INPUTS[:2], cfg(), 12)
    assert resolved == {"mode": None, "label": None, "out_index": None, "in_index": None,
                        "out_name": None, "discord_device_name": None, "connected": False}, resolved
    print("no virtual mic at all: OK")


def test_resolve_mic_prefers_wasapi():
    assert devices.resolve_mic(INPUTS, cfg()) == 63, "full name + WASAPI wins over MME truncation"
    assert devices.resolve_mic(INPUTS, cfg(microphone_name="Nicht da"), 2) == 2, "falls back to default"
    assert devices.resolve_mic([], cfg()) is None
    print("resolve_mic: OK")


def test_resolve_mic_refuses_a_virtual_fallback():
    """Critical: an unplugged mic must never fall back to a virtual cable's recording
    side. INPUTS[1] (index 1) is "Voicemeeter Out B3" - PortAudio's real default input
    on the machine this bug was found on. Opening it as the microphone would feed the
    mixer's own output straight back into itself (a feedback loop into whatever
    listens on that cable, e.g. Discord) instead of just failing to voice."""
    assert devices.resolve_mic(INPUTS, cfg(microphone_name="Nicht da"), 1) is None, \
        "a virtual cable must never become the fallback microphone"
    print("resolve_mic refuses a virtual fallback: OK")


# The real device list after VoiceMeeter was uninstalled and VB-CABLE installed: the
# cable shows up under every host API, and Windows' default playback device is the
# cable itself. PortAudio lists MME first, so "first match wins" picks MME - measured
# 7 % dropped 10 ms blocks through the cable versus 1 % on WASAPI.
CABLE_MME = {"index": 5, "name": "CABLE Input (VB-Audio Virtual C", "channels": 16, "hostapi": MME}
CABLE_DSOUND = {"index": 17, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 16,
                "hostapi": DSOUND}
CABLE_WASAPI = {"index": 24, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2,
                "hostapi": WASAPI}
HEADPHONES_MME = {"index": 10, "name": "Kopfhörer (KT USB Audio)", "channels": 2, "hostapi": MME}


def test_cable_output_prefers_wasapi():
    """The playback side of the virtual cable must land on WASAPI when it exists."""
    outputs = [CABLE_MME, HEADPHONES_MME, CABLE_DSOUND, CABLE_WASAPI]
    inputs = [CABLE_IN]

    # Windows default playback is the cable itself, so there is no monitor to borrow a
    # host API from - exactly the case that used to fall through to MME.
    resolved = devices.resolve_virtual_mic(outputs, inputs, cfg(), 5)
    assert resolved["mode"] == "cable", resolved
    assert resolved["out_index"] == 24, f"expected the WASAPI cable, got {resolved}"

    # Same when a real monitor exists on MME: the monitor's host API must not drag the
    # cable back to MME.
    with_monitor = devices.resolve_virtual_mic(outputs, inputs, cfg(monitor_device="Kopfhörer"), 10)
    assert with_monitor["out_index"] == 24, with_monitor

    # And resolve_devices, which the dock reads, must agree.
    assert devices.resolve_devices(outputs, cfg(), 5)["voicemeeter"] == 24
    print("cable output prefers WASAPI: OK")


# The headphones' WASAPI twin - HEADPHONES_MME above is the same physical device
# listed under MME. Neither collides with the virtual mic (Voicemeeter Input,
# resolved separately via OUTPUTS[1]/OUTPUTS[3]), unlike every other fixture in this
# file where the WASAPI index the monitor fix produces happens to equal the virtual
# mic's index - which would make resolve_devices's "monitor != virtual mic" guard
# null the monitor either way and hide a regression here.
HEADPHONES_WASAPI = {"index": 13, "name": "Kopfhörer (KT USB Audio)", "channels": 2, "hostapi": WASAPI}
MONITOR_TEST_OUTPUTS = [HEADPHONES_MME, HEADPHONES_WASAPI, OUTPUTS[1], OUTPUTS[3]]


def test_monitor_prefers_wasapi_over_the_raw_default_index():
    """resolve_devices must land the monitor on its WASAPI twin, not on whatever host
    API PortAudio's raw default index happens to point at - for BOTH ways a monitor
    gets chosen: monitor_device == "default" (which has to resolve the default
    device's NAME first and re-match it with the WASAPI preference) and an explicitly
    named monitor (which goes straight through _first_match already)."""
    default_result = devices.resolve_devices(MONITOR_TEST_OUTPUTS, cfg(monitor_device="default"), 10)
    assert default_result["voicemeeter"] == 56, default_result
    assert default_result["monitor"] == 13, \
        f"default output index (10, MME) must be re-matched to its WASAPI twin (13), got {default_result}"

    named_result = devices.resolve_devices(
        MONITOR_TEST_OUTPUTS, cfg(monitor_device="Kopfhörer (KT USB Audio)"), 10)
    assert named_result["monitor"] == 13, \
        f"an explicitly named monitor must also prefer WASAPI, got {named_result}"
    print("monitor prefers WASAPI over the raw default index: OK")


# Gemessen 2026-09-20 auf dem Zielrechner (WASAPI), nach der Hi-Fi-Cable-Installation.
# "CABLE In 16ch" ist die Falle: gleicher Praefix wie "CABLE Input", aber ohne
# Aufnahme-Gegenstueck. Eine Praefix-Paarung legt sonst zwei Ziele auf ein Kabel.
PAIR_OUTPUTS = [
    {"index": 5, "name": "CABLE Input (VB-Audio Virtual C", "channels": 16, "hostapi": MME},
    {"index": 10, "name": "Kopfhörer (KT USB Audio)", "channels": 2, "hostapi": MME},
    {"index": 28, "name": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)", "channels": 2,
     "hostapi": WASAPI},
    {"index": 29, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2,
     "hostapi": WASAPI},
    {"index": 32, "name": "CABLE In 16ch (VB-Audio Virtual Cable)", "channels": 16,
     "hostapi": WASAPI},
]

PAIR_INPUTS = [
    {"index": 2, "name": "Mikrofon (Endorfy Solum Voice S Mic)", "channels": 1,
     "hostapi": WASAPI},
    {"index": 38, "name": "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)", "channels": 2,
     "hostapi": WASAPI},
    {"index": 39, "name": "CABLE Output (VB-Audio Virtual Cable)", "channels": 2,
     "hostapi": WASAPI},
]


def test_list_virtual_mics_pairs_by_whole_word():
    pairs = devices.list_virtual_mics(PAIR_OUTPUTS, PAIR_INPUTS)
    by_key = {p["key"]: p for p in pairs}
    assert set(by_key) == {
        "CABLE Output (VB-Audio Virtual Cable)",
        "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)",
    }, by_key

    cable = by_key["CABLE Output (VB-Audio Virtual Cable)"]
    assert cable["out_index"] == 29, "WASAPI beats the MME twin"
    assert cable["in_index"] == 39
    assert cable["label"] == "CABLE"

    hifi = by_key["Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)"]
    assert hifi["out_index"] == 28 and hifi["in_index"] == 38
    assert hifi["label"] == "Hi-Fi Cable"

    # "CABLE In 16ch" has no "CABLE Out 16ch" counterpart and must not borrow
    # "CABLE Output" from the plain cable.
    assert all(p["out_index"] != 32 for p in pairs), pairs
    print("list_virtual_mics pairs by whole word: OK")


def test_list_virtual_mics_needs_both_sides():
    assert devices.list_virtual_mics(PAIR_OUTPUTS, []) == []
    assert devices.list_virtual_mics([], PAIR_INPUTS) == []
    # A real microphone is not a virtual cable.
    assert devices.list_virtual_mics([PAIR_OUTPUTS[1]], PAIR_INPUTS) == []
    print("list_virtual_mics needs both sides: OK")


def test_list_virtual_mics_finds_voicemeeter():
    pairs = devices.list_virtual_mics(OUTPUTS, INPUTS)
    keys = {p["key"] for p in pairs}
    assert "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)" in keys, keys
    vm = next(p for p in pairs if p["key"].startswith("Voicemeeter Out B1"))
    assert vm["out_index"] == 56, "WASAPI VoiceMeeter Input"
    assert vm["in_index"] == 67
    print("list_virtual_mics finds voicemeeter: OK")


def test_list_virtual_mics_is_stable():
    once = devices.list_virtual_mics(PAIR_OUTPUTS, PAIR_INPUTS)
    twice = devices.list_virtual_mics(list(reversed(PAIR_OUTPUTS)), list(reversed(PAIR_INPUTS)))
    assert [p["key"] for p in once] == [p["key"] for p in twice], "order must not depend on input order"
    print("list_virtual_mics is stable: OK")


# Ledger 1.2: a machine with no WASAPI host API at all must still work. This machine
# (the one running the test) DOES have WASAPI, so hostapi_index("Windows WASAPI")
# returns a real int and, on its own, the fixture below - with only one candidate per
# side - never even calls _prefers_hostapi (both call sites short-circuit on
# "entry/mate is None" before reaching it). Driving the `wasapi is None` guard for
# real requires monkeypatching hostapi_index to actually report absence, AND giving
# it competing candidates to choose between - see test body.
MME_ONLY_OUTPUTS = [
    {"index": 5, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2, "hostapi": MME},
]
MME_ONLY_INPUTS = [
    {"index": 6, "name": "CABLE Output (VB-Audio Virtual Cable)", "channels": 2, "hostapi": MME},
    {"index": 7, "name": "Mikrofon (Realtek Audio)", "channels": 1, "hostapi": MME},
]

# Competing candidates for the no-WASAPI case: a second entry on each side whose
# hostapi is None (missing/unknown host API metadata - `.get("hostapi")` returns None
# for a device dict without the key, same as the forced wasapi below). With the
# `wasapi is None` guard in _prefers_hostapi, comparisons always return False and the
# FIRST match keeps winning. Without the guard, `candidate_hostapi == wasapi` (both
# None) would spuriously be True and the second entry would wrongly win instead.
MME_ONLY_OUTPUTS_COMPETING = [
    {"index": 5, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2, "hostapi": MME},
    {"index": 6, "name": "CABLE Input (VB-Audio Virtual Cable)", "channels": 2, "hostapi": None},
]
MME_ONLY_INPUTS_COMPETING = [
    {"index": 7, "name": "CABLE Output (VB-Audio Virtual Cable)", "channels": 2, "hostapi": MME},
]
MME_ONLY_MIC_COMPETING = [
    {"index": 20, "name": "Mikrofon (Realtek Audio)", "channels": 1, "hostapi": MME},
    {"index": 21, "name": "Mikrofon (Realtek Audio) 2", "channels": 1, "hostapi": None},
]


def test_mme_only_machine_still_resolves():
    """A machine with no WASAPI host API at all must still resolve everything, picking
    the first match instead of resolving nothing. Proven for real by forcing
    hostapi_index to report no WASAPI (this test machine actually has one) and giving
    list_virtual_mics/resolve_mic competing candidates, so the `wasapi is None` guard
    inside _prefers_hostapi is the thing actually deciding the outcome."""
    real_hostapi_index = devices.hostapi_index
    devices.hostapi_index = lambda name: None
    try:
        # Baseline: single candidate per side, no competition to resolve.
        pairs = devices.list_virtual_mics(MME_ONLY_OUTPUTS, MME_ONLY_INPUTS)
        assert len(pairs) == 1, pairs
        assert pairs[0]["out_index"] == 5 and pairs[0]["in_index"] == 6, pairs

        mic_cfg = cfg(microphone_name="Mikrofon (Realtek Audio)")
        assert devices.resolve_mic(MME_ONLY_INPUTS, mic_cfg) == 7, \
            "must still resolve the configured mic without any WASAPI host API present"

        # Competing candidates: this is what actually drives the `wasapi is None`
        # branch inside _prefers_hostapi - the first match (index 5) must keep
        # winning over the second entry (index 6), whose hostapi is None.
        pairs2 = devices.list_virtual_mics(MME_ONLY_OUTPUTS_COMPETING, MME_ONLY_INPUTS_COMPETING)
        assert len(pairs2) == 1, pairs2
        assert pairs2[0]["out_index"] == 5, \
            f"no WASAPI on this machine: the FIRST match must win, got {pairs2}"

        # Same for resolve_mic: two matches for the same mic name, the second with
        # hostapi=None must not leapfrog the first.
        assert devices.resolve_mic(MME_ONLY_MIC_COMPETING, mic_cfg) == 20, \
            "no WASAPI on this machine: the FIRST matching mic must win"
    finally:
        devices.hostapi_index = real_hostapi_index
    print("MME-only machine (no WASAPI host API) still resolves: OK")


# Ledger 1.3 (hardware finding): 'CABLE Input' used to substring-match 'Hi-Fi Cable
# Input' too. Hi-Fi's WASAPI entry sits at the lower index (28 < 29) in PAIR_OUTPUTS
# and _first_match's "first WASAPI match wins, no later match dislodges it" rule kept
# it forever once found - exactly the machine that raised PaErrorCode -9997, because
# resolve_virtual_mic handed the "Prüfen" check Hi-Fi's indices instead of VB-CABLE's.
def test_resolve_virtual_mic_never_picks_hifi_for_cable_mode():
    resolved = devices.resolve_virtual_mic(PAIR_OUTPUTS, PAIR_INPUTS, cfg(), 10)
    assert resolved["mode"] == "cable", resolved
    assert resolved["out_index"] == 29, f"must be VB-CABLE's output, not Hi-Fi's 28: {resolved}"
    assert resolved["in_index"] == 39, f"must be VB-CABLE's input, not Hi-Fi's 38: {resolved}"
    assert resolved["out_name"] == "CABLE Input (VB-Audio Virtual Cable)", resolved
    assert resolved["discord_device_name"] == "CABLE Output (VB-Audio Virtual Cable)", resolved

    # Hi-Fi appearing first in list order must not change the outcome either.
    reversed_outputs = list(reversed(PAIR_OUTPUTS))
    reversed_inputs = list(reversed(PAIR_INPUTS))
    resolved2 = devices.resolve_virtual_mic(reversed_outputs, reversed_inputs, cfg(), 10)
    assert resolved2["out_index"] == 29, f"Hi-Fi first in the list must still lose: {resolved2}"
    assert resolved2["in_index"] == 39, f"Hi-Fi first in the list must still lose: {resolved2}"
    print("resolve_virtual_mic never picks Hi-Fi for cable mode: OK")


def test_resolve_mic_refuses_loopback_input():
    """Stereo Mix/What U Hear must never feed desktop audio back into the voice path."""
    loopback = [
        {"index": 90, "name": "Stereo Mix (Realtek Audio)", "channels": 2, "hostapi": WASAPI},
        {"index": 91, "name": "Mikrofon (Realtek Audio)", "channels": 1, "hostapi": WASAPI},
    ]
    assert devices.resolve_mic(loopback, cfg(microphone_name="Stereo Mix (Realtek Audio)")) is None
    assert devices.resolve_mic(loopback, cfg(microphone_name="Nicht da"), 90) is None
    assert devices.resolve_mic(loopback, cfg(microphone_name="Nicht da"), 91) == 91
    print("resolve_mic refuses loopback input: OK")


MIC_INPUTS = [
    {"index": 1, "name": "Mikrofon (Endorfy Solum Voice S", "channels": 1, "hostapi": MME},
    {"index": 34, "name": "Mikrofon (Endorfy Solum Voice S Mic)", "channels": 1, "hostapi": WASAPI},
    {"index": 35, "name": "CABLE Output (VB-Audio Virtual Cable)", "channels": 2, "hostapi": WASAPI},
    {"index": 36, "name": "Stereomix (Realtek(R) Audio)", "channels": 2, "hostapi": WASAPI},
    {"index": 37, "name": "Mikrofon (NVIDIA Broadcast)", "channels": 1, "hostapi": WASAPI},
]


def test_list_microphones_hides_cables_and_loopback():
    names = devices.list_microphones(MIC_INPUTS, prefer_hostapi=WASAPI)
    assert names == ["Mikrofon (Endorfy Solum Voice S Mic)", "Mikrofon (NVIDIA Broadcast)"], names
    only_mme = devices.list_microphones(MIC_INPUTS[:1], prefer_hostapi=WASAPI)
    assert only_mme == ["Mikrofon (Endorfy Solum Voice S"], "no WASAPI entry: use what exists"
    print("microphone list hides cables and loopback: OK")


def test_broadcast_is_a_valid_microphone():
    assert devices.is_broadcast_name("Mikrofon (NVIDIA Broadcast)")
    assert not devices.is_broadcast_name("Mikrofon (Endorfy Solum Voice S Mic)")
    assert not devices.is_broadcast_name(None)
    cfg_bc = {"microphone_name": "Mikrofon (NVIDIA Broadcast)"}
    assert devices.resolve_mic(MIC_INPUTS, cfg_bc) == 37
    print("NVIDIA Broadcast resolves as the microphone: OK")


def test_mic_choices_moved_into_devices():
    raw = [
        {"index": 0, "name": "Microsoft Soundmapper - Input", "channels": 2},
        {"index": 1, "name": "Mikrofon (Endorfy Solum Voice S", "channels": 1},
        {"index": 2, "name": "Mikrofon (eMeet Nova)", "channels": 1},
        {"index": 3, "name": "VoiceMeeter Output (VB-Audio Vo", "channels": 2},
        {"index": 9, "name": "Primärer Soundaufnahmetreiber", "channels": 2},
        {"index": 10, "name": "Mikrofon (Endorfy Solum Voice S Mic)", "channels": 1},
        {"index": 11, "name": "Mikrofon (eMeet Nova)", "channels": 1},
    ]
    assert devices.mic_choices(raw) == ["Mikrofon (Endorfy Solum Voice S Mic)", "Mikrofon (eMeet Nova)"]
    assert devices.mic_choices([]) == []
    from soundboard import onboarding
    assert onboarding.mic_choices is devices.mic_choices, "old import path keeps working"
    choices, default = devices.system_mic_choices()  # live, read-only
    assert isinstance(choices, list) and (default is None or default in choices)
    print("mic_choices lives in devices, live query answers: OK")


def main():
    test_voicemeeter_only_names_the_b1_device()
    test_cable_wins_in_auto_mode()
    test_mme_truncation_still_resolves()
    test_monitor_never_equals_the_virtual_mic()
    test_nothing_found()
    test_resolve_mic_prefers_wasapi()
    test_resolve_mic_refuses_a_virtual_fallback()
    test_cable_output_prefers_wasapi()
    test_monitor_prefers_wasapi_over_the_raw_default_index()
    test_list_virtual_mics_pairs_by_whole_word()
    test_list_virtual_mics_needs_both_sides()
    test_list_virtual_mics_finds_voicemeeter()
    test_list_virtual_mics_is_stable()
    test_mme_only_machine_still_resolves()
    test_resolve_virtual_mic_never_picks_hifi_for_cable_mode()
    test_resolve_mic_refuses_loopback_input()
    test_list_microphones_hides_cables_and_loopback()
    test_broadcast_is_a_valid_microphone()
    test_mic_choices_moved_into_devices()
    print("\nALL DEVICE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
