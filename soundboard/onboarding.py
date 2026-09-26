"""First-run assistant: 5 steps (welcome, mic, VoiceMeeter, first sound, hotkey).
The compact tuner strip on top is the progress bar — the needle tunes one
station further per step. Reuses the board's add-sound and hotkey dialogs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from soundboard import protocol as p, theme
from soundboard.devices import mic_choices  # noqa: F401 - old import path
from soundboard.gui import TunerBanner
from soundboard.layout import keycap_text
from soundboard.tuner import FREQ_MAX, FREQ_MIN
from soundboard.widgets import AddSoundDialog, HotkeyCaptureDialog, primary_button, quiet_button

if TYPE_CHECKING:
    from soundboard.gui import RuckusRadioApp

STEP_COUNT = 5
STEP_TITLES = {
    1: "Willkommen bei Ruckus Radio",
    2: "Mikrofon wählen",
    3: "Virtuelles Mikrofon prüfen",
    4: "Ersten Sound hinzufügen",
    5: "Hotkey vergeben",
}
STRIP_HEIGHT = 56

def step_needle(step: int) -> float:
    step = min(max(int(step), 1), STEP_COUNT)
    return round((step - 0.5) / STEP_COUNT, 6)


def step_frequency(step: int) -> str:
    return f"{FREQ_MIN + (FREQ_MAX - FREQ_MIN) * step_needle(step):.1f}"


def next_action(step: int, state: dict) -> tuple[str, bool]:
    """(label, enabled) of the forward button for `step` given the assistant state."""
    if step == 1:
        return "Los geht's", True
    if step == 2:
        if not state.get("mic_choices"):
            return "Überspringen", True
        return "Weiter", bool(state.get("microphone"))
    if step == 3:
        return ("Weiter", True) if state.get("vm_found") else ("Später einrichten", True)
    if step == 4:
        return ("Weiter", True) if state.get("sound_count", 0) > 0 else ("Überspringen", True)
    return "Ruckus Radio öffnen", True


def can_advance(step: int, state: dict) -> bool:
    return next_action(step, state)[1]


class OnboardingWindow(ctk.CTkToplevel):
    def __init__(self, app: "RuckusRadioApp", on_done: Callable[[bool], None],
                 mics: list[str] | None = None, preselect: bool = True):
        super().__init__(app)
        self.app = app
        self.on_done = on_done
        self._closed = False
        app.core.send(p.SetOnboardingActive(True))
        self.title("Ruckus Radio – Einrichtung")
        self.geometry("720x500")
        self.minsize(640, 480)
        self.configure(fg_color=theme.BG)

        devices_state = app.snapshot.get("devices") or {}
        default_mic = None
        self._mics_from_core = mics is None  # tests pin the list with mics=
        self._preselect = preselect
        if mics is None:
            mics = list(devices_state.get("mic_choices") or [])
            default_mic = devices_state.get("default_mic")
        settings = app.snapshot.get("settings") or {}
        saved = settings.get("microphone_name")
        self.flow = {
            "mic_choices": list(mics),
            "microphone": None,
            "vm_checked": False,
            "vm_found": False,
            "vm_name": None,
            "vm_signal": None,  # None = not measured, False = device found but silent
            "vm_discord": None,  # exact recording device the voice chat has to use
            "vm_reason": "",
            "sound_count": len(app.sounds()),
            "awaiting": None,  # None | "devices" | "signal": which core answer step 3 waits for
        }
        self.step = 1

        outer = ctk.CTkFrame(self, fg_color=theme.BG, corner_radius=0)
        outer.pack(fill="both", expand=True, padx=24, pady=24)

        self.strip = TunerBanner(outer, height=STRIP_HEIGHT, wordmark=False,
                                 needle=step_needle(1), radius=12)
        self.strip.pack(fill="x")

        meta = ctk.CTkFrame(outer, fg_color="transparent")
        meta.pack(fill="x", pady=(14, 0))
        self.step_label = ctk.CTkLabel(meta, text="", height=18, text_color=theme.MUTED,
                                       font=theme.body_font(theme.SIZE_TILE_NAME))
        self.step_label.pack(side="left")
        self.freq_label = ctk.CTkLabel(meta, text="", height=18, text_color=theme.MUTED,
                                       font=theme.mono_font(theme.SIZE_TILE_NAME))
        self.freq_label.pack(side="right")

        self.title_label = ctk.CTkLabel(outer, text="", anchor="w", text_color=theme.TEXT,
                                        font=theme.display_font(theme.SIZE_WORDMARK - 6))
        self.title_label.pack(fill="x", pady=(2, 0))

        footer = ctk.CTkFrame(outer, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(16, 0))
        self.back_button = quiet_button(footer, "Zurück", self.go_back, width=110,
                                        text_color_disabled=theme.LINE)
        self.back_button.pack(side="left")
        self.next_button = primary_button(footer, "", self.go_next, width=170,
                                          text_color_disabled=theme.MUTED)
        self.next_button.pack(side="right")

        self.card = ctk.CTkFrame(outer, fg_color=theme.PANEL, corner_radius=14)
        self.card.pack(fill="both", expand=True, pady=(14, 0))
        self.body = ctk.CTkFrame(self.card, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=20, pady=20)

        if saved in mics:
            self.set_microphone(saved)
        elif preselect and default_mic in mics:
            self.set_microphone(default_mic)

        app.state_listeners.append(self._on_state)
        app.device_listeners.append(self._on_devices)
        app.signal_listeners.append(self._signal_result)
        self.protocol("WM_DELETE_WINDOW", self.close_early)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.show_step(1)
        self.after(100, self._raise)

    def _raise(self) -> None:
        if self.winfo_exists() and self.winfo_viewable():
            self.lift()
            self.focus_force()

    # ---- state ----

    def set_microphone(self, name: str) -> None:
        self.flow["microphone"] = name
        self.app.core.send(p.SetMicrophone(name, apply=False))  # applied by the rescan on close
        self._update_footer()

    def _on_state(self, state: dict) -> None:
        count = len(state.get("sounds") or [])
        if count != self.flow["sound_count"]:
            self.flow["sound_count"] = count
            if self.step in (4, 5):
                self.show_step(self.step)
        if self._mics_from_core:
            self._sync_mic_choices(state)
        self._sync_autostart(state)

    def _sync_mic_choices(self, state: dict) -> None:
        """A real core resolves the devices after start(): the assistant may open on an
        empty list, so it picks the microphones up from any later state."""
        devices_state = state.get("devices") or {}
        choices = list(devices_state.get("mic_choices") or [])
        if choices == self.flow["mic_choices"]:
            return
        self.flow["mic_choices"] = choices  # before set_microphone: its state re-enters here
        if self.flow["microphone"] is None:
            saved = (state.get("settings") or {}).get("microphone_name")
            default_mic = devices_state.get("default_mic")
            if saved in choices:
                self.set_microphone(saved)
            elif self._preselect and default_mic in choices:
                self.set_microphone(default_mic)
        if self.step == 2:
            self.show_step(2)
        self._update_footer()

    def _sync_autostart(self, state: dict) -> None:
        """The checkbox follows the core: a refused SetAutostart puts it back."""
        box = getattr(self, "autostart_box", None)
        if self.step != 3 or box is None or not box.winfo_exists():
            return
        if (state.get("settings") or {}).get("autostart"):
            box.select()
        else:
            box.deselect()

    def _first_sound(self) -> dict | None:
        sounds = self.app.sounds()
        return sounds[0] if sounds else None

    # ---- navigation ----

    def show_step(self, step: int) -> None:
        self.step = min(max(step, 1), STEP_COUNT)
        self.strip.set_needle(step_needle(self.step))
        self.step_label.configure(text=f"Schritt {self.step} von {STEP_COUNT}")
        self.freq_label.configure(text=f"{step_frequency(self.step)} MHz")
        self.title_label.configure(text=STEP_TITLES[self.step])
        for child in self.body.winfo_children():
            child.destroy()
        getattr(self, f"_build_step_{self.step}")()
        self._update_footer()

    def _update_footer(self) -> None:
        label, enabled = next_action(self.step, self.flow)
        self.next_button.configure(text=label, state="normal" if enabled else "disabled")
        self.back_button.configure(state="normal" if self.step > 1 else "disabled")

    def go_next(self) -> None:
        if not can_advance(self.step, self.flow):
            return
        if self.step == STEP_COUNT:
            self.finish()
        else:
            self.show_step(self.step + 1)

    def go_back(self) -> None:
        if self.step > 1:
            self.show_step(self.step - 1)

    def finish(self) -> None:
        self.app.core.send(p.CompleteOnboarding())
        self._close(True)

    def close_early(self) -> None:
        self._close(False)

    def _close(self, completed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self.app.core.send(p.SetOnboardingActive(False))
        self.app.core.send(p.Rescan())  # the core no longer scans by itself; applies the mic too
        self.destroy()
        self.on_done(completed)

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        for listeners, fn in ((self.app.state_listeners, self._on_state),
                              (self.app.device_listeners, self._on_devices),
                              (self.app.signal_listeners, self._signal_result)):
            if fn in listeners:
                listeners.remove(fn)

    # ---- step content ----

    def _text(self, text: str, color: str = theme.MUTED, pady=0) -> ctk.CTkLabel:
        label = ctk.CTkLabel(self.body, text=text, text_color=color, font=theme.body_font(),
                             anchor="w", justify="left", wraplength=580)
        label.pack(fill="x", pady=pady)
        return label

    def _build_step_1(self) -> None:
        self._text("Ruckus Radio spielt deine Sounds per Hotkey direkt in Discord, Steam und Spiele — "
                   "als wären sie dein Mikrofon.", theme.TEXT)
        self._text("In fünf kurzen Schritten wählst du dein Mikro, prüfst VoiceMeeter "
                   "und legst deinen ersten Sound samt Hotkey an.", pady=(10, 0))
        version = (self.app.snapshot.get("updates") or {}).get("current")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(18, 0))
        ctk.CTkLabel(row, text=f"Version {version}" if version else "", text_color=theme.MUTED,
                     font=theme.body_font()).pack(side="left")
        quiet_button(row, "Nach Updates suchen", self.app.check_updates, width=170).pack(
            side="left", padx=(12, 0))

    def _build_step_2(self) -> None:
        choices = self.flow["mic_choices"]
        if not choices:
            self._text("Kein Mikrofon gefunden. Schließ eins an und öffne den Assistenten später "
                       "über „Assistent“ in der Seitenleiste.", theme.TEXT)
            return
        self._text("Welches Mikrofon benutzt du im Voice-Chat?", theme.TEXT)
        self.mic_menu = ctk.CTkOptionMenu(
            self.body, values=choices, command=self.set_microphone, height=38, corner_radius=10,
            fg_color=theme.RAISED, button_color=theme.RAISED, button_hover_color=theme.LINE,
            text_color=theme.TEXT, font=theme.body_font(), dropdown_font=theme.body_font(),
            dropdown_fg_color=theme.PANEL, dropdown_hover_color=theme.RAISED,
            dropdown_text_color=theme.TEXT, anchor="w", dynamic_resizing=False,
        )
        self.mic_menu.set(self.flow["microphone"] or "Mikrofon wählen…")
        self.mic_menu.pack(fill="x", pady=(12, 12))
        self._text("Ruckus Radio mischt dieses Mikro mit deinen Sounds zusammen.")

    def _build_step_3(self) -> None:
        self._text("Ruckus Radio mischt Mikro und Sounds in ein virtuelles Mikrofon. "
                   "Discord und Steam nehmen dann dieses Gerät auf statt deines Mikros.", theme.TEXT)
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(14, 0))
        self.check_button = quiet_button(row, "Erneut prüfen" if self.flow["vm_checked"] else "Jetzt prüfen",
                                         self.check_voicemeeter, width=140)
        self.check_button.pack(side="left")
        self.vm_mark = ctk.CTkLabel(row, text="✓", width=24, text_color=theme.ACCENT,
                                    font=theme.body_font(18, "bold"))
        self.copy_button = quiet_button(row, "Discord-Gerät kopieren", self.copy_discord_device,
                                        width=190)
        self.vm_result = ctk.CTkLabel(row, text="", anchor="w", text_color=theme.TEXT, font=theme.body_font())
        self.vm_result.pack(side="right", fill="x", expand=True, padx=(14, 0))
        self.vm_steps = ctk.CTkLabel(self.body, text="", anchor="nw", justify="left", wraplength=580,
                                     text_color=theme.MUTED, font=theme.body_font())
        self.vm_steps.pack(fill="x", pady=(14, 0))
        self.autostart_box = ctk.CTkCheckBox(
            self.body, text="Mit Windows starten (Discord hat nur ein Mikro, solange Ruckus Radio läuft)",
            command=self.toggle_autostart, text_color=theme.MUTED, font=theme.body_font(),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT, checkmark_color=theme.ACCENT_INK,
            border_color=theme.LINE, checkbox_width=18, checkbox_height=18,
        )
        if (self.app.snapshot.get("settings") or {}).get("autostart"):
            self.autostart_box.select()
        self.autostart_box.pack(anchor="w", pady=(18, 0))
        self._show_vm_result()

    def toggle_autostart(self) -> None:
        self.app.core.send(p.SetAutostart(bool(self.autostart_box.get())))

    def check_voicemeeter(self) -> None:
        """Two stages: find the device (Rescan -> DevicesChanged), then prove a tone
        travels through it (RunSignalCheck -> SignalCheckDone)."""
        self.flow["awaiting"] = "devices"
        if self.step == 3:
            self.vm_result.configure(text="Wird gesucht …", text_color=theme.MUTED)
        self.app.core.send(p.Rescan())

    def _on_devices(self, summary: dict) -> None:
        if self.flow["awaiting"] != "devices":
            return  # a rescan somebody else started (e.g. headphone switch)
        self.flow.update(vm_checked=True, vm_found=bool(summary.get("found")),
                         vm_name=summary.get("name"), vm_signal=None,
                         vm_discord=summary.get("discord_device_name"), awaiting=None)
        if self._closed:
            return
        if self.step == 3:
            self.check_button.configure(text="Erneut prüfen")
            self._show_vm_result()
        self._update_footer()
        if self.flow["vm_found"]:
            self.flow["awaiting"] = "signal"
            if self.step == 3:
                self.vm_result.configure(text="Signal wird geprüft …", text_color=theme.MUTED)
            self.app.core.send(p.RunSignalCheck())

    def _signal_result(self, results: list[dict]) -> None:
        """The signal check now measures every cable the mixer opened, not just the one
        this step recommends - pick out that one's own result by its key (the exact
        recording device name, same as `vm_discord`) so step 3 keeps showing the
        verdict for the device it is actually telling the user to pick in Discord.
        Empty results: the core reported NO_VIRTUAL_MIC or CHECK_FAILED itself."""
        if self.flow["awaiting"] != "signal":
            return
        self.flow["awaiting"] = None
        if not results:
            self.flow["vm_signal"] = False
            self.flow["vm_reason"] = "Prüfung fehlgeschlagen."
            if not self._closed and self.step == 3:
                self._show_vm_result()
            return
        result = next((r for r in results if r.get("key") == self.flow.get("vm_discord")),
                      results[0] if results else {})
        self.flow["vm_signal"] = bool(result.get("ok"))
        self.flow["vm_reason"] = result.get("reason", "")
        if self._closed or self.step != 3:
            return
        self._show_vm_result()

    def copy_discord_device(self) -> None:
        name = self.flow.get("vm_discord")
        if not name:
            return
        self.clipboard_clear()
        self.clipboard_append(name)
        self.copy_button.configure(text="Kopiert ✓")

    def _show_vm_result(self) -> None:
        if not self.flow["vm_checked"]:
            return
        if self.flow["vm_found"]:
            self._show_vm_found()
            return
        mic = self.flow["microphone"] or "dein Mikro"
        self.vm_mark.pack_forget()
        self.copy_button.pack_forget()
        self.vm_result.pack_configure(padx=(14, 0))
        self.vm_result.configure(text="Kein virtuelles Mikrofon gefunden. So richtest du es ein:",
                                 text_color=theme.MUTED)
        self.vm_steps.configure(text="\n".join([
            "1  VB-CABLE von vb-audio.com/Cable installieren, PC neu starten.",
            "2  Discord und Steam: Eingabegerät „CABLE Output (VB-Audio Virtual Cable)“.",
            "3  Discord: Rauschunterdrückung und Echounterdrückung aus, sonst filtert es die Sounds weg.",
            f"4  Ruckus Radio mischt {mic} selbst dazu — es muss dafür laufen.",
            "5  Danach „Erneut prüfen“.",
        ]))

    def _show_vm_found(self) -> None:
        signal = self.flow.get("vm_signal")
        discord = self.flow.get("vm_discord")
        self.vm_mark.configure(text="✓" if signal else ("…" if signal is None else "!"),
                               text_color=theme.ACCENT if signal is not False else theme.DANGER)
        self.vm_mark.pack(side="left", padx=(14, 0))
        self.vm_result.pack_configure(padx=(6, 0))
        self.vm_result.configure(text=f"Gefunden: {self.flow['vm_name']}", text_color=theme.TEXT)
        if discord:
            self.copy_button.pack(side="left", padx=(14, 0))
        lines = [f"Discord → Einstellungen → Sprache & Video → Eingabegerät: „{discord}“."] if discord else []
        lines.append("Rauschunterdrückung und Echounterdrückung in Discord ausschalten.")
        if signal is False:
            lines.insert(0, self.flow.get("vm_reason", "Kein Signal.") + " Gerät oder Routing prüfen.")
        elif signal:
            lines.insert(0, "Signal kommt an — die Gegenstelle hört deine Sounds.")
        self.vm_steps.configure(text="\n".join(lines))

    def _build_step_4(self) -> None:
        self._text("Wähl eine MP3 oder MP4 und optional ein Icon. Der Sound landet sofort auf deinem Board.",
                   theme.TEXT)
        quiet_button(self.body, "Sound hinzufügen…", self.open_add_sound, width=170).pack(
            anchor="w", pady=(14, 0))
        count = self.flow["sound_count"]
        if count:
            last = self.app.sounds()[-1]["name"]
            text = f"„{last}“ ist auf dem Board." if count == 1 else f"{count} Sounds auf dem Board, zuletzt „{last}“."
            self._text(text, theme.TEXT, pady=(14, 0))
        else:
            self._text("Noch kein Sound auf dem Board.", pady=(14, 0))

    def open_add_sound(self) -> None:
        AddSoundDialog(self, on_saved=self.app.add_sound)

    def _build_step_5(self) -> None:
        sound = self._first_sound()
        stop = keycap_text((self.app.snapshot.get("settings") or {}).get("stop_all_hotkey")) or "—"
        if sound is None:
            self.hotkey_title = self._text("Noch kein Sound vorhanden. Hotkeys vergibst du später per "
                                           "Rechtsklick auf einen Sound.", theme.TEXT)
        else:
            self.hotkey_title = self._text(f"Hotkey für „{sound['name']}“", theme.TEXT)
            row = ctk.CTkFrame(self.body, fg_color="transparent")
            row.pack(fill="x", pady=(12, 0))
            keycap = keycap_text(sound.get("hotkey"))
            ctk.CTkLabel(
                row, text=keycap or "Kein Hotkey", height=38, corner_radius=10, fg_color=theme.RAISED,
                text_color=theme.ACCENT if keycap else theme.MUTED,
                font=theme.mono_font(14, "bold") if keycap else theme.body_font(),
            ).pack(side="left", ipadx=14)
            quiet_button(row, "Hotkey ändern…" if keycap else "Hotkey aufnehmen…",
                         lambda: HotkeyCaptureDialog(self, self.app, sound), width=160).pack(
                side="left", padx=(12, 0))
            self._text("Der Hotkey funktioniert überall, auch mitten im Spiel.", pady=(12, 0))
        self._text(f"Alle Sounds stoppen: {stop}", pady=(6, 0))
