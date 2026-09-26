"""Audio device discovery via sounddevice/PortAudio.

Two things are resolved here, both by NAME (indices change whenever a driver is
installed or a USB device is plugged in):

* the **virtual microphone** - the output device Ruckus Radio plays into plus the
  matching input device the voice chat has to record from. `VIRTUAL_MICS` lists
  the supported pairs in preference order.
* the **monitor** (your own headphones) and the **real microphone**.
"""

import ctypes

import sounddevice as sd

COINIT_MULTITHREADED = 0


def init_thread_com() -> None:
    """Initialise COM (MTA) on the calling thread; call first on every thread that
    opens PortAudio streams. PortAudio only sets COM up on the thread that ran
    Pa_Initialize, and a WASAPI stream started on any other thread without COM fails
    with PaErrorCode -9999 ("Unanticipated host error"). Never uninitialised: the
    thread keeps COM for its lifetime. No-op off Windows."""
    try:
        ole32 = ctypes.windll.ole32
    except AttributeError:
        return
    ole32.CoInitializeEx(None, COINIT_MULTITHREADED)  # S_FALSE / changed mode: fine


def _hostapi_names() -> list[str]:
    try:
        return [a["name"] for a in sd.query_hostapis()]
    except Exception:
        return []


def hostapi_index(name: str) -> int | None:
    """Index of a host API by name, e.g. 'Windows WASAPI'. None when absent."""
    for i, api in enumerate(_hostapi_names()):
        if api == name:
            return i
    return None


def list_output_devices() -> list[dict]:
    """Return all devices with at least one output channel."""
    devices = sd.query_devices()
    return [
        {"index": i, "name": d["name"], "channels": d["max_output_channels"], "hostapi": d.get("hostapi")}
        for i, d in enumerate(devices)
        if d["max_output_channels"] > 0
    ]


def list_input_devices() -> list[dict]:
    """Return all devices with at least one input channel."""
    devices = sd.query_devices()
    return [
        {"index": i, "name": d["name"], "channels": d["max_input_channels"], "hostapi": d.get("hostapi")}
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]


def default_output_device() -> int:
    return sd.default.device[1]


def default_input_device() -> int:
    return sd.default.device[0]


VOICEMEETER_FALLBACK = "VoiceMeeter Input"

# (mode key, label, output device the app plays into, input device the voice chat
# records from). Order = preference when picking ONE device to recommend in the
# onboarding assistant (step 3) - the needles are deliberately short: MME truncates
# device names to 31 characters, so a full 44-char name never matches there.
VIRTUAL_MICS = [
    ("cable", "VB-CABLE", "CABLE Input", "CABLE Output"),
    ("voicemeeter", "VoiceMeeter", VOICEMEETER_FALLBACK, "Voicemeeter Out B1"),
]

# Namensteile, an denen ein virtuelles Kabel erkannt wird. Ein echtes Mikrofon oder
# ein Kopfhoerer darf hier nie hineinfallen.
VIRTUAL_NEEDLES = ("cable", "voicemeeter")

# Wort-Paare, die Ausgangs- und Aufnahmeseite desselben Kabels verbinden.
# Ganze Woerter, keine Praefixe: "CABLE In 16ch" faengt genauso an wie "CABLE Input",
# hat aber kein Gegenstueck, und eine Praefix-Paarung legte sonst zwei Ziele auf ein Kabel.
SIDE_WORDS = (("Input", "Output"), ("Input", "Out B1"))


def _is_virtual_name(name: str) -> bool:
    low = name.lower()
    return any(needle in low for needle in VIRTUAL_NEEDLES)


# Windows exposes several playback-capture devices as inputs. They are useful for
# recording desktop audio, but must never become Ruckus's microphone: feeding one into
# the mixer sends PC audio (and potentially the mixer's own output) back to Discord.
LOOPBACK_INPUT_NEEDLES = (
    "stereo mix", "stereomix", "what u hear", "wave out mix", "loopback",
    "playback capture", "soundmapper", "sound mapper", "primary sound capture",
    "soundaufnahmetreiber",
)


def _is_loopback_name(name: str) -> bool:
    low = name.lower()
    return any(needle in low for needle in LOOPBACK_INPUT_NEEDLES)


def is_broadcast_name(name: str | None) -> bool:
    """NVIDIA Broadcast's virtual microphone: noise removal on the voice only, which is
    exactly what the microphone branch wants - never treated as a cable."""
    return "nvidia broadcast" in (name or "").lower()


def list_microphones(input_devices: list[dict], prefer_hostapi: int | None = None) -> list[str]:
    """Pure: names the user may pick as microphone, once each, in device order.

    Virtual cables and loopback inputs never appear: picking one feeds the mixer its
    own output. Only one host API is listed so a name does not show up three times
    (MME even cuts it to 31 characters) - WASAPI when present, else whatever exists."""
    pool = [d for d in input_devices
            if prefer_hostapi is not None and d.get("hostapi") == prefer_hostapi]
    names: list[str] = []
    for device in pool or input_devices:
        name = device["name"]
        if _is_virtual_name(name) or _is_loopback_name(name) or name in names:
            continue
        names.append(name)
    return names


def _split_side(name: str, word: str) -> tuple[str, str] | None:
    """('CABLE Input (VB-Audio Virtual Cable)', 'Input') -> ('CABLE', '(VB-Audio ...)').

    Splits on the WHOLE word only, so 'CABLE In 16ch' never matches 'Input'."""
    idx = name.find(word)
    if idx == -1:
        return None
    # The word has to stand alone: the character right before it (in the
    # ORIGINAL string, not after stripping) must not be a letter/digit.
    if idx > 0 and name[idx - 1].isalnum():
        return None
    return name[:idx].strip(), name[idx + len(word):].strip()


def _prefers_hostapi(current_hostapi, candidate_hostapi, wasapi) -> bool:
    """True when a candidate on `candidate_hostapi` should replace one on
    `current_hostapi`, under the "prefer WASAPI, else keep the first match" rule
    shared by `_first_match` and `list_virtual_mics`.

    Both need this because the same physical virtual device is listed once per host
    API (MME/DirectSound/WASAPI). When `wasapi` is None (no WASAPI host API on this
    machine) this always returns False, so the first match found simply wins - the
    guard that makes an MME-only machine keep working."""
    if wasapi is None:
        return False
    return candidate_hostapi == wasapi and current_hostapi != wasapi


def _best_by_hostapi(candidates: list[dict], prefer_hostapi) -> dict:
    """First candidate, unless a later one sits on `prefer_hostapi` and the current
    pick does not - the tie-break rule `_first_match` and `_first_whole_word_match`
    share (also mirrored, inline, by `list_virtual_mics`)."""
    best = candidates[0]
    for candidate in candidates[1:]:
        if _prefers_hostapi(best.get("hostapi"), candidate.get("hostapi"), prefer_hostapi):
            best = candidate
    return best


def list_virtual_mics(output_devices: list[dict], input_devices: list[dict]) -> list[dict]:
    """Every virtual cable that has BOTH sides, WASAPI preferred, sorted by label.

    Each entry: {"key", "label", "out_index", "in_index", "out_name", "in_name"}.
    `key` is the recording device's name - what the user picks in Discord, and the
    key under config["outputs"]. A cable whose counterpart is missing is dropped."""
    wasapi = hostapi_index("Windows WASAPI")
    found: dict[str, dict] = {}

    for out_word, in_word in SIDE_WORDS:
        for out_dev in output_devices:
            if not _is_virtual_name(out_dev["name"]):
                continue
            split = _split_side(out_dev["name"], out_word)
            if split is None:
                continue
            label = split[0]
            if not label:
                continue
            # The recording side carries the same label and the matching word.
            mate = None
            for in_dev in input_devices:
                if not _is_virtual_name(in_dev["name"]):
                    continue
                in_split = _split_side(in_dev["name"], in_word)
                if in_split is not None and in_split[0] == label:
                    if mate is None or _prefers_hostapi(mate.get("hostapi"),
                                                        in_dev.get("hostapi"), wasapi):
                        mate = in_dev
            if mate is None:
                continue
            entry = found.get(label)
            better = entry is None or _prefers_hostapi(entry["_out_hostapi"],
                                                        out_dev.get("hostapi"), wasapi)
            if better:
                found[label] = {
                    "key": mate["name"], "label": label,
                    "out_index": out_dev["index"], "in_index": mate["index"],
                    "out_name": out_dev["name"], "in_name": mate["name"],
                    "_out_hostapi": out_dev.get("hostapi"),
                }

    result = [{k: v for k, v in e.items() if not k.startswith("_")}
              for e in found.values()]
    return sorted(result, key=lambda e: e["label"])


def _device_by_index(output_devices: list[dict], index: int) -> dict | None:
    return next((d for d in output_devices if d["index"] == index), None)


def _first_match(output_devices: list[dict], name: str | None, prefer_hostapi=None) -> int | None:
    """First device whose name contains `name` (case-insensitive). When several match
    and `prefer_hostapi` is given, a match on that host API wins; otherwise the first
    match in list order wins (e.g. MME/DirectSound/WASAPI entries for the same device)."""
    if not name:
        return None
    needle = name.lower()
    matches = [d for d in output_devices if needle in d["name"].lower()]
    if not matches:
        return None
    return _best_by_hostapi(matches, prefer_hostapi)["index"]


# The two words `_split_side` ever splits on (see SIDE_WORDS) - also the only words a
# VIRTUAL_MICS needle can end in, so `_needle_label_and_word` knows what to anchor on.
_NEEDLE_WORDS = tuple({word for pair in SIDE_WORDS for word in pair})


def _needle_label_and_word(needle: str) -> tuple[str, str] | None:
    """('CABLE Input', ) -> ('CABLE', 'Input'); ('Voicemeeter Out B1', ) -> ('Voicemeeter',
    'Out B1'). None when `needle` does not end in one of `_NEEDLE_WORDS` - the caller
    then falls back to a plain substring match."""
    for word in _NEEDLE_WORDS:
        suffix = " " + word
        if needle.endswith(suffix) and len(needle) > len(suffix):
            return needle[: -len(suffix)], word
    return None


def _first_whole_word_match(devices_list: list[dict], needle: str | None,
                            prefer_hostapi=None) -> int | None:
    """Like `_first_match`, but a needle ending in a known word (see `SIDE_WORDS`) only
    matches a device whose name splits - on that WHOLE word, via `_split_side` - into
    exactly that label.

    This is the same trap `_split_side`'s own docstring calls out, one level up: plain
    `_first_match`'s "'name' in device name" check treats 'CABLE Input' as a match
    inside 'Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)' too, since the substring really
    is there. Hi-Fi's WASAPI entry then keeps winning over `_prefers_hostapi`'s "first
    WASAPI match wins, nothing later dislodges it" rule even once VB-CABLE's own WASAPI
    entry is reached - the exact bug that handed the "Prüfen" check and the onboarding
    assistant Hi-Fi Cable's indices whenever both cables were installed. Splitting off
    the label first and comparing it whole, the way `list_virtual_mics` already does
    for pairing, tells the two apart: 'Hi-Fi Cable' != 'CABLE'."""
    split_needle = _needle_label_and_word(needle) if needle else None
    if split_needle is None:
        return _first_match(devices_list, needle, prefer_hostapi)
    label, word = split_needle
    matches = []
    for dev in devices_list:
        split = _split_side(dev["name"], word)
        if split is not None and split[0].lower() == label.lower():
            matches.append(dev)
    if not matches:
        return None
    return _best_by_hostapi(matches, prefer_hostapi)["index"]


def _is_virtual_mic_device(output_devices: list[dict], index: int) -> bool:
    device = _device_by_index(output_devices, index)
    if device is None:
        return False
    return _is_virtual_name(device["name"])


def _resolve_virtual_output(output_devices: list[dict], cfg: dict) -> tuple[str | None, int | None]:
    """(mode, output index) of the virtual mic the onboarding assistant recommends.

    The matrix in Einstellungen decides what actually goes where; this only picks one
    sensible device to name in step 3. WASAPI preferred, see list_virtual_mics."""
    wasapi = hostapi_index("Windows WASAPI")
    for key, _label, out_needle, _in_needle in VIRTUAL_MICS:
        needle = cfg.get("voicemeeter_device_name") if key == "voicemeeter" else None
        index = _first_match(output_devices, needle, prefer_hostapi=wasapi)
        if index is None:
            # Whole-word match, not `_first_match`'s plain substring one: see
            # `_first_whole_word_match` for why "CABLE Input" must not also match
            # "Hi-Fi Cable Input".
            index = _first_whole_word_match(output_devices, out_needle, prefer_hostapi=wasapi)
        if index is not None:
            return key, index
    return None, None


def resolve_devices(output_devices: list[dict], cfg: dict, default_index: int | None) -> dict:
    """Pure: map configured device NAMES to current indices.
    Returns {"voicemeeter": idx|None, "monitor": idx|None, "connected": bool}."""
    wasapi = hostapi_index("Windows WASAPI")
    default = default_index if default_index is not None and default_index >= 0 else None
    wanted = cfg.get("monitor_device") or "default"
    if wanted == "default":
        # "default" names no device, so it bypasses _first_match entirely - resolve the
        # default output's NAME instead and re-match it with the same WASAPI preference
        # every other output gets. Without this the monitor keeps whatever host API
        # PortAudio happened to list the default device under first (usually MME),
        # and it now runs through the mixer's real-time callback like everything else.
        default_device = _device_by_index(output_devices, default) if default is not None else None
        monitor = (_first_match(output_devices, default_device["name"], prefer_hostapi=wasapi)
                  if default_device else None)
    else:
        monitor = _first_match(output_devices, wanted, prefer_hostapi=wasapi)
    if monitor is None:
        monitor = default

    vm = _resolve_virtual_output(output_devices, cfg)[1]

    # MME truncates names to 31 chars while the configured VoiceMeeter name is the full
    # 44-char string, so the monitor (esp. "default") can resolve to a *different index*
    # that is nonetheless the same physical virtual device -> drop it, don't double up.
    # Only when a virtual input was resolved: otherwise that device is the only output left.
    if monitor is not None and vm is not None and (
            monitor == vm or _is_virtual_mic_device(output_devices, monitor)):
        monitor = None

    monitor_device = _device_by_index(output_devices, monitor) if monitor is not None else None
    return {"voicemeeter": vm, "monitor": monitor,
            "monitor_name": monitor_device["name"] if monitor_device else None,
            "connected": vm is not None}


def resolve_virtual_mic(output_devices: list[dict], input_devices: list[dict], cfg: dict,
                        default_index: int | None) -> dict:
    """Pure: everything the mixer and the dock need to describe the virtual mic path.

    {"mode", "label", "out_index", "in_index", "out_name", "discord_device_name",
     "connected"} - `connected` only means the output device exists. Whether audio
    actually reaches the voice chat is what `miccheck.verify_path` answers."""
    mode, out_index = _resolve_virtual_output(output_devices, cfg)
    out_device = _device_by_index(output_devices, out_index) if out_index is not None else None
    entry = next((e for e in VIRTUAL_MICS if e[0] == mode), None)

    in_index = None
    discord_name = None
    if entry is not None:
        # Same whole-word trap as the output side: "CABLE Output" must not also match
        # "Hi-Fi Cable Output".
        in_index = _first_whole_word_match(input_devices, entry[3],
                                           prefer_hostapi=hostapi_index("Windows WASAPI"))
        in_device = _device_by_index(input_devices, in_index) if in_index is not None else None
        discord_name = in_device["name"] if in_device else entry[3]

    return {
        "mode": mode,
        "label": entry[1] if entry else None,
        "out_index": out_index,
        "in_index": in_index,
        "out_name": out_device["name"] if out_device else None,
        "discord_device_name": discord_name,
        "connected": out_index is not None,
    }


def resolve_mic(input_devices: list[dict], cfg: dict, default_index: int | None = None) -> int | None:
    """Index of the real microphone from cfg["microphone_name"], WASAPI preferred
    (lowest capture latency). Falls back to the system default input - but never to a
    virtual cable's recording side: when the configured mic is missing (unplugged) and
    PortAudio's default input happens to be e.g. "Hi-Fi Cable Output", opening it as the
    microphone would feed the mixer's own output straight back into itself, saturating
    into a loud squeal on whatever listens on that cable (e.g. Discord). A configured
    name that explicitly names a virtual device is the user's own choice and is not
    second-guessed here - only the unnamed fallback is."""
    index = _first_match(input_devices, cfg.get("microphone_name"),
                         prefer_hostapi=hostapi_index("Windows WASAPI"))
    if index is not None:
        selected = _device_by_index(input_devices, index)
        if selected is not None and (_is_virtual_name(selected["name"])
                                     or _is_loopback_name(selected["name"])):
            return None
        return index
    if default_index is not None and default_index >= 0:
        default_device = _device_by_index(input_devices, default_index)
        if default_device is not None and (_is_virtual_name(default_device["name"])
                                           or _is_loopback_name(default_device["name"])):
            return None
        return default_index
    return None


def _snapshot() -> tuple[list[dict], list[dict], int | None, int | None]:
    """(outputs, inputs, default output, default input); empty/None when PortAudio fails."""
    try:
        return list_output_devices(), list_input_devices(), default_output_device(), default_input_device()
    except Exception:
        return [], [], None, None


def _describe(cfg: dict) -> dict:
    outputs, inputs, default_out, default_in = _snapshot()
    resolved = resolve_devices(outputs, cfg, default_out)
    resolved["virtual_mic"] = resolve_virtual_mic(outputs, inputs, cfg, default_out)
    resolved["virtual_mics"] = list_virtual_mics(outputs, inputs)
    resolved["mic"] = resolve_mic(inputs, cfg, default_in)
    mic_device = _device_by_index(inputs, resolved["mic"]) if resolved["mic"] is not None else None
    resolved["mic_name"] = mic_device["name"] if mic_device else None
    resolved["microphones"] = list_microphones(inputs, hostapi_index("Windows WASAPI"))
    return resolved


def rescan_system_devices(cfg: dict, reinit: bool) -> dict:
    """resolve_system_devices plus the virtual mic's own name/index. PortAudio caches its
    device list at init, so reinit=True restarts it to see newly installed drivers - only
    allowed while no stream is open (caller's job). Never raises."""
    if reinit:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
    resolved = _describe(cfg)
    vm = resolved["virtual_mic"]
    resolved.update(found=vm["connected"], name=vm["out_name"], index=vm["out_index"])
    return resolved


def resolve_system_devices(cfg: dict) -> dict:
    """resolve_devices against the live PortAudio device list; never raises."""
    return _describe(cfg)


PSEUDO_INPUTS = ("soundmapper", "sound mapper", "soundaufnahmetreiber", "primary sound capture")


def mic_choices(input_devices: list[dict]) -> list[str]:
    """Real microphones by name, once each. Drops Windows pseudo devices, the virtual
    mics' own recording ends (VoiceMeeter, VB-CABLE) and MME's 31-char truncations."""
    names: list[str] = []
    for d in input_devices:
        name = d["name"].strip()
        low = name.lower()
        if not name or _is_virtual_name(name) or any(p in low for p in PSEUDO_INPUTS):
            continue
        truncated = next((i for i, n in enumerate(names) if name != n and name.startswith(n)), None)
        if truncated is not None:
            names[truncated] = name
        elif not any(n.startswith(name) for n in names):
            names.append(name)
    return names


def system_mic_choices() -> tuple[list[str], str | None]:
    """The assistant's microphone list plus the Windows default among it. Live
    PortAudio query - device thread only (routing.DeviceBackend.mic_choices)."""
    try:
        inputs = list_input_devices()
        default_index = default_input_device()
    except Exception:
        return [], None
    choices = mic_choices(inputs)
    default = next((d["name"] for d in inputs if d["index"] == default_index), None)
    if default:
        default = next((c for c in choices if c.startswith(default)), None)
    return choices, default
