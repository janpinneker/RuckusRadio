"""Hauptfenster am Attrappen-Kern: Ereignisse -> Anzeige, Klicks -> Befehle."""

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-gui-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tk_quiet  # noqa: E402

tk_quiet.install()  # no test window may show up or take focus

from soundboard import config, gui, layout, levels, protocol as p, routing  # noqa: E402
import core_fakes  # noqa: E402

MESSAGES: list[tuple[str, str]] = []
gui.messagebox.showinfo = lambda title, text, **kw: MESSAGES.append(("info", text))
gui.messagebox.showerror = lambda title, text, **kw: MESSAGES.append(("error", text))

# The core swallows a failing subscriber (and _pump_ui a failing callback) and only logs
# it: collect those so a broken _apply_state cannot hide behind a green run.
SWALLOWED: list[logging.LogRecord] = []


class _Swallowed(logging.Handler):
    def emit(self, record):
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


def add_fixture_sound(app, name="Tröte"):
    app.add_sound(name, core_fakes.FIXTURES / "test_tone.mp3", None)
    return next(s for s in app.sounds() if s["name"] == name)


# ---- mandatory: events -> display, clicks -> commands ----

def test_board_follows_the_core_state():
    app, c, events = make_app()
    assert app.board.tiles == {}
    sound = add_fixture_sound(app)
    assert sound["id"] in app.board.tiles, "AddSound -> StateChanged -> new tile"
    app.rename_sound(sound, "  Hupe ")
    assert app.board.tiles[sound["id"]].sound["name"] == "Hupe"
    app.delete_sound(sound["id"])
    assert app.board.tiles == {}
    app.destroy()
    print("the board follows the core state (add, rename, delete): OK")


def test_board_only_touches_the_changed_tile():
    app, c, events = make_app()
    first = add_fixture_sound(app, "Eins")
    kept = app.board.tiles[first["id"]]
    slots = list(app.board.slots)
    second = add_fixture_sound(app, "Zwei")
    assert app.board.tiles[first["id"]] is kept, "adding a sound must not rebuild the others"
    assert app.board.slots == slots[:len(app.board.slots)], "slots are only trimmed, not rebuilt"
    renamed = app.board.tiles[second["id"]]
    app.rename_sound(second, "Drei")
    assert app.board.tiles[first["id"]] is kept, "renaming one sound keeps the other tile"
    assert app.board.tiles[second["id"]] is not renamed, "the renamed sound gets a fresh tile"
    app.delete_sound(second["id"])
    assert app.board.tiles[first["id"]] is kept, "deleting one sound keeps the other tile"
    assert len(app.board.tiles) + len(app.board.slots) == len(app.board._placed)
    redraws: list[str] = []
    kept.name_label.configure = lambda **kw: redraws.append("name")
    app._apply_state(dict(app.snapshot, sounds=list(app.snapshot["sounds"])))
    assert redraws == [], "an unchanged state update must not redraw the tile"
    app.destroy()
    print("the board touches only the changed tile (add, rename, delete, state tick): OK")


def test_click_plays_and_rings_until_the_end():
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    app.board.tiles[sound["id"]]._on_click()
    assert c.engine.plays and c.engine.plays[-1][0] == sound["id"]
    assert sound["id"] in app.playing_ids
    c.engine.playing.clear()
    c.executor.advance(1.0)  # playback poll notices the end
    assert sound["id"] not in app.playing_ids
    app.destroy()
    print("a click plays through the core, the ring follows start and end: OK")


def test_notices_reach_dock_and_dialogs():
    app, c, events = make_app()
    MESSAGES.clear()
    c.notice("nur ein Hinweis")
    assert "nur ein Hinweis" in app.dock.hint_label.cget("text")
    c.notice("kaputt", "error")
    assert MESSAGES[-1] == ("error", "kaputt")
    assert app.dock.hint_label.cget("text") == "", "progress hint cleared before a dialog"
    c.notice("fertig", "info")
    assert MESSAGES[-1] == ("info", "fertig")
    app.destroy()
    print("hint notices go to the dock, info/error to a dialog: OK")


def test_icon_change_rebuilds_the_tile():
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    before = app.board.tiles[sound["id"]]
    app.change_icon(sound, str(core_fakes.FIXTURES / "test_image.png"))
    after = app.board.tiles[sound["id"]]
    assert after is not before, "icon_rev changed -> tile rebuilt, image reloaded"
    MESSAGES.clear()
    app.change_icon(sound, str(core_fakes.FIXTURES / "test_tone.mp3"))
    assert MESSAGES and MESSAGES[-1][0] == "error"
    app.destroy()
    print("a new icon rebuilds the tile, a bad one shows the core's error: OK")


def test_hotkey_dialog_suspends_sets_and_resumes():
    hotkeys = core_fakes.FakeHotkeys()
    app, c, events = make_app(hotkeys=hotkeys)
    sound = add_fixture_sound(app)
    registered_before = dict(hotkeys.registered)
    dialog = gui.HotkeyCaptureDialog(app, app, sound)
    assert hotkeys.registered == {}, "suspended while recording"
    # AltGr+1 types "¹" too: allowed, the dialog only warns
    dialog._handle_captured(dialog._capture_generation, "alt gr+1")
    assert dialog.apply_button.cget("state") == "normal"
    assert dialog.error_label.cget("text").startswith("Achtung: AltGr")
    dialog._apply()
    assert "alt gr+1" in hotkeys.registered
    assert all(k in hotkeys.registered for k in registered_before)
    assert next(s for s in app.sounds() if s["id"] == sound["id"])["hotkey"] == "alt gr+1"
    app.destroy()
    print("hotkey capture suspends, sets and resumes through the core: OK")


def test_hotkey_dialog_destroyed_from_outside_resumes():
    hotkeys = core_fakes.FakeHotkeys()
    app, c, events = make_app(hotkeys=hotkeys)
    sound = add_fixture_sound(app)
    registered_before = dict(hotkeys.registered)
    assert registered_before, "stop-all at least"
    dialog = gui.HotkeyCaptureDialog(app, app, sound)
    assert hotkeys.registered == {}, "suspended while recording"
    dialog.destroy()  # e.g. its parent (the assistant) was closed
    assert hotkeys.registered.keys() == registered_before.keys(), "hotkeys back after an outside close"
    app.destroy()
    print("a hotkey dialog destroyed from outside resumes the hotkeys: OK")


def test_hotkey_conflict_is_checked_against_the_state():
    app, c, events = make_app()
    stop_key = app.snapshot["settings"]["stop_all_hotkey"]
    sound = add_fixture_sound(app)
    dialog = gui.HotkeyCaptureDialog(app, app, sound)
    dialog._handle_captured(dialog._capture_generation, stop_key)
    assert dialog._captured is None and dialog.apply_button.cget("state") == "disabled"
    dialog._cancel()
    app.destroy()
    print("a hotkey clashing with stop-all is refused from the snapshot: OK")


def test_settings_rebuild_only_on_structure():
    app, c, events = make_app()
    app.show_view("settings")
    view = app.views["settings"]
    mixer_before = view.mixer
    app.set_output(core_fakes.CABLE_KEY, sounds_gain=0.3)
    assert view.mixer is mixer_before, "a slider move must not rebuild the page"
    assert c.store.data["outputs"][core_fakes.CABLE_KEY]["sounds_gain"] == 0.3
    c.routing._backend.resolved["microphones"].append("Mikrofon (Neu)")
    c.send(p.Rescan())
    assert view.mixer is not mixer_before, "new devices rebuild the page"
    app.destroy()
    print("settings rebuild on new devices, not on slider moves: OK")


def test_mic_busy_puts_the_dropdown_back():
    app, c, events = make_app()
    app.show_view("settings")
    view = app.views["settings"]
    real = app.snapshot["devices"]["mic_name"]
    c.send(p.SetOnboardingActive(True))
    view.mixer.menu.set("Mikrofon (NVIDIA Broadcast)")
    app.set_microphone("Mikrofon (NVIDIA Broadcast)")
    assert routing.MIC_BUSY in app.dock.hint_label.cget("text")
    assert view.mixer.menu.get() == real
    app.destroy()
    print("MIC_BUSY puts the microphone dropdown back: OK")


def test_signal_check_button_and_summary():
    app, c, events = make_app()
    app.check_signal()
    assert app.dock.check_button.cget("state") == "normal"
    assert app.dock.check_button.cget("text") == "Prüfen"
    ok_text = layout.signal_check_summary(
        [{"key": core_fakes.CABLE_KEY, "label": "CABLE", "ok": True, "reason": "Signal kommt an."}])[0]
    assert ok_text in app.dock.hint_label.cget("text")
    app.destroy()  # one Tk root at a time: CTkImage draws into the default root
    backend = core_fakes.FakeBackend()
    backend.resolved["virtual_mics"] = []
    app2, c2, _ = make_app(backend=backend)
    app2.check_signal()
    assert app2.dock.check_button.cget("state") == "normal"
    assert routing.NO_VIRTUAL_MIC in app2.dock.hint_label.cget("text")
    app2.destroy()
    print("the check button frees itself, with a summary or the core's hint: OK")


def test_discord_button_mutes_the_sounds_in_discord_only():
    app, c, events = make_app()
    key = core_fakes.CABLE_KEY
    app.show_view("settings")
    assert app.dock.discord_button.cget("text") == "Discord: Sounds an"
    app.toggle_discord_sounds()
    assert c.store.data["outputs"][key]["sounds"] is False
    assert c.store.data["outputs"][key]["mic"] is True, "the voice stays"
    assert app.dock.discord_button.cget("text") == "Discord: Sounds aus"
    assert gui.DISCORD_SOUNDS_OFF in app.dock.hint_label.cget("text")
    row = next(r for r in app.snapshot["devices"]["output_rows"] if r["key"] == key)
    assert row["settings"]["sounds"] is False
    view = app.views["settings"]
    switch_rebuilt = view.mixer  # the page follows the switch
    app.toggle_discord_sounds()
    assert c.store.data["outputs"][key]["sounds"] is True
    assert gui.DISCORD_SOUNDS_ON in app.dock.hint_label.cget("text")
    assert view.mixer is not switch_rebuilt, "settings page redrawn after a dock toggle"
    app.destroy()
    print("the Discord button mutes only the sounds in the Discord cable: OK")


def test_headphone_hint():
    app, c, events = make_app()
    c.emit(p.HeadphonesSwitched("Kopfhörer (Neu)"))
    assert "Kopfhörer gewechselt: Kopfhörer (Neu)" in app.dock.hint_label.cget("text")
    app.destroy()
    print("headphone switches are hinted: OK")


def test_toggle_mic_shows_the_mute_button_state():
    app, c, events = make_app()
    assert app.dock._mic_shown
    app.toggle_mic()
    assert app.dock.mic_button.cget("text") == "Mikro aus"
    assert routing.MIC_MUTED in app.dock.hint_label.cget("text")
    app.destroy()
    print("the dock's mic button mirrors the core's mute: OK")


def test_config_reset_hint_once():
    app, c, events = make_app()
    c.store.was_reset = True
    c.state_changed()
    assert "zurückgesetzt" in app.dock.hint_label.cget("text")
    app.show_hint("")
    c.state_changed()
    assert app.dock.hint_label.cget("text") == ""
    app.destroy()
    print("a config reset is hinted once: OK")


class ThreadedStub:
    """Just enough core for marshalling and the watchdog: not inline, never alive."""
    inline = False
    alive = True

    def __init__(self, store):
        self.store = store
        self.callback = None

    def subscribe(self, callback):
        self.callback = callback
        return lambda: None

    def send(self, command):
        pass

    def get_state(self):
        return {}


def test_threaded_events_go_through_the_ui_queue_and_the_watchdog():
    c, _ = core_fakes.make_core()
    stub = ThreadedStub(c.store)
    app = gui.RuckusRadioApp(stub)
    app.withdraw()
    stub.callback(p.Notice("aus dem Kern-Thread"))
    assert app.dock.hint_label.cget("text") == "", "not handled on the calling thread"
    app._pump_ui()
    assert "aus dem Kern-Thread" in app.dock.hint_label.cget("text")
    MESSAGES.clear()
    stub.alive = False
    app._watch_core()
    app._watch_core()
    assert MESSAGES == [("error", gui.CORE_DEAD)], "reported exactly once"
    app.destroy()
    print("threaded events are marshalled, a dead core is reported once: OK")


def test_export_default_name():
    sounds = [{"id": "a", "name": "Air/Horn?"}, {"id": "b", "name": "B"}]
    assert gui.export_default_name(sounds, ["a"]).endswith(".ruckuspack")
    assert "/" not in gui.export_default_name(sounds, ["a"])
    assert gui.export_default_name(sounds, ["a", "b"]) == "Ruckus-Board.ruckuspack"
    assert gui.export_default_name(sounds, ["x"]) == "Sound.ruckuspack"
    print("export file name suggestion: OK")


# ---- kept from the old file: view building blocks and pure functions ----

def test_navigation():
    keys = [item[0] for item in gui.NAV_ITEMS]
    assert set(gui.RuckusRadioApp.VIEWS) <= set(keys)
    assert set(gui.RuckusRadioApp.ACTIONS) <= set(keys)
    app, c, events = make_app()
    assert isinstance(app.snapshot, dict) and "sounds" in app.snapshot
    assert app.state() == "withdrawn", "Tk's own state() stays untouched"
    app.snapshot = {"sounds": []}
    assert app.state() == "withdrawn", "assigning the snapshot leaves Tk alone"
    app.refresh_state()
    assert app.current_view == "board"
    app.on_nav("settings")
    assert app.current_view == "settings" and app.views["settings"].winfo_manager() == "pack"
    assert not app.board.winfo_manager(), "the board is hidden behind the settings"
    opened = []
    app.open_onboarding = lambda: opened.append(1)
    app.on_nav("setup")
    assert opened == [1] and app.current_view == "settings", "an action opens, no view swap"
    app._focus_search()
    assert app.current_view == "board"
    app.destroy()
    print("navigation: views swap, actions open: OK")


def test_grid_slots_search_and_tuner():
    app, c, events = make_app()
    assert len(app.board.tiles) == 0 and len(app.board.slots) == layout.slots_total(0) == 18
    first = add_fixture_sound(app, "Airhorn")
    second = add_fixture_sound(app, "Bruh (2)")
    assert len(app.board.tiles) == 2 and len(app.board.slots) == 16
    tile = app.tile_for(second["id"])
    tile.set_playing(True)
    assert tile.disc.state_name == "playing"
    tile.set_playing(False)

    app.board.set_query("(2)")
    app.update_idletasks()
    assert not any(s.grid_info() for s in app.board.slots), "searching hides the empty slots"
    assert app.board.tiles[second["id"]].grid_info() and not app.board.tiles[first["id"]].grid_info()
    app.board.set_query("zzz")
    assert app.board.empty_label.grid_info()
    assert "zzz" in app.board.empty_label.cget("text")
    app.board.clear_search()
    assert all(s.grid_info() for s in app.board.slots)

    app.set_tuner_needle(0.25)
    assert app.banner.needle == 0.25
    app.set_tuner_needle(7)
    assert app.banner.needle == 1.0, "needle is clamped"
    app.destroy()
    print("grid, placeholder slots, search and tuner needle: OK")


def test_playing_ring_survives_a_rebuild():
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    app.play_sound(sound["id"])
    assert app.tile_for(sound["id"]).disc.state_name == "playing"
    add_fixture_sound(app, "Zweiter")  # new sound -> board rebuilt
    assert app.tile_for(sound["id"]).disc.state_name == "playing", "ring must survive a rebuild"
    app.stop_all()
    assert app.tile_for(sound["id"]).disc.state_name != "playing"
    app.destroy()
    print("the playing ring survives a rebuild, stop-all clears it: OK")


def test_dock_status_and_mic_button_from_the_snapshot():
    app, c, events = make_app()
    status = app.snapshot["devices"]["status"]
    assert app.dock.status_label.cget("text") == status["text"] != ""
    app.show_hint("Hallo")
    assert app.dock.hint_label.cget("text") == "·  Hallo"
    app.show_hint("")
    assert app.dock.hint_label.cget("text") == ""
    app.destroy()  # one Tk root at a time: CTkImage draws into the default root
    backend = core_fakes.FakeBackend()
    backend.resolved["virtual_mics"] = []
    backend.resolved["virtual_mic"] = {"connected": False, "label": None}
    backend.build_sink = lambda cfg, resolved: None
    app2, c2, _ = make_app(backend=backend)
    assert "Kein virtuelles Mikrofon" in app2.dock.status_label.cget("text")
    assert not app2.dock._mic_shown, "no mixer -> no mute button"
    app2.toggle_discord_sounds()
    assert routing.NO_VIRTUAL_MIC in app2.dock.hint_label.cget("text")
    app2.destroy()
    print("dock status, hint and mic button follow the snapshot: OK")


def test_missing_tile_blocks_hotkey_capture():
    from soundboard import theme

    app, c, events = make_app()
    sound = add_fixture_sound(app)
    c.emit(p.SoundMissing(sound["id"]))
    tile = app.tile_for(sound["id"])
    assert tile.name_label.cget("text_color") == theme.MUTED
    assert tile.hotkey_label.cget("text") == "Datei fehlt"
    opened = []
    app.open_hotkey_capture = lambda snd: opened.append(snd["id"])
    tile._on_hotkey_label_click(None)
    assert opened == [], "a missing-file tile's label click must not open hotkey capture"
    tile.set_missing(False)
    tile._on_hotkey_label_click(None)
    assert opened == [sound["id"]], "a normal tile's label click still opens hotkey capture"
    tile.set_missing(True)
    app.refresh_grid()
    assert app.tile_for(sound["id"]).hotkey_label.cget("text") == "Datei fehlt", "mark survives rebuild"
    app.destroy()
    print("missing-file tile: marked, blocks hotkey capture, survives rebuild: OK")


def test_tile_icon_missing_or_unreadable_falls_back():
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    icon = app.data_dir / sound["icon"]
    icon.unlink()
    app.refresh_grid()
    icon.write_bytes(b"not a png")
    app.refresh_grid()
    assert app.tile_for(sound["id"]) is not None
    app.destroy()
    print("tile icon missing/unreadable -> placeholder: OK")


def test_volume_dialog_texts_preview_and_apply():
    from soundboard import dynamics

    app, c, events = make_app()
    sound = add_fixture_sound(app)
    dlg = gui.VolumeDialog(app, app, sound)
    assert dlg.readout_label.cget("text") == "0 dB", dlg.readout_label.cget("text")
    top_db = dynamics.gain_to_db(1.5)
    dlg.slider.set(top_db)
    dlg._on_slide(top_db)
    assert dlg.readout_label.cget("text") == "+3,5 dB", dlg.readout_label.cget("text")
    assert "lauter" in dlg.describe_label.cget("text")
    dlg._preview()
    assert c.engine.stop_calls[-1] == sound["id"], "preview restarts (stops first), not stacks"
    assert c.engine.plays[-1][0] == sound["id"]
    assert c.engine.monitor_only == [sound["id"]], "a preview stays in the headphones"
    assert next(s for s in app.sounds() if s["id"] == sound["id"])["volume"] == 1.0, "preview must not save"
    dlg._apply()
    assert next(s for s in app.sounds() if s["id"] == sound["id"])["volume"] == 1.5
    app.destroy()
    print("volume dialog: dB texts, preview restarts, apply saves: OK")


def test_tile_badge_follows_the_configured_klangbild_target():
    """F4: the tile badge must read the configured Klangbild target for the sound's
    category (from the core snapshot), not always the voice target - a music sound
    with a custom target must show a different auto-dB than the voice target would."""
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    c.send(p.SetKlangbild({"music": -18.0}))  # first: the rebuild below must already see it
    c.send(p.SetSoundCategory(sound["id"], "music"))  # triggers board.rebuild()
    app.update_idletasks()
    sound = next(s for s in app.sounds() if s["id"] == sound["id"])  # app.snapshot's own copy
    assert sound["category"] == "music", sound
    cfg = {"klangbild_targets": app.snapshot["devices"]["levels"]["targets"]}
    assert cfg["klangbild_targets"]["music"] == -18.0, cfg
    expected_badge = levels.loudness_badge(sound, cfg)
    assert expected_badge != levels.loudness_badge(sound), "the fixture must actually differ"
    tile = app.board.tiles[sound["id"]]
    assert tile.level_label.cget("text") == expected_badge, tile.level_label.cget("text")
    app.destroy()
    print("tile badge follows the configured Klangbild target: OK")


def test_tile_badge_updates_when_only_the_klangbild_target_changes():
    """K1: SetSoundCategory rebuilds the board first (sounds JSON changed by the
    category). A later SetKlangbild only changes state["devices"]["levels"]["targets"],
    not state["sounds"] - the rebuild key must still notice, or the badge stays stale."""
    app, c, events = make_app()
    sound = add_fixture_sound(app)
    c.send(p.SetSoundCategory(sound["id"], "music"))  # rebuilds once, sounds JSON changed
    app.update_idletasks()
    c.send(p.SetKlangbild({"music": -18.0}))  # only targets change now
    app.update_idletasks()
    sound = next(s for s in app.sounds() if s["id"] == sound["id"])
    cfg = {"klangbild_targets": app.snapshot["devices"]["levels"]["targets"]}
    assert cfg["klangbild_targets"]["music"] == -18.0, cfg
    expected_badge = levels.loudness_badge(sound, cfg)
    tile = app.board.tiles[sound["id"]]
    assert tile.level_label.cget("text") == expected_badge, tile.level_label.cget("text")
    app.destroy()
    print("tile badge updates when only the klangbild target changes: OK")


def test_settings_rows_from_the_snapshot_without_leaks():
    app, c, events = make_app()
    app.show_view("settings")
    view = app.views["settings"]
    app.update_idletasks()
    rows = app.snapshot["devices"]["output_rows"]
    assert rows and len(view.scroll.winfo_children()) == len(rows)
    for _ in range(5):
        view.refresh()
    app.update_idletasks()
    assert len(view.scroll.winfo_children()) == len(rows), "refresh() must not leak rows"
    assert len(view.mixer_host.winfo_children()) == 1, "exactly one mixer panel"

    app.snapshot = {"devices": {}}
    view.refresh()
    app.update_idletasks()
    children = view.scroll.winfo_children()
    assert len(children) == 1 and "Kein virtuelles Mikrofon" in children[0].cget("text")
    app.destroy()
    print("settings rows come from the snapshot, no leaked rows, empty state: OK")


def test_output_row_and_mixer_panel_from_a_snapshot():
    from soundboard import dynamics, levels

    app, c, events = make_app()
    sent = []
    app.core = type("Rec", (), {"send": staticmethod(sent.append)})()
    host = gui.ctk.CTkFrame(app)
    row = {"key": "cable-a", "label": "Cable A", "subtitle": "CABLE-A In",
           "settings": {"mic": True, "mic_gain": 1.0, "sounds": False, "sounds_gain": 0.5}}
    output_row = gui.OutputRow(host, app, row)
    assert output_row.key == "cable-a"

    devices = {"microphones": ["Mikro A", "Mikro B"], "mic_name": "Mikro B",
               "levels": {"sounds_offset_db": -9.0, "ducking_enabled": False, "ducking_db": -12.0}}
    panel = gui.MixerPanel(host, app, devices)
    assert panel.menu.get() == "Mikro B"
    panel._pick_mic("Mikro A")
    assert sent[-1] == p.SetMicrophone("Mikro A")
    panel.menu.set("Mikro A")
    panel.sync(devices)
    assert panel.menu.get() == "Mikro B", "sync puts the dropdown back"
    empty = gui.MixerPanel(host, app, {})
    assert empty.menu.get() == "Kein Mikrofon gefunden"
    n = len(sent)
    empty._pick_mic("Kein Mikrofon gefunden")
    assert len(sent) == n, "the placeholder entry sends nothing"

    line = gui.ctk.CTkFrame(host)
    slider, label = gui._db_slider(line, -20.0, 0.0, -6.0, sent.append, levels.describe_db)
    assert label.cget("text") == levels.describe_db(-6.0)
    assert slider.get() == -6.0
    app.set_output("cable-a", sounds_gain=dynamics.db_to_gain(-6.0))
    assert sent[-1] == p.SetOutput("cable-a", {"sounds_gain": dynamics.db_to_gain(-6.0)})
    app.destroy()
    print("output row, mixer panel and dB slider texts from a snapshot: OK")


def test_export_guard():
    app, c, events = make_app()
    asked = []
    orig = gui.filedialog.asksaveasfilename
    gui.filedialog.asksaveasfilename = lambda **kw: asked.append(kw) or ""
    try:
        MESSAGES.clear()
        app.export_sounds(None)
        assert MESSAGES[-1] == ("info", "Noch keine Sounds zum Exportieren.") and not asked
        sound = add_fixture_sound(app, "Air/Horn")
        app.export_sounds([sound["id"]])
        assert asked and "/" not in asked[-1]["initialfile"], "dialog opens with a safe name"
    finally:
        gui.filedialog.asksaveasfilename = orig
    app.destroy()
    print("export guard and dialog suggestion: OK")


# ---- kept from the old file: pure config / logging functions ----

def _with_data_dir(fn):
    """Run fn(data_dir) with RUCKUS_DATA_DIR pointed at a fresh temp dir."""
    d = Path(tempfile.mkdtemp(prefix="ruckus-cfg-"))
    old = os.environ["RUCKUS_DATA_DIR"]
    os.environ["RUCKUS_DATA_DIR"] = str(d)
    try:
        fn(d)
    finally:
        os.environ["RUCKUS_DATA_DIR"] = old
        shutil.rmtree(d, ignore_errors=True)


def test_config_file():
    logger = logging.getLogger("soundboard.config")
    captured = []
    handler = logging.Handler()
    handler.emit = captured.append
    logger.addHandler(handler)

    def atomic(d):
        cfg = config.load_config()
        cfg["monitor_volume"] = 0.3
        config.save_config(cfg)
        assert not (d / "config.json.tmp").exists(), "tmp file must not be left behind"
        assert json.loads((d / "config.json").read_text(encoding="utf-8"))["monitor_volume"] == 0.3
        cfg["bad"] = object()
        try:
            config.save_config(cfg)
        except TypeError:
            pass
        else:
            raise AssertionError("expected TypeError for unserializable config")
        assert json.loads((d / "config.json").read_text(encoding="utf-8"))["monitor_volume"] == 0.3
        assert not (d / "config.json.tmp").exists()

    def corrupt(d):
        (d / "config.json.bak").write_text("older backup", encoding="utf-8")
        (d / "config.json").write_text('{"sounds": [', encoding="utf-8")
        cfg = config.load_config()
        assert cfg == config.DEFAULT_CONFIG, cfg
        assert config.last_load_was_reset is True
        assert (d / "config.json.bak").read_text(encoding="utf-8") == '{"sounds": ['
        config.load_config()
        assert config.last_load_was_reset is False, "a healthy load clears the flag"
        (d / "config.json").write_bytes(b"\xff\xfe\x00garbage")
        config.load_config()
        assert config.last_load_was_reset is True, "UnicodeDecodeError must also recover"
        assert any("config.json is corrupt" in r.getMessage() for r in captured), "reset must be logged"

    try:
        _with_data_dir(atomic)
        _with_data_dir(corrupt)
    finally:
        logger.removeHandler(handler)
    cfg = {"sounds": [{"id": "a", "name": "Horn"}, {"id": "b", "name": "Bruh"}]}
    assert config.unique_name(cfg, "Horn", exclude_id="a") == "Horn"
    assert config.unique_name(cfg, "Horn", exclude_id="b") == "Horn (2)"
    assert config.unique_name(cfg, "Horn") == "Horn (2)"
    print("config: atomic save, corrupt-file recovery, unique_name: OK")


def test_file_log():
    from logging.handlers import RotatingFileHandler

    from soundboard import main as main_mod

    def check(d):
        old_stderr = sys.stderr
        sys.stderr = None  # windowed exe: no console
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            handler = main_mod.configure_logging(d)
            logging.getLogger("ruckus.test").error("Testeintrag")
            handler.flush()
        finally:
            sys.stderr = old_stderr
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)
                    h.close()
        assert "Testeintrag" in (d / "ruckus.log").read_text(encoding="utf-8")
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 1024 * 1024 and handler.backupCount == 3

    _with_data_dir(check)
    print("file log (ruckus.log, 1 MB x 3, stderr None ok): OK")


def test_build_app_starts_the_core_after_subscribing():
    from soundboard import main as app_main
    c, _ = core_fakes.make_core()
    app = app_main.build_app(core=c)
    app.withdraw()
    assert app.snapshot.get("settings") is not None, "state loaded after start"
    assert app.dock.status_label.cget("text"), "start result reached the dock"
    app_main.show_first_screen(app)
    assert app.onboarding is not None, "fresh config opens the assistant"
    app.onboarding.close_early()
    app.destroy()
    print("build_app subscribes, starts the core and shows the first screen: OK")


def test_show_first_screen_waits_for_a_real_state():
    from soundboard import main as app_main
    c, _ = core_fakes.make_core()
    app = app_main.build_app(core=c)
    app.withdraw()
    app.snapshot = {}
    app.refresh_state = lambda: None  # the core does not answer in time
    app_main.show_first_screen(app)
    assert app.onboarding is None, "no state, no assistant"
    app.destroy()
    print("a late core state never reopens the assistant: OK")


def test_on_close_asks_while_the_mic_runs_and_shuts_the_core_down():
    from soundboard import main as app_main
    c, _ = core_fakes.make_core()
    app = app_main.build_app(core=c)
    app.withdraw()
    real_askyesno = app_main.messagebox.askyesno
    real_destroy = app.destroy
    destroyed: list[bool] = []
    try:
        asked: list[str] = []
        app_main.messagebox.askyesno = lambda title, text, **kw: asked.append(title) or False
        app.iconify = lambda: asked.append("iconify")
        app_main.on_close(app)
        sink = c.routing._backend.sinks[-1]
        assert asked == ["Ruckus Radio beenden?", "iconify"] and not sink.stopped
        app_main.messagebox.askyesno = lambda title, text, **kw: True
        app.destroy = lambda: (destroyed.append(True), real_destroy())
        app_main.on_close(app)
        assert destroyed == [True] and sink.stopped, "core.shutdown ran (inline core stays 'alive')"
    finally:
        app_main.messagebox.askyesno = real_askyesno
        if not destroyed:
            real_destroy()  # one Tk root at a time: later tests build their own
    print("closing asks while the mic runs, then shuts the core down: OK")


def test_update_button_asks_and_installs():
    release, files = core_fakes.fake_release(version="9.0.0", notes="Neu")
    app, c, events = make_app(updates=core_fakes.FakeReleaseSource(release, files))
    app.show_view("settings")
    view = app.views["settings"]
    assert view.update_label.cget("text") == f"Version {app.snapshot['updates']['current']}"
    asked: list[str] = []
    real_askyesno = gui.messagebox.askyesno  # shared module: later tests need the real one
    try:
        gui.messagebox.askyesno = lambda title, text, **kw: asked.append(text) or False
        app.check_updates()
        assert asked and asked[0].startswith("Version 9.0.0 ist da") and "Neu" in asked[0]
        assert app.snapshot["updates"]["status"] == "available", "no: nothing downloaded"
        launched: list[str] = []
        app.launch_update = launched.append
        gui.messagebox.askyesno = lambda title, text, **kw: True
        app.check_updates()
        assert launched and launched[0].endswith("RuckusRadioSetup-9.0.0.exe")
    finally:
        gui.messagebox.askyesno = real_askyesno
    app.destroy()
    print("update: button asks, 'Ja' downloads and hands the installer over: OK")


def test_launch_update_starts_the_installer_and_closes_without_asking():
    # The actual Popen/env/log-path mechanics live in updates.launch_installer now
    # (tests/test_updates_logic.py); this only checks gui.launch_update's own wiring:
    # hand the path to the shared launcher, close on success, show an error and stay
    # open on failure - no microphone question either way, the user already agreed.
    app, c, events = make_app()
    closed: list[bool] = []
    app.shutdown_and_close = lambda: closed.append(True)
    calls: list[str] = []
    original_launch = gui.launch_installer
    try:
        gui.launch_installer = lambda path: calls.append(path) or True
        app.launch_update("C:/tmp/RuckusRadioSetup-9.0.0.exe")
        assert calls == ["C:/tmp/RuckusRadioSetup-9.0.0.exe"]
        assert closed == [True], "no microphone question: the user already agreed"
        MESSAGES.clear()
        gui.launch_installer = lambda path: False
        closed.clear()
        app.launch_update("C:/tmp/x.exe")
        assert closed == [] and MESSAGES and MESSAGES[-1][0] == "error"
    finally:
        gui.launch_installer = original_launch
    app.destroy()
    print("launch_update hands the path to the shared launcher and closes on success, "
          "a failed start keeps it open: OK")


def test_pump_stops_once_a_callback_closes_the_app():
    app, c, events = make_app()
    ran: list[str] = []
    app.call_in_ui(app.shutdown_and_close)  # what UpdateReady -> launch_update does
    app.call_in_ui(ran.append, "late")  # e.g. a StateChanged queued during shutdown
    app._pump_ui()
    assert ran == [], "no callback may touch the destroyed window"
    print("closing from inside the pump stops the pump: OK")


def main():
    tests = [
        test_board_follows_the_core_state,
        test_board_only_touches_the_changed_tile,
        test_click_plays_and_rings_until_the_end,
        test_notices_reach_dock_and_dialogs,
        test_icon_change_rebuilds_the_tile,
        test_hotkey_dialog_suspends_sets_and_resumes,
        test_hotkey_dialog_destroyed_from_outside_resumes,
        test_hotkey_conflict_is_checked_against_the_state,
        test_settings_rebuild_only_on_structure,
        test_mic_busy_puts_the_dropdown_back,
        test_signal_check_button_and_summary,
        test_discord_button_mutes_the_sounds_in_discord_only,
        test_headphone_hint,
        test_toggle_mic_shows_the_mute_button_state,
        test_config_reset_hint_once,
        test_threaded_events_go_through_the_ui_queue_and_the_watchdog,
        test_export_default_name,
        test_navigation,
        test_grid_slots_search_and_tuner,
        test_playing_ring_survives_a_rebuild,
        test_dock_status_and_mic_button_from_the_snapshot,
        test_missing_tile_blocks_hotkey_capture,
        test_tile_icon_missing_or_unreadable_falls_back,
        test_volume_dialog_texts_preview_and_apply,
        test_tile_badge_follows_the_configured_klangbild_target,
        test_tile_badge_updates_when_only_the_klangbild_target_changes,
        test_settings_rows_from_the_snapshot_without_leaks,
        test_output_row_and_mixer_panel_from_a_snapshot,
        test_build_app_starts_the_core_after_subscribing,
        test_show_first_screen_waits_for_a_real_state,
        test_on_close_asks_while_the_mic_runs_and_shuts_the_core_down,
        test_update_button_asks_and_installs,
        test_launch_update_starts_the_installer_and_closes_without_asking,
        test_pump_stops_once_a_callback_closes_the_app,
        test_export_guard,
        test_config_file,
        test_file_log,
    ]
    try:
        for test in tests:
            test()
            assert not SWALLOWED, f"{test.__name__}: {SWALLOWED[0].exc_info}"
        print("\nALL GUI CHECKS PASSED")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
