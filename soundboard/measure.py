"""Measurement mode for probe build B0 (spec §6.1). Off unless RUCKUS_MEASURE_DIR
is set - tools/measure_b0.ps1 sets it together with a throw-away RUCKUS_DATA_DIR;
without RUCKUS_DATA_DIR the measurement refuses to run.

Both interfaces are measured the same way, from a background thread after
RUCKUS_MEASURE_IDLE seconds (default 35, so the script can read the RAM after 30 s
of idle first):
  1. resize: 20 window sizes; Tk: time until the layout is idle, web: time from
     the DOM resize event to the second painted frame (webui/src/lib/measure.ts)
  2. hotkey: the interface is blocked for 3 s, the measure sound's hotkey is sent
     with the keyboard library; latency = until PlaybackStarted arrives from the core
  3. cable: the measure sound is played through the interface (web: through the
     page and the bridge), dropped_blocks of every sink target are compared before
     and after, then the core's signal check runs
Results go to tk.json / web.json; then the app closes itself."""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config
from .protocol import PlaybackStarted, Play, RunSignalCheck, SignalCheckDone, StopAll

log = logging.getLogger(__name__)

ENV = "RUCKUS_MEASURE_DIR"
DATA_ENV = "RUCKUS_DATA_DIR"
IDLE_ENV = "RUCKUS_MEASURE_IDLE"
RESIZE_LIMIT_MS = 50.0
HOTKEY_LIMIT_MS = 150.0
BLOCK_S = 3.0
# Injected key presses (keyboard.send) never reach the keyboard hook on some machines,
# so the user can press the measure hotkey by hand: the block is longer, a beep marks
# its start, and latency runs from the real key-down (hooked) to PlaybackStarted.
MANUAL_ENV = "RUCKUS_MEASURE_MANUAL_HOTKEY"
MANUAL_BLOCK_S = 8.0
SOUND_INFO = "measure-sound.json"


def out_dir() -> Path | None:
    value = os.environ.get(ENV)
    return Path(value) if value else None


def enabled() -> bool:
    """On only with RUCKUS_MEASURE_DIR *and* a throw-away RUCKUS_DATA_DIR: the measurement
    plays sounds and closes the app, so it never runs against the real %APPDATA%\\Soundboard."""
    if out_dir() is None:
        return False
    if not os.environ.get(DATA_ENV):
        log.warning("%s is set without %s: measurement refused (never on the real data folder)", ENV, DATA_ENV)
        return False
    return True


def write_json(name: str, data: Any) -> Path | None:
    if not enabled():
        return None
    folder = out_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def mark_ready(ui: str) -> None:
    write_json(f"ready-{ui}.json", {"ui": ui, "t": time.time()})


# ---- pure helpers ----

def resize_plan(steps: int = 20, base: tuple[int, int] = (1280, 820)) -> list[tuple[int, int]]:
    """Alternating sizes around `base`, all above the web window's minimum (960x620)."""
    deltas = (-240, 200, -120, 280, -200, 120, -280, 240, -160, 160)
    plan = []
    for i in range(steps):
        dw = deltas[i % len(deltas)]
        dh = -(dw // 2) if i % 2 else dw // 3
        plan.append((max(960, min(1600, base[0] + dw)), max(620, min(1000, base[1] + dh))))
    return plan


def summarize(step_ms: list[float], limit_ms: float = RESIZE_LIMIT_MS) -> dict:
    values = [float(v) for v in step_ms]
    if not values:
        return {"steps": 0, "max_ms": None, "median_ms": None, "over_limit": 0, "limit_ms": limit_ms, "ok": False}
    top = max(values)
    return {"steps": len(values), "max_ms": round(top, 1), "median_ms": round(statistics.median(values), 1),
            "over_limit": sum(v > limit_ms for v in values), "limit_ms": limit_ms, "ok": top <= limit_ms}


class PlaybackProbe:
    """Time (clock()) of the first PlaybackStarted for `sound_id` - seen on the core thread."""

    def __init__(self, core, sound_id: str, clock: Callable[[], float] = time.perf_counter):
        self._id = sound_id
        self._clock = clock
        self._event = threading.Event()
        self.at: float | None = None
        self._off = core.subscribe(self._on_event)

    def _on_event(self, event) -> None:
        if isinstance(event, PlaybackStarted) and event.sound_id == self._id and self.at is None:
            self.at = self._clock()
            self._event.set()

    def wait(self, timeout: float) -> float | None:
        return self.at if self._event.wait(timeout) else None

    def close(self) -> None:
        self._off()


class KeyPressProbe:
    """Time (clock()) of the first key-down of the hotkey's main key (e.g. f9)."""

    def __init__(self, hotkey: str, clock: Callable[[], float] = time.perf_counter):
        self.key = hotkey.split("+")[-1].strip().lower()
        self._clock = clock
        self._event = threading.Event()
        self.at: float | None = None

    def on_event(self, event) -> None:
        if (getattr(event, "event_type", None) == "down" and self.at is None
                and str(getattr(event, "name", "")).lower() == self.key):
            self.at = self._clock()
            self._event.set()

    def wait(self, timeout: float) -> float | None:
        return self.at if self._event.wait(timeout) else None


def manual_hotkey() -> bool:
    return os.environ.get(MANUAL_ENV) == "1"


def block_seconds() -> float:
    return MANUAL_BLOCK_S if manual_hotkey() else BLOCK_S


def dropped_blocks(core) -> dict[str, int]:
    sink = getattr(getattr(core, "routing", None), "_sink", None)
    targets = getattr(sink, "targets", None) or []
    return {str(getattr(t, "key", i)): int(getattr(t, "dropped_blocks", 0)) for i, t in enumerate(targets)}


def verdict(tk: dict, web: dict) -> list[str]:
    """The hard values of spec §6.1; an empty list means B0 passes technically."""
    fails = []
    resize = web.get("resize") or {}
    if not resize.get("ok"):
        fails.append(f"Fenster-Anpassen (Web): größter Schritt {resize.get('max_ms')} ms, Grenze 50 ms")
    latency = (web.get("hotkey") or {}).get("latency_ms")
    if latency is None or latency > HOTKEY_LIMIT_MS:
        fails.append(f"Hotkey bei blockierter Web-Oberfläche: {latency} ms, Grenze {HOTKEY_LIMIT_MS:.0f} ms")
    cable = web.get("cable") or {}
    if not cable.get("played") or cable.get("signal_ok") is not True:
        fails.append("Ton am Kabel (Web): Wiedergabe oder Signalprüfung fehlgeschlagen")
    web_drop = sum((cable.get("dropped_delta") or {}).values())
    tk_drop = sum(((tk.get("cable") or {}).get("dropped_delta") or {}).values())
    if web_drop > tk_drop:
        fails.append(f"dropped_blocks: Web {web_drop} > Tk {tk_drop}")
    return fails


def _fmt(value, unit: str = "") -> str:
    return "–" if value is None else f"{value}{unit}"


def _security(sec: dict | None) -> str:
    if not sec:
        return "–"
    api = "ok" if sec.get("api_ok") else f"abweichend: {sec.get('api_keys')}"
    return f"API {api}, CSP-Verstöße: {len(sec.get('csp_violations') or [])}"


def render_report(tk: dict, web: dict, exe: dict, when: str) -> str:
    fails = verdict(tk, web)
    tk_r, web_r = tk.get("resize") or {}, web.get("resize") or {}
    tk_c, web_c = tk.get("cable") or {}, web.get("cable") or {}
    rows = [
        ("Fenster-Anpassen (größter Schritt, Median)", f"{_fmt(tk_r.get('max_ms'), ' ms')} / {_fmt(tk_r.get('median_ms'), ' ms')}",
         f"{_fmt(web_r.get('max_ms'), ' ms')} / {_fmt(web_r.get('median_ms'), ' ms')}", "hart: Web ≤ 50 ms je Schritt"),
        ("Hotkey bei blockierter Oberfläche" + (" (von Hand gedrückt)" if (web.get("hotkey") or {}).get("manual") else ""), _fmt((tk.get("hotkey") or {}).get("latency_ms"), " ms"),
         _fmt((web.get("hotkey") or {}).get("latency_ms"), " ms"), f"hart: ≤ {HOTKEY_LIMIT_MS:.0f} ms"),
        ("Ton am Kabel (Wiedergabe + Signalprüfung)", "ok" if tk_c.get("played") and tk_c.get("signal_ok") else "fehlt",
         "ok" if web_c.get("played") and web_c.get("signal_ok") else "fehlt", "hart: Ton kommt an"),
        ("dropped_blocks während des Messtons", str(sum((tk_c.get("dropped_delta") or {}).values())),
         str(sum((web_c.get("dropped_delta") or {}).values())), "hart: Web ≤ Tk"),
        ("RAM nach 30 s Leerlauf (inkl. WebView2)", _fmt(tk.get("ram_mb"), " MB"), _fmt(web.get("ram_mb"), " MB"), "notiert"),
        ("Kaltstart bis Oberfläche bereit", _fmt(tk.get("cold_s"), " s"), _fmt(web.get("cold_s"), " s"), "notiert"),
        ("exe-Größe (vorher / nachher)", _fmt(exe.get("before_mb"), " MB"), _fmt(exe.get("after_mb"), " MB"), "notiert"),
        ("JS-API und CSP (nur Web)", "–", _security(web.get("security")), "Beleg Spec §6: nur send/get_state/ready, keine CSP-Verstöße"),
    ]
    lines = [
        "# B0 – Messwerte Tk vs. Web-Oberfläche",
        "",
        f"Stand: {when} · erzeugt von `tools/measure_b0.ps1` (Spec `2026-09-26-b0-prototyp-design.md` §6.1)",
        "",
        "| Messwert | Tk | Web | Grenze |",
        "|---|---|---|---|",
        *[f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows],
        "",
        "Vergleichswerte vor B0: exe 108 MB, Start 4,7 s, Fenster-Anpassen ~0,4 s je Schritt (Tk).",
        "",
        "## Entscheidung",
        "",
    ]
    if fails:
        lines += ["**Harte Werte: nicht bestanden.** Neu entscheiden (Rückfallebene Tauri + Python-Sidecar, Spec §6.1):", ""]
        lines += [f"- {f}" for f in fails]
    else:
        lines += ["**Harte Werte: bestanden.** Zusammen mit dem Urteil zum Look (`b0-sichtpruefung.md`) ist B0 bestanden, "
                  "weiter mit Spec und Plan für B1/B2."]
    lines += ["", "## Rohdaten", "", "```json", json.dumps({"tk": tk, "web": web, "exe": exe}, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(lines)


# ---- growth probe (spec §5, §11) ----
# The B0 value (503.7 MB) was a cold start summed over the whole process tree, where
# shared pages are counted once per process. This probe measures the WARM profile, per
# process, and above all whether the number MOVES. A flat curve is the goal; the
# absolute value is not.

GROWTH_LIMIT_MB_PER_1000 = 60.0  # additional MB per 1000 sounds
IDLE_LIMIT_MB = 40.0             # growth during 30 minutes of idling
RELOAD_TOLERANCE_MB = 60.0       # how far above the baseline a reload may land
GROWTH_ENV = "RUCKUS_MEASURE_GROWTH_SOUNDS"


def growth_counts() -> list[int]:
    raw = os.environ.get(GROWTH_ENV, "500,2000")
    counts = [int(part.strip()) for part in raw.split(",") if part.strip().isdigit()]
    return counts or [500, 2000]


UI_PROCESS = "msedgewebview2.exe"
APP_PROCESS = "RuckusRadio.exe"


def _ui_mb(reading: dict) -> float:
    """What the interface costs. The limits judge only this: the app process grows with
    the decoded audio cache on purpose (every sound up to LAZY_DECODE_BYTES is decoded at
    start), which is reported, not judged (Jan, 2026-09-27). A reading without the
    per-name split counts as interface as a whole."""
    if "by_name" not in reading:
        return reading["ram_mb"]
    return sum(p["mb"] for p in reading["by_name"] if p["name"] == UI_PROCESS)


def _app_mb(reading: dict) -> float | None:
    if "by_name" not in reading:
        return None
    return sum(p["mb"] for p in reading["by_name"] if p["name"] == APP_PROCESS)


def growth_report(readings: list[dict]) -> str:
    """Judge the interface curve: flat while the library grows, flat while idle, and back
    on the baseline after a reload. The app process is listed with its cost per sound.
    The verdict names every reason it failed."""
    baseline = next((r for r in readings if r["sounds"] == 0), None)
    if baseline is None:
        return ("# Wachstumsmessung Oberfläche\n\nKeine Grundlinie (0 Sounds) gemessen, "
                "das Urteil braucht sie.")
    base_ui = _ui_mb(baseline)
    failures: list[str] = []

    for r in readings:
        if r.get("settled") is False:
            failures.append(f"Messung ungültig: {r['phase']} bei {r['sounds']} Sounds wurde "
                            "gemessen, während das Vorladen noch lief")

    loaded = [r for r in readings if r["phase"] == "load" and r["sounds"] > 0]
    for r in loaded:
        allowed = base_ui + GROWTH_LIMIT_MB_PER_1000 * (r["sounds"] / 1000.0)
        if _ui_mb(r) > allowed:
            failures.append(f"Bibliothek (Oberfläche): {r['sounds']} Sounds brauchen "
                            f"{_ui_mb(r):.1f} MB, erlaubt sind {allowed:.1f} MB "
                            f"(Grundlinie {base_ui:.1f} MB)")

    idle = [r for r in readings if r["phase"] == "idle"]
    if idle:
        last = idle[-1]
        if last.get("alive") is False or last["ram_mb"] <= 0:
            failures.append(f"Leerlauf: die App wurde nach {last.get('died_after_s', '?')} s "
                            "beendet, ohne dass die Probe sie gestoppt hat")
        else:
            # A leak anywhere counts here, so the whole tree against the last load reading.
            last_load = max((r["ram_mb"] for r in loaded), default=baseline["ram_mb"])
            if last["ram_mb"] - last_load > IDLE_LIMIT_MB:
                failures.append(f"Leerlauf: +{last['ram_mb'] - last_load:.1f} MB in 30 Minuten, "
                                f"erlaubt sind {IDLE_LIMIT_MB:.0f} MB")

    reloads = [r for r in readings if r["phase"] == "reload"]
    if reloads and _ui_mb(reloads[-1]) - base_ui > RELOAD_TOLERANCE_MB:
        failures.append(f"Neuladen: die Oberfläche landet bei {_ui_mb(reloads[-1]):.1f} MB statt "
                        f"auf der Grundlinie {base_ui:.1f} MB (erlaubt +{RELOAD_TOLERANCE_MB:.0f} MB)")

    lines = ["# Wachstumsmessung Oberfläche", "",
             "| Phase | Sounds | Oberfläche (MiB) | App-Prozess (MiB) | Gesamt (MiB) |",
             "|---|---|---|---|---|"]
    for r in readings:
        app = _app_mb(r)
        app_cell = "–" if app is None else f"{app:.1f}"
        lines.append(f"| {r['phase']} | {r['sounds']} | {_ui_mb(r):.1f} | {app_cell} "
                     f"| {r['ram_mb']:.1f} |")
    lines += ["", f"Grundlinie Oberfläche: {base_ui:.1f} MiB", ""]
    base_app = _app_mb(baseline)
    biggest = max(loaded, key=lambda r: r["sounds"], default=None)
    if base_app is not None and biggest is not None and _app_mb(biggest) is not None:
        per_sound = (_app_mb(biggest) - base_app) / biggest["sounds"]
        lines += [f"App-Prozess (nur berichtet, entpackter Audio-Cache): {per_sound:.2f} MB je "
                  f"Sound bei {biggest['sounds']} Sounds.", ""]
    if failures:
        lines += ["**Nicht bestanden.**"] + [f"- {f}" for f in failures]
    else:
        lines += ["**Bestanden.** Die Kurve flacht ab, das Neuladen kehrt zur Grundlinie zurück."]
    return "\n".join(lines)


# ---- running inside the app ----

def _sound_info() -> dict:
    return json.loads((config.get_app_data_dir() / SOUND_INFO).read_text(encoding="utf-8"))


def _idle_s() -> float:
    try:
        return float(os.environ.get(IDLE_ENV, "35"))
    except ValueError:
        return 35.0


def _hotkey_while_blocked(core, info: dict, block: Callable[[], Any]) -> dict:
    import keyboard

    block_s = block_seconds()
    probe = PlaybackProbe(core, info["sound_id"])
    blocker = threading.Thread(target=block, name="measure-block", daemon=True)
    blocker.start()
    time.sleep(0.5)  # the interface is now inside its block
    if manual_hotkey():
        keys = KeyPressProbe(info["hotkey"])
        seen: list[str] = []

        def on_key(event) -> None:
            seen.append(f"{event.event_type}:{event.name}")
            keys.on_event(event)

        hook = keyboard.hook(on_key)
        try:
            import winsound
            winsound.Beep(880, 250)  # "press now"
        except Exception:  # noqa: BLE001 - no beep, the user still has the whole block
            pass
        at = probe.wait(block_s - 1.0)
        keyboard.unhook(hook)
        pressed = keys.at
        log.info("manual hotkey: %d key events (first %s), %s pressed=%s, playback=%s",
                 len(seen), seen[:6], keys.key, pressed, at)
    else:
        pressed = time.perf_counter()
        keyboard.send(info["hotkey"])
        at = probe.wait(2.0)
    probe.close()
    core.send(StopAll())
    blocker.join(block_s + 3)
    latency = None if at is None or pressed is None else round((at - pressed) * 1000, 1)
    return {"latency_ms": latency, "blocked_s": block_s, "manual": manual_hotkey()}


def _cable(core, info: dict, play: Callable[[], Any], signal: Callable[[], Any]) -> dict:
    before = dropped_blocks(core)
    probe = PlaybackProbe(core, info["sound_id"])
    play()
    played = probe.wait(3.0) is not None
    probe.close()
    time.sleep(float(info.get("seconds", 10)) + 1.0)
    after = dropped_blocks(core)
    done = threading.Event()
    box: dict = {}

    def on_event(event) -> None:
        if isinstance(event, SignalCheckDone):
            box["results"] = list(event.results)
            done.set()

    off = core.subscribe(on_event)
    signal()
    done.wait(20.0)
    off()
    results = box.get("results")
    return {"played": played,
            "signal_ok": None if results is None else bool(results) and all(r.get("ok") for r in results),
            "results": results or [],
            "dropped_delta": {k: after.get(k, 0) - before.get(k, 0) for k in after}}


def attach_tk(app) -> None:
    """main.py: called right after build_app(); no-op outside the measure mode."""
    if not enabled():
        return
    app.after_idle(lambda: mark_ready("tk"))
    threading.Thread(target=_run_tk, args=(app,), name="measure-tk", daemon=True).start()


def _in_tk(app, fn, timeout: float = 10.0):
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn()
        finally:
            done.set()

    app.call_in_ui(run)
    done.wait(timeout)
    return box.get("value")


def _run_tk(app) -> None:
    try:
        time.sleep(_idle_s())
        info, core = _sound_info(), app.core

        def step(w: int, h: int) -> float:
            start = time.perf_counter()
            app.geometry(f"{w}x{h}")
            app.update_idletasks()
            return (time.perf_counter() - start) * 1000

        steps = [_in_tk(app, lambda w=w, h=h: step(w, h)) for w, h in resize_plan()]
        hotkey = _hotkey_while_blocked(core, info, lambda: _in_tk(app, lambda: time.sleep(block_seconds()), block_seconds() + 5))
        cable = _cable(core, info, lambda: core.send(Play(info["sound_id"])), lambda: core.send(RunSignalCheck()))
        write_json("tk.json", {"ui": "tk", "resize": summarize([s for s in steps if s is not None]), "hotkey": hotkey, "cable": cable})
    except Exception:  # noqa: BLE001 - a failed measurement must still close the app
        log.exception("Tk measurement failed")
        write_json("tk.json", {"ui": "tk", "error": "measurement failed, see ruckus.log"})
    finally:
        app.call_in_ui(app.shutdown_and_close)


def attach_web(window, bridge, core) -> None:
    """webmain.run(on_window=...): starts the measurement once the page reports ready."""
    if not enabled():
        return

    def on_ready(_client_id: str, _info: dict) -> None:
        mark_ready("web")
        threading.Thread(target=_run_web, args=(window, core), name="measure-web", daemon=True).start()

    bridge.on_ready = on_ready


def _run_web(window, core) -> None:
    try:
        time.sleep(_idle_s())
        info = _sound_info()
        window.evaluate_js("window.__ruckusMeasure.reset()")
        base = (window.width, window.height)
        for w, h in resize_plan():
            window.resize(w, h)
            time.sleep(0.4)
        window.resize(*base)
        time.sleep(0.5)
        frames = window.evaluate_js("window.__ruckusMeasure.collect()") or {}
        resize = summarize(frames.get("resize_ms") or [])
        resize["long_tasks_ms"] = [round(t.get("duration", 0), 1) for t in frames.get("long_tasks") or []]
        # spec §6 security, checked in the real window: only three JS-API methods, no CSP violations
        api_keys = sorted(window.evaluate_js("Object.keys(window.pywebview.api)") or [])
        security = {"api_keys": api_keys, "api_ok": api_keys == ["get_state", "ready", "send"],
                    "csp_violations": frames.get("csp_violations") or []}
        block_ms = int(block_seconds() * 1000)
        hotkey = _hotkey_while_blocked(core, info, lambda: window.evaluate_js(f"window.__ruckusMeasure.block({block_ms})"))
        sound = json.dumps(info["sound_id"])
        cable = _cable(core, info, lambda: window.evaluate_js(f"window.__ruckusMeasure.play({sound})"),
                       lambda: window.evaluate_js("window.__ruckusMeasure.signal()"))
        write_json("web.json", {"ui": "web", "resize": resize, "hotkey": hotkey, "cable": cable, "security": security})
    except Exception:  # noqa: BLE001
        log.exception("web measurement failed")
        write_json("web.json", {"ui": "web", "error": "measurement failed, see ruckus.log"})
    finally:
        window.destroy()
