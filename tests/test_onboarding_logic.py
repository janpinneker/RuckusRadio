"""Einrichtungs-Assistent am Attrappen-Kern: Schritt-Logik, Mikrofone, Kabelprüfung,
Autostart, Abschluss - der Assistent spricht nur über core.send mit dem Kern."""

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-onb-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tk_quiet  # noqa: E402

tk_quiet.install()  # no test window may show up or take focus

from soundboard import config, devices, gui, onboarding, protocol as p  # noqa: E402
from soundboard.onboarding import (  # noqa: E402
    STEP_COUNT,
    can_advance,
    mic_choices,
    next_action,
    step_needle,
)
from soundboard.tuner import needle_x, render_tuner  # noqa: E402
import core_fakes  # noqa: E402

MESSAGES: list[tuple[str, str]] = []
gui.messagebox.showinfo = lambda title, text, **kw: MESSAGES.append(("info", text))
gui.messagebox.showerror = lambda title, text, **kw: MESSAGES.append(("error", text))

# The core swallows a failing subscriber (and _pump_ui a failing callback) and only logs
# it: collect those so a broken listener cannot hide behind a green run.
SWALLOWED: list[logging.LogRecord] = []
RECORDS: list[logging.LogRecord] = []


class _Swallowed(logging.Handler):
    def emit(self, record):
        RECORDS.append(record)
        if record.getMessage() in ("event subscriber failed", "ui callback failed"):
            SWALLOWED.append(record)


logging.getLogger("soundboard").addHandler(_Swallowed())


def make_app(**overrides):
    c, events = core_fakes.make_core(**overrides)
    app = gui.RuckusRadioApp(c)
    app.withdraw()
    c.start()
    app.update_idletasks()
    return app, c, events


def open_wizard(app, **kw):
    done: list[bool] = []
    wizard = onboarding.OnboardingWindow(app, on_done=done.append, **kw)
    wizard.withdraw()
    return wizard, done


# ---- pure functions (unchanged) ----

def state(**kw):
    base = {"mic_choices": ["Mikrofon (eMeet Nova)"], "microphone": None,
            "vm_checked": False, "vm_found": False, "sound_count": 0}
    base.update(kw)
    return base


def test_step_needle():
    assert STEP_COUNT == 5
    assert [step_needle(k) for k in range(1, 6)] == [0.1, 0.3, 0.5, 0.7, 0.9]
    assert step_needle(0) == 0.1 and step_needle(9) == 0.9  # clamped
    print("step_needle: OK")


def test_next_action_welcome():
    assert next_action(1, state()) == ("Los geht's", True)
    print("step 1 gating: OK")


def test_next_action_mic():
    assert next_action(2, state()) == ("Weiter", False)
    assert next_action(2, state(microphone="")) == ("Weiter", False)
    assert next_action(2, state(microphone="Mikrofon (eMeet Nova)")) == ("Weiter", True)
    assert next_action(2, state(mic_choices=[])) == ("Überspringen", True)
    print("step 2 gating: OK")


def test_next_action_voicemeeter():
    assert next_action(3, state()) == ("Später einrichten", True)
    assert next_action(3, state(vm_checked=True, vm_found=False)) == ("Später einrichten", True)
    assert next_action(3, state(vm_checked=True, vm_found=True)) == ("Weiter", True)
    print("step 3 gating: OK")


def test_next_action_sound():
    assert next_action(4, state()) == ("Überspringen", True)
    assert next_action(4, state(sound_count=1)) == ("Weiter", True)
    assert next_action(4, state(sound_count=7)) == ("Weiter", True)
    print("step 4 gating: OK")


def test_next_action_finish():
    assert next_action(5, state()) == ("Ruckus Radio öffnen", True)
    assert can_advance(5, state()) is True
    assert can_advance(2, state()) is False
    print("step 5 gating: OK")


def test_mic_choices():
    raw = [
        {"index": 0, "name": "Microsoft Soundmapper - Input", "channels": 2},
        {"index": 1, "name": "Mikrofon (Endorfy Solum Voice S", "channels": 1},
        {"index": 2, "name": "Mikrofon (eMeet Nova)", "channels": 1},
        {"index": 3, "name": "VoiceMeeter Output (VB-Audio Vo", "channels": 2},
        {"index": 9, "name": "Primärer Soundaufnahmetreiber", "channels": 2},
        {"index": 10, "name": "Mikrofon (Endorfy Solum Voice S Mic)", "channels": 1},
        {"index": 11, "name": "Mikrofon (eMeet Nova)", "channels": 1},
        {"index": 12, "name": "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", "channels": 2},
    ]
    assert mic_choices(raw) == ["Mikrofon (Endorfy Solum Voice S Mic)", "Mikrofon (eMeet Nova)"]
    assert mic_choices([]) == []
    print("mic_choices: OK")


def test_compact_tuner():
    img = render_tuner(700, 56, 0.3, wordmark=False)
    assert img.size == (700, 56)
    x = needle_x(700, 0.3)
    y = 10
    px, left = img.getpixel((x, y)), img.getpixel((x - 14, y))
    assert sum(px) > sum(left), (px, left)
    # scale ticks are drawn in the compact strip (dark ink below the needle area)
    plain = render_tuner(700, 56, 0.3, wordmark=False, scale=False)
    assert plain.tobytes() != img.tobytes()
    print("compact tuner strip: OK")


def test_real_rescan_shape():
    """Whether a virtual mic exists depends on the machine, so only the shape is asserted:
    every key the core's device summary reads must be there, and `found` must agree with
    the resolved virtual mic."""
    result = devices.rescan_system_devices(config._default_config(), reinit=False)
    assert set(result) >= {"found", "name", "voicemeeter", "monitor", "connected",
                           "virtual_mic", "mic"}, result
    virtual = result["virtual_mic"]
    assert set(virtual) >= {"mode", "out_index", "in_index", "discord_device_name", "connected"}
    assert result["found"] == virtual["connected"]
    if virtual["connected"]:
        assert virtual["discord_device_name"]
    print("real rescan shape:", virtual["mode"], "->", virtual["discord_device_name"])


# ---- mandatory: the wizard talks to the core ----

def test_wizard_holds_the_devices_and_rescans_on_close():
    app, c, events = make_app()
    wizard, done = open_wizard(app)
    assert app.snapshot["devices"]["busy"] is True
    rescans = len(c.routing._backend.rescans)
    wizard.close_early()
    assert done == [False]
    assert app.snapshot["devices"]["busy"] is False
    assert len(c.routing._backend.rescans) == rescans + 1, "one rescan after closing"
    assert app.state_listeners == [] and app.device_listeners == [] and app.signal_listeners == []
    app.destroy()
    print("the wizard holds the devices and rescans once when it closes: OK")


def test_microphones_come_from_the_core_and_are_only_stored():
    app, c, events = make_app()
    wizard, done = open_wizard(app)
    assert wizard.flow["mic_choices"] == c.routing._backend.mic_choice_list
    assert wizard.flow["microphone"] == c.routing._backend.default_mic, "default preselected"
    rescans = len(c.routing._backend.rescans)
    wizard.set_microphone("Mikrofon (eMeet Nova)")
    assert c.store.data["microphone_name"] == "Mikrofon (eMeet Nova)"
    assert len(c.routing._backend.rescans) == rescans, "apply=False: no rescan"
    wizard.close_early()
    app.destroy()
    print("the wizard's microphones come from the core and are only stored: OK")


def test_late_microphones_reach_the_open_wizard():
    """A real core resolves the devices after start(): the wizard may open on an empty
    list and has to pick the microphones up from the next state."""
    backend = core_fakes.FakeBackend()
    backend.mic_choice_list = []
    backend.default_mic = None
    app, c, events = make_app(backend=backend)
    wizard, done = open_wizard(app)
    assert wizard.flow["mic_choices"] == []
    assert wizard.flow["microphone"] is None
    wizard.show_step(2)
    assert wizard.next_button.cget("text") == "Überspringen"
    backend.mic_choice_list = ["Mikro A", "Mikro B"]
    backend.default_mic = "Mikro B"
    c.send(p.Rescan())  # not gated by the busy flag: the list travels with the result
    assert wizard.flow["mic_choices"] == ["Mikro A", "Mikro B"], wizard.flow
    assert wizard.flow["microphone"] == "Mikro B", "default preselected"
    assert c.store.data["microphone_name"] == "Mikro B"
    assert wizard.step == 2 and wizard.mic_menu.get() == "Mikro B", "step 2 redrawn"
    assert wizard.next_button.cget("text") == "Weiter"
    wizard.close_early()
    app.destroy()
    print("microphones found after the wizard opened reach step 2: OK")


def test_cable_check_waits_for_devices_then_the_signal():
    app, c, events = make_app()
    wizard, done = open_wizard(app)
    wizard.show_step(3)
    wizard.check_voicemeeter()
    assert wizard.flow["vm_found"] is True
    assert wizard.flow["vm_discord"] == core_fakes.CABLE_KEY
    assert wizard.flow["vm_signal"] is True, "signal check ran after DevicesChanged"
    assert wizard.flow["awaiting"] is None
    c.emit(p.DevicesChanged({"found": False}))
    assert wizard.flow["vm_found"] is True, "a foreign rescan changes nothing"
    wizard.close_early()
    app.destroy()
    print("the cable check waits for DevicesChanged, then the signal: OK")


def test_autostart_failure_puts_the_box_back():
    app, c, events = make_app(autostart=core_fakes.FakeAutostart(ok=False))
    wizard, done = open_wizard(app)
    wizard.show_step(3)
    wizard.autostart_box.select()
    wizard.toggle_autostart()
    assert wizard.autostart_box.get() == 0
    wizard.close_early()
    app.destroy()
    print("a failed autostart puts the checkbox back: OK")


def test_added_sound_and_finish():
    app, c, events = make_app()
    wizard, done = open_wizard(app)
    wizard.show_step(4)
    app.add_sound("Tröte", core_fakes.FIXTURES / "test_tone.mp3", None)
    assert wizard.flow["sound_count"] == 1
    wizard.show_step(onboarding.STEP_COUNT)
    wizard.finish()
    assert done == [True]
    assert app.snapshot["settings"]["onboarding_completed"] is True
    app.destroy()
    print("an added sound updates the wizard, finish completes onboarding: OK")


# ---- display, kept from the old suite ----

def test_rail_has_setup_action():
    from soundboard.gui import NAV_ITEMS
    keys = [item[0] for item in NAV_ITEMS]
    assert keys.index("setup") == keys.index("settings") - 1, keys
    setup = next(i for i in NAV_ITEMS if i[0] == "setup")
    settings = next(i for i in NAV_ITEMS if i[0] == "settings")
    assert setup[1] == "Assistent" and setup[3] is True and setup[4] == "action", setup
    assert settings[3] is True, settings
    assert "setup" in gui.RuckusRadioApp.ACTIONS
    print("rail setup action item: OK")


def test_window_navigation_and_gating():
    app, c, events = make_app()
    w, _ = open_wizard(app, mics=["Mikro A", "Mikro B"], preselect=False)
    assert w.step == 1 and w.back_button.cget("state") == "disabled"
    w.go_next()
    assert w.step == 2
    assert w.next_button.cget("state") == "disabled"
    w.go_next()
    assert w.step == 2, "must not advance without a microphone"
    w.set_microphone("Mikro B")
    assert w.next_button.cget("state") == "normal"
    assert c.store.data["microphone_name"] == "Mikro B"
    w.go_next()
    assert w.step == 3 and w.next_button.cget("text") == "Später einrichten"
    w.go_back()
    assert w.step == 2
    w.close_early()
    app.destroy()
    print("window navigation + gating: OK")


def test_window_cable_found_and_not_found_texts():
    app, c, events = make_app()
    w, _ = open_wizard(app)
    w.show_step(3)
    w.check_voicemeeter()
    assert w.flow["vm_found"] and w.next_button.cget("text") == "Weiter"
    name = core_fakes.RESOLVED["virtual_mic"]["out_name"]
    assert name in w.vm_result.cget("text")
    assert "Signal kommt an" in w.vm_steps.cget("text")
    w.close_early()
    app.destroy()

    backend = core_fakes.FakeBackend()
    backend.resolved["virtual_mic"]["connected"] = False
    backend.resolved["virtual_mics"] = []
    app, c, events = make_app(backend=backend)
    w, _ = open_wizard(app)
    w.show_step(3)
    checks = len(backend.checks)
    w.check_voicemeeter()
    assert w.flow["vm_checked"] and not w.flow["vm_found"]
    assert len(backend.checks) == checks, "no signal check without a cable"
    assert w.flow["awaiting"] is None
    assert w.next_button.cget("text") == "Später einrichten"
    assert w.check_button.cget("text") == "Erneut prüfen"
    assert "Kein virtuelles Mikrofon gefunden" in w.vm_result.cget("text")
    w.close_early()
    app.destroy()
    print("window cable found / not found: OK")


def test_failed_signal_check_is_reported():
    backend = core_fakes.FakeBackend()
    app, c, events = make_app(backend=backend)
    w, _ = open_wizard(app)
    w.show_step(3)

    def broken(out_index, in_index):
        raise RuntimeError("measurement failed")

    backend.verify_path = broken  # the core's CHECK_FAILED path: empty results
    w.check_voicemeeter()
    assert w.flow["vm_found"] is True and w.flow["awaiting"] is None
    assert w.flow["vm_signal"] is False
    assert "Prüfung fehlgeschlagen." in w.vm_steps.cget("text")
    w.close_early()
    app.destroy()
    print("a failed signal check shows up in step 3: OK")


def test_sound_step_follows_board():
    app, c, events = make_app()
    w, _ = open_wizard(app, mics=["Mikro A"])
    w.show_step(4)
    assert w.next_button.cget("text") == "Überspringen"
    app.add_sound("Airhorn", core_fakes.FIXTURES / "test_tone.mp3", None)
    assert w.flow["sound_count"] == 1, w.flow
    assert w.next_button.cget("text") == "Weiter"
    w.show_step(5)
    assert "Airhorn" in w.hotkey_title.cget("text")
    w.close_early()
    app.destroy()
    print("step 4 follows board / step 5 names first sound: OK")


def test_close_early_does_not_complete():
    app, c, events = make_app()
    w, results = open_wizard(app, mics=["Mikro A"])
    w.close_early()
    assert results == [False], results
    assert c.store.data["onboarding_completed"] is False
    assert app.snapshot["settings"]["onboarding_completed"] is False
    app.destroy()
    print("close early keeps onboarding_completed False: OK")


def test_settings_page_is_fresh_after_the_wizard():
    app, c, events = make_app()
    app.show_view("settings")
    view = app.views["settings"]
    mixer = view.mixer
    app.open_onboarding()
    assert app.onboarding is not None and app.state() == "withdrawn"
    app.onboarding.withdraw()
    app.onboarding.close_early()
    assert app.onboarding is None and app.state() != "withdrawn"
    assert view.mixer is not mixer, "settings page rebuilt after the wizard closed"
    app.destroy()
    print("the settings page is rebuilt after the wizard closes: OK")


def test_open_onboarding_restores_main_on_failure():
    """If OnboardingWindow raises during construction, the main window must come back
    from withdrawn so the app stays usable and its hotkeys reachable."""
    app, c, events = make_app()
    app.deiconify()
    original_init = onboarding.OnboardingWindow.__init__

    def failing_init(self, app, *args, **kwargs):
        app.core.send(p.SetOnboardingActive(True))  # like the real window, first thing
        raise RuntimeError("Simulated OnboardingWindow construction failure")

    RECORDS.clear()
    onboarding.OnboardingWindow.__init__ = failing_init
    try:
        app.open_onboarding()
    finally:
        onboarding.OnboardingWindow.__init__ = original_init
    assert app.state() != "withdrawn", "main window restored after the failure"
    assert app.onboarding is None
    assert app.snapshot["devices"]["busy"] is False, "devices released after the failure"
    assert any(r.getMessage() == "onboarding window failed to open" and r.exc_info
               for r in RECORDS), "the exception is logged"
    assert "Der Setup-Assistent konnte nicht geöffnet werden" in app.dock.hint_label.cget("text")
    app.destroy()
    print("open_onboarding recovery on failure: OK")


def main():
    tests = [
        test_step_needle,
        test_next_action_welcome,
        test_next_action_mic,
        test_next_action_voicemeeter,
        test_next_action_sound,
        test_next_action_finish,
        test_mic_choices,
        test_compact_tuner,
        test_real_rescan_shape,
        test_wizard_holds_the_devices_and_rescans_on_close,
        test_microphones_come_from_the_core_and_are_only_stored,
        test_late_microphones_reach_the_open_wizard,
        test_cable_check_waits_for_devices_then_the_signal,
        test_autostart_failure_puts_the_box_back,
        test_added_sound_and_finish,
        test_rail_has_setup_action,
        test_window_navigation_and_gating,
        test_window_cable_found_and_not_found_texts,
        test_failed_signal_check_is_reported,
        test_sound_step_follows_board,
        test_close_early_does_not_complete,
        test_settings_page_is_fresh_after_the_wizard,
        test_open_onboarding_restores_main_on_failure,
    ]
    try:
        for test in tests:
            test()
            assert not SWALLOWED, f"{test.__name__}: {SWALLOWED[0].exc_info}"
        print("\nALL ONBOARDING LOGIC CHECKS PASSED")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
