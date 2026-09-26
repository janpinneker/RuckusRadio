"""Main window: nav rail, tuner banner, board view (6-column grid + empty slots), dock.

The window holds no domain state of its own: it shows the core's last snapshot
(`state`) and turns clicks into protocol commands (core.send). Core events arrive on
the core thread and reach the Tk thread through call_in_ui's queue (a core in test
mode, inline, is handled at once)."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from soundboard import protocol as p, theme
from soundboard.layout import signal_check_summary
from soundboard.library import NOTHING_TO_EXPORT
from soundboard.packs import sanitize_filename
from soundboard.routing import NO_VIRTUAL_MIC
from soundboard.updates import UPDATE_QUESTION
from soundboard.widgets import AddSoundDialog, HotkeyCaptureDialog, SoundTile, VolumeDialog

ctk.set_appearance_mode("dark")

log = logging.getLogger(__name__)

from soundboard.views import (  # noqa: E402,F401 - NAV_ITEMS/TunerBanner/OutputRow re-exported
    BANNER_RADIUS, CONTENT_PAD, NAV_ITEMS, BoardView, Dock, MixerPanel, NavRail, OutputRow,
    SettingsView, TunerBanner, _db_slider,
)

UI_PUMP_MS = 30
WATCHDOG_MS = 1000
HINT_RESET_MS = 15000
CORE_DEAD = "Ruckus Radio reagiert nicht mehr. Bitte Ruckus neu starten."
DISCORD_SOUNDS_OFF = "Sounds in Discord aus – dein Mikro bleibt an."
DISCORD_SOUNDS_ON = "Sounds in Discord wieder an."
CONFIG_RESET_HINT = "Einstellungen waren beschädigt und wurden zurückgesetzt. Sicherung: config.json.bak"
UPDATE_LAUNCH_FAILED = "Das Update konnte nicht gestartet werden. Installationsdatei: {path}"
DETACHED_FLAGS = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def clean_child_env() -> dict:
    """Env for the relaunched installer, without PyInstaller's onefile markers.

    Ruckus itself runs as a PyInstaller onefile exe, so its process carries
    _PYI_ARCHIVE_FILE/_PYI_APPLICATION_HOME_DIR/_PYI_PARENT_PROCESS_LEVEL. Popen
    inherits the environment by default, and the installer's own [Run] entry
    relaunches the (also onefile) app exe with that same environment still set -
    its bootloader then thinks it is a onefile child of this (by then dead)
    process and shows an "Error" window instead of starting normally.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def export_default_name(sounds: list[dict], sound_ids: list[str]) -> str:
    """Vorschlag im Speichern-Dialog: ein Sound heißt wie der Sound, sonst das Board."""
    if len(sound_ids) == 1:
        sound = next((s for s in sounds if s["id"] == sound_ids[0]), None)
        return f"{sanitize_filename(sound['name'])}.ruckuspack" if sound else "Sound.ruckuspack"
    return "Ruckus-Board.ruckuspack"


class RuckusRadioApp(ctk.CTk):
    """Verbindungsstück zwischen Kern und Tk: Ereignisse -> Anzeige, Klicks -> Befehle.
    Liest nur `self.snapshot` (Momentaufnahme des Kerns), schreibt nur über core.send."""

    VIEWS = {"board": BoardView, "settings": SettingsView}
    ACTIONS = {"setup": "open_onboarding"}

    def __init__(self, core):
        super().__init__()
        self.title("Ruckus Radio")
        self.geometry("1100x760")
        self.minsize(960, 680)
        self.configure(fg_color=theme.BG)

        self.core = core
        self.data_dir = core.store.data_dir
        self.snapshot: dict = {}  # not `state`: Tk windows own a state() method
        self.playing_ids: set[str] = set()
        self.missing_ids: set[str] = set()
        self.onboarding = None
        self.device_listeners: list = []
        self.signal_listeners: list = []
        self.state_listeners: list = []
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._sounds_key: str | None = None
        self._settings_key: str | None = None
        self._reset_shown = False
        self._dead_shown = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.rail = NavRail(self, self.on_nav)
        self.rail.grid(row=0, column=0, sticky="ns")
        content = ctk.CTkFrame(self, fg_color=theme.BG, corner_radius=0)
        content.grid(row=0, column=1, sticky="nsew", padx=CONTENT_PAD, pady=(CONTENT_PAD, 0))
        self.dock = Dock(self, self)
        self.dock.grid(row=1, column=0, columnspan=2, sticky="ew")

        self.banner = TunerBanner(content)
        self.banner.pack(fill="x")
        self.view_host = ctk.CTkFrame(content, fg_color=theme.BG, corner_radius=0)
        self.view_host.pack(fill="both", expand=True)
        self.views: dict[str, ctk.CTkFrame] = {}
        self.current_view: str | None = None
        self.show_view("board")

        self._closing = False
        # before core.start(): start notices (CONFIG_RESET, NO_OUTPUT) must not be lost
        self._unsubscribe = core.subscribe(self._on_core_event)
        self.after(UI_PUMP_MS, self._pump_ui)
        self.after(WATCHDOG_MS, self._watch_core)

        self.bind("<Control-f>", self._focus_search)
        self.bind("<Control-F>", self._focus_search)
        self.bind("<Escape>", lambda _e: self.board.clear_search())

    def destroy(self) -> None:
        getattr(self, "_unsubscribe", lambda: None)()
        # no stale pump/watchdog callbacks; plain Tcl cancel, the commands themselves
        # are freed by destroy() below
        for job in self.tk.splitlist(self.tk.call("after", "info")):
            self.tk.call("after", "cancel", job)
        super().destroy()

    # ---- views / rail actions ----

    def on_nav(self, key: str) -> None:
        if key in self.ACTIONS:
            getattr(self, self.ACTIONS[key])()
        else:
            self.show_view(key)

    def show_view(self, key: str) -> None:
        if key not in self.VIEWS or key == self.current_view:
            return
        if key not in self.views:
            self.views[key] = self.VIEWS[key](self.view_host, self)
        for k, view in self.views.items():
            if k == key:
                view.pack(fill="both", expand=True)
            else:
                view.pack_forget()
        self.current_view = key
        self.rail.set_active(key)

    @property
    def board(self) -> BoardView:
        return self.views["board"]  # type: ignore[return-value]

    def _focus_search(self, _event=None):
        self.show_view("board")
        self.board.search.focus_set()
        return "break"

    # ---- core -> Tk ----

    def _on_core_event(self, event) -> None:  # core thread
        if self.core.inline:
            self._handle_event(event)
        else:
            self.call_in_ui(self._handle_event, event)

    def _handle_event(self, event) -> None:  # Tk thread
        if isinstance(event, p.StateChanged):
            self._apply_state(event.state)
        elif isinstance(event, p.Notice):
            self._show_notice(event.text, event.level)
        elif isinstance(event, p.PlaybackStarted):
            self.set_tuner_needle(event.needle)
            self._mark_playing(event.sound_id, True)
        elif isinstance(event, p.PlaybackEnded):
            self._mark_playing(event.sound_id, False)
        elif isinstance(event, p.SoundMissing):
            self.missing_ids.add(event.sound_id)
            tile = self.tile_for(event.sound_id)
            if tile:
                tile.set_missing(True)
        elif isinstance(event, p.HeadphonesSwitched):
            self.show_hint(f"Kopfhörer gewechselt: {event.name}")
        elif isinstance(event, p.SignalCheckDone):
            self.dock.check_button.configure(state="normal", text="Prüfen")
            results = [dict(r) for r in event.results]
            if results:  # empty: the core already sent its own notice
                self.show_hint(signal_check_summary(results)[0])
            for listener in list(self.signal_listeners):
                listener(results)
        elif isinstance(event, p.DevicesChanged):
            for listener in list(self.device_listeners):
                listener(dict(event.summary))
        elif isinstance(event, p.UpdateAvailable):
            text = UPDATE_QUESTION.format(version=event.version)
            if event.notes:
                text += f"\n\n{event.notes}"
            if messagebox.askyesno("Ruckus Radio", text, parent=self):
                self.core.send(p.InstallUpdate())
        elif isinstance(event, p.UpdateReady):
            self.launch_update(event.installer_path)

    def refresh_state(self) -> None:
        try:
            self._apply_state(self.core.get_state())
        except (TimeoutError, RuntimeError):
            log.warning("could not read the core state", exc_info=True)

    def _apply_state(self, state: dict) -> None:
        if not state:
            return
        self.snapshot = state
        sounds_key = json.dumps(state.get("sounds") or [], sort_keys=True)
        if sounds_key != self._sounds_key:
            self._sounds_key = sounds_key
            self.board.rebuild()  # new tiles reload their icon (icon_rev changed)
        playback = state.get("playback") or {}
        self.playing_ids = set(playback.get("playing") or [])
        self.missing_ids = set(playback.get("missing") or [])
        for sound_id, tile in self.board.tiles.items():
            tile.set_playing(sound_id in self.playing_ids)
            tile.set_missing(sound_id in self.missing_ids)
        devices = state.get("devices") or {}
        status = devices.get("status") or {}
        self.dock.set_status(status.get("text", ""), status.get("tone", "off"))
        mixer = devices.get("mixer") or {}
        self.dock.set_mic_button(bool(mixer.get("running")), bool(mixer.get("mic_muted")))
        row = self._discord_row()
        self.dock.set_discord_button(None if row is None else bool(row["settings"].get("sounds")))
        settings_key = json.dumps(self._settings_shape(devices), sort_keys=True)
        if "settings" in self.views:
            view = self.views["settings"]
            if settings_key != self._settings_key:
                view.refresh()
            else:
                view.sync()  # e.g. MIC_BUSY: dropdown back to the real microphone
            view.sync_updates(state.get("updates") or {})
        self._settings_key = settings_key
        if (state.get("settings") or {}).get("config_was_reset") and not self._reset_shown:
            self._reset_shown = True
            self.dock.show_hint(CONFIG_RESET_HINT, ms=HINT_RESET_MS)
        for listener in list(self.state_listeners):
            listener(state)

    @staticmethod
    def _settings_shape(devices: dict) -> dict:
        """What rebuilds the settings page. Gains and offsets are left out on purpose: a
        slider drag would otherwise destroy the slider being dragged. Switch states are
        in, so a switch flipped elsewhere (the dock's Discord button) shows up here."""
        rows = [(r.get("key"), r.get("label"), r.get("subtitle"),
                 bool((r.get("settings") or {}).get("mic")),
                 bool((r.get("settings") or {}).get("sounds")))
                for r in devices.get("output_rows") or []]
        return {"rows": rows, "microphones": devices.get("microphones") or [],
                "mic_name": devices.get("mic_name")}

    def _show_notice(self, text: str, level: str) -> None:
        if level == "hint":
            self.show_hint(text)
            return
        self.show_hint("")  # clears "… wird hinzugefügt …" and friends
        if level == "error":
            messagebox.showerror("Ruckus Radio", text, parent=self)
        else:
            messagebox.showinfo("Ruckus Radio", text, parent=self)

    def _mark_playing(self, sound_id: str, playing: bool) -> None:
        (self.playing_ids.add if playing else self.playing_ids.discard)(sound_id)
        tile = self.tile_for(sound_id)
        if tile:
            tile.set_playing(playing)

    def _watch_core(self) -> None:
        """Spec §5: a dead core thread must be reported, not hang silently."""
        if not self.core.alive and not self._dead_shown:
            self._dead_shown = True
            messagebox.showerror("Ruckus Radio", CORE_DEAD, parent=self)
        if not self._dead_shown:
            self.after(WATCHDOG_MS, self._watch_core)

    # ---- worker -> Tk marshalling ----

    def call_in_ui(self, fn, *args) -> None:
        """Thread-safe: queue fn(*args) for the Tk thread. Makes no Tcl call, so it
        also works before mainloop starts and after the window is gone."""
        self._ui_queue.put((fn, args))

    def _pump_ui(self) -> None:
        while True:
            if self._closing:  # a callback quit the app (update): the widgets are gone
                return
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:  # one failing callback must not stop the pump
                log.exception("ui callback failed")
        self.after(UI_PUMP_MS, self._pump_ui)

    # ---- reading ----

    def sounds(self) -> list[dict]:
        return list(self.snapshot.get("sounds") or [])

    def tile_for(self, sound_id: str) -> SoundTile | None:
        return self.board.tiles.get(sound_id)

    def set_tuner_needle(self, value: float) -> None:
        self.banner.set_needle(value)

    def show_hint(self, text: str) -> None:
        self.dock.show_hint(text)

    def refresh_grid(self) -> None:
        self.board.rebuild()
        for sound_id, tile in self.board.tiles.items():
            tile.set_playing(sound_id in self.playing_ids)
            tile.set_missing(sound_id in self.missing_ids)

    # ---- sounds -> commands ----

    def play_sound(self, sound_id: str, volume: float | None = None) -> None:
        self.core.send(p.Play(sound_id, volume))

    def preview_sound(self, sound_id: str, volume: float) -> None:
        """"Probehören": the headphones only - the voice chat never hears a preview."""
        self.core.send(p.Play(sound_id, volume, preview=True))

    def stop_sound(self, sound_id: str) -> None:
        self.core.send(p.Stop(sound_id))

    def stop_all(self) -> None:
        self.core.send(p.StopAll())

    def open_add_dialog(self) -> None:
        AddSoundDialog(self, on_saved=self.add_sound)

    def add_sound(self, name: str, audio_path: Path, icon_path: Path | None) -> None:
        self.core.send(p.AddSound(str(audio_path), name, str(icon_path) if icon_path else None))

    def delete_sound(self, sound_id: str) -> None:
        self.core.send(p.DeleteSound(sound_id))

    def rename_sound(self, sound: dict, new_name: str) -> None:
        name = new_name.strip()
        if name:
            self.core.send(p.RenameSound(sound["id"], name))

    def open_volume_dialog(self, sound: dict) -> None:
        VolumeDialog(self, self, sound)

    def set_sound_volume(self, sound: dict, volume: float) -> None:
        self.core.send(p.SetSoundVolume(sound["id"], float(volume)))

    def change_icon(self, sound: dict, path: str) -> None:
        self.core.send(p.SetSoundIcon(sound["id"], str(path)))

    def open_hotkey_capture(self, sound: dict) -> None:
        HotkeyCaptureDialog(self, self, sound)

    def suspend_hotkeys(self) -> None:
        self.core.send(p.SuspendHotkeys())

    def set_hotkey(self, sound: dict, hotkey: str) -> None:
        self.core.send(p.SetHotkey(sound["id"], hotkey))

    def remove_hotkey(self, sound: dict) -> None:
        self.core.send(p.RemoveHotkey(sound["id"]))

    def resume_hotkeys(self) -> None:
        self.core.send(p.ResumeHotkeys())

    def export_sounds(self, sound_ids: list[str] | None) -> None:
        """Context menu passes a single id; toolbar passes None (whole board)."""
        sounds = self.sounds()
        ids = [s["id"] for s in sounds] if sound_ids is None else list(sound_ids)
        if not ids:
            messagebox.showinfo("Ruckus Radio", NOTHING_TO_EXPORT, parent=self)
            return
        dest = filedialog.asksaveasfilename(
            parent=self, title="Sounds exportieren", defaultextension=".ruckuspack",
            initialfile=export_default_name(sounds, ids),
            filetypes=[("Ruckus-Radio-Paket", "*.ruckuspack")],
        )
        if dest:
            self.core.send(p.ExportSounds(str(dest), None if sound_ids is None else tuple(ids)))

    def import_pack(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Sound-Paket importieren",
            filetypes=[("Ruckus-Radio-Paket", "*.ruckuspack")],
        )
        if path:
            self.core.send(p.ImportPack(str(path)))

    # ---- devices -> commands ----

    def set_output(self, key: str, **changes) -> None:
        self.core.send(p.SetOutput(key, dict(changes)))  # the store debounces gains

    def set_levels(self, **changes) -> None:
        self.core.send(p.SetLevels(dict(changes)))

    def set_microphone(self, name: str) -> None:
        self.core.send(p.SetMicrophone(name))

    def toggle_mic(self) -> None:
        self.core.send(p.ToggleMicMute())

    def check_signal(self) -> None:
        self.dock.check_button.configure(state="disabled", text="prüft …")
        self.core.send(p.RunSignalCheck())  # SignalCheckDone frees the button again

    def _discord_row(self) -> dict | None:
        """The output row of the cable Discord records from (None: no such cable)."""
        devices = self.snapshot.get("devices") or {}
        name = devices.get("discord_device_name")
        return next((r for r in devices.get("output_rows") or [] if name and r.get("key") == name),
                    None)

    def toggle_discord_sounds(self) -> None:
        """Dock button: the sounds leave Discord's cable (or come back); the voice and
        every other cable stay as they are."""
        row = self._discord_row()
        if row is None:
            self.show_hint(NO_VIRTUAL_MIC)
            return
        on = not bool(row["settings"].get("sounds"))
        self.core.send(p.SetOutput(row["key"], {"sounds": on}))
        self.show_hint(DISCORD_SOUNDS_ON if on else DISCORD_SOUNDS_OFF)

    # ---- updates ----

    def check_updates(self) -> None:
        self.core.send(p.CheckForUpdates())

    def launch_update(self, path: str) -> None:
        """Start the verified installer detached, then quit through the core - the user
        already said yes, so no microphone question here."""
        log_path = Path(path).with_name("install.log")
        # No quotes inside /LOG=: Popen quotes the whole argument when needed, while an inner
        # quote would reach Inno as \" - it then cannot create the log and aborts the setup.
        try:
            subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE",
                              f"/LOG={log_path}"],
                             close_fds=True, creationflags=DETACHED_FLAGS, env=clean_child_env())
        except OSError:
            log.exception("starting the update installer failed")
            self.show_hint("")
            messagebox.showerror("Ruckus Radio", UPDATE_LAUNCH_FAILED.format(path=path), parent=self)
            return
        self.shutdown_and_close()

    def shutdown_and_close(self) -> None:
        self._closing = True
        try:
            self.core.shutdown()  # hooks off, sounds stopped, mic handed back, config flushed
        finally:
            self.destroy()

    # ---- onboarding ----

    def open_onboarding(self) -> None:
        """Hides the board while the assistant runs; its dialogs are parented to it."""
        if self.onboarding is not None and self.onboarding.winfo_exists():
            self.onboarding.lift()
            self.onboarding.focus_force()
            return
        from soundboard.onboarding import OnboardingWindow

        self.withdraw()
        try:
            self.onboarding = OnboardingWindow(self, on_done=self._onboarding_done)
        except Exception:
            self.onboarding = None
            # the window may have claimed the devices before it failed - release them,
            # or every later microphone change would be refused as MIC_BUSY
            self.core.send(p.SetOnboardingActive(False))
            self.deiconify()
            log.exception("onboarding window failed to open")
            self.show_hint("Der Setup-Assistent konnte nicht geöffnet werden. Ruckus Radio läuft normal weiter.")

    def _onboarding_done(self, _completed: bool) -> None:
        self.onboarding = None
        self.deiconify()
        self.lift()
        # the assistant may have changed gains, switches or the microphone while the
        # settings page existed; that page only rebuilds on structure changes -> force it
        self._settings_key = None
        self.refresh_state()
