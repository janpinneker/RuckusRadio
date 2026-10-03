"""Board widgets: sound tile, empty placeholder slot, add-sound dialog."""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image, ImageDraw

from soundboard import filedialogs, hotkeys, icons, theme
from soundboard.config import MAX_SOUND_VOLUME, clamp_volume
from soundboard.dynamics import db_to_gain, gain_to_db
from soundboard.layout import ellipsize, keycap_text
from soundboard.levels import describe_db, format_db, loudness_badge, loudness_summary

if TYPE_CHECKING:
    from soundboard.gui import RuckusRadioApp

ICON_INSET = 8  # room for the hover/playing ring inside the disc
IMAGE_FILETYPES = [("Bilder", "*.png *.jpg *.jpeg *.bmp *.webp")]
BAD_IMAGE_HINT = "Wähl ein PNG-, JPG-, BMP- oder WEBP-Bild."

log = logging.getLogger(__name__)


def _klangbild_cfg(app: "RuckusRadioApp") -> dict | None:
    """F4: loudness_badge()/loudness_summary() need the configured Klangbild targets,
    not always the voice target - Tk only has the core's last snapshot (RoutingService
    publishes them at devices.levels.targets, see routing.py's snapshot())."""
    targets = (((getattr(app, "snapshot", None) or {}).get("devices") or {})
              .get("levels") or {}).get("targets")
    return {"klangbild_targets": targets} if targets else None


SS = 4  # supersampling factor for anti-aliased discs


def circular_ctk_image(path: Path, size: int) -> ctk.CTkImage:
    """Raises icons.IconError if `path` isn't a readable image."""
    cropped = icons.load_circle(path, size * 2)
    return ctk.CTkImage(light_image=cropped, dark_image=cropped, size=(size, size))


def _slot_face(s: int) -> Image.Image:
    """Dashed LINE ring with a small '+' (drawn at supersampled size s)."""
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset, width, dashes = 3 * SS, 2 * SS, 28
    step = 360 / dashes
    for i in range(dashes):
        start = i * step
        draw.arc([inset, inset, s - inset, s - inset], start, start + step * 0.55,
                 fill=theme.LINE, width=width)
    arm, thick, c = s // 9, 2 * SS, s // 2
    draw.rounded_rectangle([c - arm, c - thick, c + arm, c + thick], radius=thick, fill=theme.MUTED)
    draw.rounded_rectangle([c - thick, c - arm, c + thick, c + arm], radius=thick, fill=theme.MUTED)
    return img


def disc_image(face: Image.Image | None, ring_color: str | None, ring_width: int,
               size: int = theme.TILE_SIZE) -> ctk.CTkImage:
    """RAISED disc, optional face (icon) inset by ICON_INSET/2, optional ring on the rim."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, s - 1, s - 1], fill=theme.RAISED)
    if face is not None:
        inner = s - ICON_INSET * SS
        face = face.resize((inner, inner), Image.LANCZOS)
        img.alpha_composite(face, ((s - inner) // 2, (s - inner) // 2))
    if ring_color:
        draw.ellipse([0, 0, s - 1, s - 1], outline=ring_color, width=ring_width * SS)
    img = img.resize((size, size), Image.LANCZOS)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


class Disc(ctk.CTkLabel):
    """Circular clickable disc with idle / hover / playing faces (pre-rendered images)."""

    def __init__(self, master, face: Image.Image | None, command, faces: dict | None = None):
        super().__init__(master, text="", width=theme.TILE_SIZE, height=theme.TILE_SIZE,
                         fg_color="transparent", cursor="hand2")
        self._faces = faces or disc_faces(face)
        self.hover = False
        self.playing = False
        self.state_name = ""
        self._apply()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: command())

    def _set_hover(self, value: bool) -> None:
        self.hover = value
        self._apply()

    def set_playing(self, value: bool) -> None:
        self.playing = value
        self._apply()

    def _apply(self) -> None:
        state = "playing" if self.playing else "hover" if self.hover else "idle"
        if state != self.state_name:
            self.state_name = state
            self.configure(image=self._faces[state])


def disc_faces(face: Image.Image | None) -> dict[str, ctk.CTkImage]:
    return {
        "idle": disc_image(face, None, 0),
        "hover": disc_image(face, theme.ACCENT, 2),
        "playing": disc_image(face, theme.ACCENT, 3),
    }


_slot_faces: dict[str, ctk.CTkImage] = {}
_slot_faces_root = None  # Tk images die with their root: rebuild for a new one


def slot_faces(master) -> dict[str, ctk.CTkImage]:
    global _slot_faces_root
    root = master._root()
    if _slot_faces_root is not root:
        _slot_faces.clear()
        _slot_faces.update(disc_faces(_slot_face((theme.TILE_SIZE - ICON_INSET) * SS)))
        _slot_faces_root = root
    return _slot_faces


class SoundTile(ctk.CTkFrame):
    def __init__(self, master, app: "RuckusRadioApp", sound: dict):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.sound = sound
        self._missing = False

        face_size = (theme.TILE_SIZE - ICON_INSET) * SS
        try:
            face = icons.load_circle(app.data_dir / sound["icon"], face_size)
        except icons.IconError:  # icon file deleted or broken -> initials placeholder
            face = icons.placeholder_icon(sound["name"], face_size)
        self.disc = Disc(self, face, self._on_click)
        self.disc.pack(pady=(0, 8))

        self.name_label = ctk.CTkLabel(
            self, text=ellipsize(sound["name"]), height=18,
            font=theme.body_font(theme.SIZE_TILE_NAME), text_color=theme.TEXT,
        )
        self.name_label.pack()

        keycap = keycap_text(sound.get("hotkey"))
        if keycap:
            self.hotkey_label = ctk.CTkLabel(
                self, text=keycap, height=20, corner_radius=6, fg_color=theme.PANEL,
                font=theme.mono_font(theme.SIZE_KEYCAP), text_color=theme.ACCENT,
            )
        else:
            self.hotkey_label = ctk.CTkLabel(
                self, text="Hotkey setzen", height=20, cursor="hand2",
                font=theme.body_font(theme.SIZE_KEYCAP), text_color=theme.MUTED,
            )
            self.hotkey_label.bind("<Button-1>", self._on_hotkey_label_click)
        self.hotkey_label.pack(pady=(2, 0), ipadx=6)
        self.level_label = ctk.CTkLabel(
            self, text=loudness_badge(sound, _klangbild_cfg(app)), height=14,
            font=theme.body_font(theme.SIZE_KEYCAP), text_color=theme.MUTED,
        )
        self.level_label.pack(pady=(1, 0))
        for widget in (self, self.disc, self.name_label, self.hotkey_label, self.level_label):
            widget.bind("<Button-3>", self._on_right_click, add="+")

        self._hotkey_look = (self.hotkey_label.cget("text"), self.hotkey_label.cget("text_color"))

    def set_playing(self, playing: bool) -> None:
        self.disc.set_playing(playing)

    def refresh_badge(self) -> None:
        """Re-read the Klangbild targets from the app's snapshot (SetKlangbild changes
        them without touching this tile's sound dict)."""
        self.level_label.configure(text=loudness_badge(self.sound, _klangbild_cfg(self.app)))

    def set_missing(self, missing: bool) -> None:
        """Audio file missing/unreadable: MUTED name, 'Datei fehlt' in place of the keycap."""
        if missing == self._missing:
            return  # called on every state update: an unchanged tile must not redraw
        self._missing = missing
        self.name_label.configure(text_color=theme.MUTED if missing else theme.TEXT)
        text, color = ("Datei fehlt", theme.MUTED) if missing else self._hotkey_look
        self.hotkey_label.configure(text=text, text_color=color)

    def _on_click(self):
        self.app.play_sound(self.sound["id"])

    def _on_hotkey_label_click(self, _event):
        """The 'Hotkey setzen' label is clickable only while the sound isn't marked
        'Datei fehlt' — otherwise a click there must not open hotkey capture."""
        if not self._missing:
            self._set_hotkey()

    def _on_right_click(self, event):
        menu = tk.Menu(
            self, tearoff=0, bg=theme.PANEL, fg=theme.TEXT, bd=0,
            activebackground=theme.RAISED, activeforeground=theme.ACCENT,
        )
        menu.add_command(label="Icon ändern…", command=self._change_icon)
        menu.add_command(label="Umbenennen…", command=self._rename)
        menu.add_command(label="Hotkey neu belegen…", command=self._set_hotkey)
        menu.add_command(label="Lautstärke…", command=self._set_volume)
        menu.add_command(label="Exportieren…", command=self._export)
        menu.add_separator()
        menu.add_command(label="Löschen", command=self._delete)
        menu.tk_popup(event.x_root, event.y_root)

    def _change_icon(self):
        path = filedialog.askopenfilename(title="Neues Icon wählen", filetypes=IMAGE_FILETYPES)
        if path:  # the core checks the image and reports a bad one as an error notice
            self.app.change_icon(self.sound, path)

    def _rename(self):
        dialog = ctk.CTkInputDialog(
            text="Neuer Name:", title="Umbenennen",
            fg_color=theme.PANEL, text_color=theme.TEXT,
            button_fg_color=theme.ACCENT, button_hover_color=theme.ACCENT,
            button_text_color=theme.ACCENT_INK,
            entry_fg_color=theme.RAISED, entry_border_color=theme.LINE,
            entry_text_color=theme.TEXT, font=theme.body_font(),
        )
        new_name = dialog.get_input()
        if new_name and new_name.strip():
            self.app.rename_sound(self.sound, new_name)

    def _set_hotkey(self):
        self.app.open_hotkey_capture(self.sound)

    def _set_volume(self):
        self.app.open_volume_dialog(self.sound)

    def _export(self):
        self.app.export_sounds([self.sound["id"]])

    def _delete(self):
        if messagebox.askyesno("Löschen", f"„{self.sound['name']}“ wirklich löschen?"):
            self.app.delete_sound(self.sound["id"])


class PlaceholderSlot(ctk.CTkFrame):
    """Empty slot: click to fill it with a new sound."""

    def __init__(self, master, on_click):
        super().__init__(master, fg_color="transparent")
        self.disc = Disc(self, None, on_click, faces=slot_faces(master))
        self.disc.pack(pady=(0, 8))
        title = ctk.CTkLabel(self, text="Freier Slot", height=18, cursor="hand2",
                             font=theme.body_font(theme.SIZE_TILE_NAME), text_color=theme.MUTED)
        title.pack()
        hint = ctk.CTkLabel(self, text="Klicken zum Füllen", height=20, cursor="hand2",
                            font=theme.body_font(theme.SIZE_KEYCAP), text_color=theme.MUTED)
        hint.pack(pady=(2, 0))
        for label in (title, hint):
            label.bind("<Button-1>", lambda _e: on_click())


def primary_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(
        master, text=text, command=command, corner_radius=10, height=36,
        fg_color=theme.ACCENT, hover_color=theme.ACCENT, text_color=theme.ACCENT_INK,
        font=theme.body_font(theme.SIZE_BODY, "bold"), **kw,
    )


def quiet_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(
        master, text=text, command=command, corner_radius=10, height=36,
        fg_color=theme.RAISED, hover_color=theme.LINE, text_color=theme.TEXT,
        font=theme.body_font(), **kw,
    )


class AddSoundDialog(ctk.CTkToplevel):
    def __init__(self, master, on_saved):
        super().__init__(master)
        self.title("Sound hinzufügen")
        self.geometry("400x420")
        self.configure(fg_color=theme.PANEL)
        self.resizable(False, False)
        self.on_saved = on_saved
        self._audio_path: Path | None = None
        self._icon_path: Path | None = None

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(body, text="Sound hinzufügen", text_color=theme.TEXT,
                     font=theme.display_font(theme.SIZE_SECTION)).pack(anchor="w")

        self._icon_preview = ctk.CTkLabel(
            body, text="Kein Icon", width=72, height=72, corner_radius=36,
            fg_color=theme.RAISED, text_color=theme.MUTED, font=theme.body_font(theme.SIZE_KEYCAP),
        )
        self._icon_preview.pack(pady=(16, 8))

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        row.grid_columnconfigure((0, 1), weight=1, uniform="b")
        quiet_button(row, "Datei wählen…", self._pick_audio).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        quiet_button(row, "Icon wählen…", self._pick_icon).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self._audio_label = ctk.CTkLabel(body, text="Keine Datei gewählt (MP3 oder MP4)",
                                         text_color=theme.MUTED, font=theme.body_font())
        self._audio_label.pack(anchor="w", pady=(10, 0))

        self._name_entry = ctk.CTkEntry(
            body, placeholder_text="Name des Sounds", height=36, corner_radius=10,
            fg_color=theme.RAISED, border_color=theme.LINE, text_color=theme.TEXT,
            placeholder_text_color=theme.MUTED, font=theme.body_font(),
        )
        self._name_entry.pack(fill="x", pady=(12, 0))

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(side="bottom", fill="x")
        primary_button(actions, "Hinzufügen", self._save).pack(side="right")
        quiet_button(actions, "Abbrechen", self.destroy).pack(side="right", padx=(0, 8))

        self.transient(master)
        self.after(50, self.grab_set)

    def _pick_audio(self):
        path = filedialog.askopenfilename(parent=self, title="Sound wählen",
                                          filetypes=filedialogs.SOUND_TYPES)
        if path:
            self._audio_path = Path(path)
            self._audio_label.configure(text=self._audio_path.name, text_color=theme.TEXT)
            if not self._name_entry.get():
                self._name_entry.insert(0, self._audio_path.stem)

    def _pick_icon(self):
        path = filedialog.askopenfilename(parent=self, title="Icon wählen", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        try:
            preview = circular_ctk_image(Path(path), 72)
        except icons.IconError as exc:
            messagebox.showerror("Ruckus Radio", f"{exc} {BAD_IMAGE_HINT}", parent=self)
            return
        self._icon_path = Path(path)
        self._icon_preview.configure(image=preview, text="")
        self._icon_preview.image = preview

    def _save(self):
        if not self._audio_path:
            messagebox.showwarning("Ruckus Radio", "Keine Audiodatei gewählt. Wähle zuerst eine MP3- oder MP4-Datei.",
                                   parent=self)
            return
        name = self._name_entry.get().strip() or self._audio_path.stem
        self.on_saved(name=name, audio_path=self._audio_path, icon_path=self._icon_path)
        self.destroy()


class HotkeyCaptureDialog(ctk.CTkToplevel):
    """Capture a new hotkey for `sound`. While open, the app's global hotkeys are
    paused (suspended in the core) so a combo that matches an existing binding doesn't fire
    a sound while the user is trying to record it — they're re-wired on close,
    whatever the outcome (apply / remove / cancel)."""

    def __init__(self, master, app: "RuckusRadioApp", sound: dict):
        super().__init__(master)
        self.title("Hotkey vergeben")
        self.geometry("380x336")
        self.configure(fg_color=theme.PANEL)
        self.resizable(False, False)
        self.app = app
        self.sound = sound
        self._captured: str | None = None
        self._resumed = False
        self._capture_generation = 0  # bumped on every restart so a stale in-flight
        # capture thread (from before "Nochmal") is ignored when it finally completes

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            body, text=f"Hotkey für „{sound['name']}“", text_color=theme.TEXT,
            font=theme.display_font(theme.SIZE_SECTION),
        ).pack(anchor="w")

        self.status_label = ctk.CTkLabel(
            body, text="", text_color=theme.MUTED, font=theme.body_font(),
            wraplength=340, justify="left", anchor="w",
        )
        self.status_label.pack(fill="x", pady=(18, 10))

        self.keycap_label = ctk.CTkLabel(
            body, text="", height=48, corner_radius=10, fg_color=theme.RAISED,
            text_color=theme.MUTED, font=theme.mono_font(18, "bold"),
        )
        self.keycap_label.pack(fill="x")

        self.error_frame = ctk.CTkFrame(
            body, fg_color=theme.RAISED, corner_radius=8, border_width=1, border_color=theme.LINE,
        )
        self.error_label = ctk.CTkLabel(
            self.error_frame, text="", text_color=theme.TEXT, font=theme.body_font(),
            wraplength=300, justify="left",
        )
        self.error_label.pack(padx=10, pady=8)

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(side="bottom", fill="x", pady=(16, 0))

        primary_row = ctk.CTkFrame(actions, fg_color="transparent")
        primary_row.pack(side="bottom", fill="x")
        self.apply_button = primary_button(primary_row, "Übernehmen", self._apply)
        self.apply_button.configure(state="disabled")
        self.apply_button.pack(side="right")
        quiet_button(primary_row, "Abbrechen", self._cancel).pack(side="right", padx=(0, 8))

        utility_row = ctk.CTkFrame(actions, fg_color="transparent")
        utility_row.pack(side="bottom", fill="x", pady=(0, 8))
        quiet_button(utility_row, "Nochmal", self._restart, width=108).pack(side="left")
        if sound.get("hotkey"):
            quiet_button(utility_row, "Hotkey entfernen", self._remove, width=140).pack(side="left", padx=(8, 0))

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # destroyed from outside (e.g. the assistant closed): hotkeys must not stay dead
        self.bind("<Destroy>", lambda e: e.widget is self and self._resume_hotkeys(), add="+")

        self.transient(master)
        self.after(50, self.grab_set)

        self.app.suspend_hotkeys()
        self._start_capture()

    def _start_capture(self) -> None:
        # bump the generation so a still-running capture thread from before a
        # "Nochmal" restart (or any earlier _start_capture call) is ignored when
        # it eventually completes — see _handle_captured.
        self._capture_generation += 1
        generation = self._capture_generation
        self._captured = None
        self.apply_button.configure(state="disabled")
        self.keycap_label.configure(text="", text_color=theme.MUTED)
        self.error_frame.pack_forget()
        self.status_label.configure(text="Drück jetzt die Tastenkombination…")
        hotkeys.capture_hotkey_async(lambda combo: self._on_captured(generation, combo))

    def _on_captured(self, generation: int, combo: str) -> None:
        # fires on the keyboard-hook thread: no Tcl calls here, hand off via the
        # app's UI queue, and never let an exception escape into that thread
        try:
            self.app.call_in_ui(self._handle_captured, generation, combo)
        except Exception:
            log.exception("hotkey capture callback failed")

    def _handle_captured(self, generation: int, combo: str) -> None:
        if not self.winfo_exists():
            return
        if generation != self._capture_generation:
            return  # stale capture from before a "Nochmal" restart — ignore it
        normalized = hotkeys.normalize_hotkey(combo)
        self.status_label.configure(text="Aufgenommene Kombination:")
        self.keycap_label.configure(text=normalized.upper())
        error = hotkeys.hotkey_error(
            normalized, self.app.sounds(), self.sound["id"],
            (self.app.snapshot.get("settings") or {}).get("stop_all_hotkey"),
        )
        if error:
            self.keycap_label.configure(text_color=theme.MUTED)
            self.error_label.configure(text=error)
            self.error_frame.pack(fill="x", pady=(10, 0))
            self.apply_button.configure(state="disabled")
            return
        self.keycap_label.configure(text_color=theme.ACCENT)
        warning = hotkeys.typing_warning(normalized)
        if warning:  # allowed anyway - the user decides
            self.error_label.configure(text=warning)
            self.error_frame.pack(fill="x", pady=(10, 0))
        else:
            self.error_frame.pack_forget()
        self._captured = normalized
        self.apply_button.configure(state="normal")

    def _restart(self) -> None:
        self._start_capture()

    def _resume_hotkeys(self) -> None:
        if self._resumed:
            return
        self._resumed = True
        self.app.resume_hotkeys()

    def _apply(self) -> None:
        if not self._captured:
            return
        try:
            self.app.set_hotkey(self.sound, self._captured)
        finally:
            self._resume_hotkeys()
        self.destroy()

    def _remove(self) -> None:
        try:
            self.app.remove_hotkey(self.sound)
        finally:
            self._resume_hotkeys()
        self.destroy()

    def _cancel(self) -> None:
        self._resume_hotkeys()
        self.destroy()


class VolumeDialog(ctk.CTkToplevel):
    """Per-sound volume in dB on top of the automatic loudness normalization.
    'Probehören' plays at the slider value without saving; 'Übernehmen' saves
    sound['volume'] (still stored linear, 0-150 %, so packs stay compatible)."""

    MIN_DB = -30.0

    def __init__(self, master, app: "RuckusRadioApp", sound: dict):
        super().__init__(master)
        self.title("Lautstärke")
        self.geometry("440x380")
        self.configure(fg_color=theme.PANEL)
        self.resizable(False, False)
        self.app = app
        self.sound = sound
        self.value = clamp_volume(sound.get("volume", 1.0))
        self.max_db = gain_to_db(MAX_SOUND_VOLUME)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(body, text=f"Lautstärke für „{sound['name']}“", text_color=theme.TEXT,
                     font=theme.display_font(theme.SIZE_SECTION), anchor="w").pack(fill="x")
        ctk.CTkLabel(body, text=loudness_summary(sound, _klangbild_cfg(app)), anchor="w", justify="left",
                     wraplength=400, text_color=theme.MUTED,
                     font=theme.body_font()).pack(fill="x", pady=(2, 0))

        readout = ctk.CTkFrame(body, fg_color=theme.RAISED, corner_radius=12, height=92)
        readout.pack(fill="x", pady=(14, 14))
        readout.pack_propagate(False)
        self.readout_label = ctk.CTkLabel(readout, text="", text_color=theme.ACCENT,
                                          font=theme.mono_font(34, "bold"))
        self.readout_label.pack(pady=(10, 0))
        self.describe_label = ctk.CTkLabel(readout, text="", text_color=theme.MUTED,
                                           font=theme.body_font())
        self.describe_label.pack()

        self.slider = ctk.CTkSlider(
            body, from_=self.MIN_DB, to=self.max_db,
            number_of_steps=int(round((self.max_db - self.MIN_DB) * 2)),
            command=self._on_slide, fg_color=theme.LINE, progress_color=theme.ACCENT,
            button_color=theme.TEXT, button_hover_color=theme.ACCENT,
        )
        self.slider.set(self._db(self.value))
        self.slider.pack(fill="x")

        scale = ctk.CTkFrame(body, fg_color="transparent", height=18)
        scale.pack(fill="x", pady=(4, 0))
        zero = -self.MIN_DB / (self.max_db - self.MIN_DB)
        for text, relx, anchor in ((format_db(self.MIN_DB), 0.0, "nw"), ("0 dB", zero, "n"),
                                   (format_db(self.max_db), 1.0, "ne")):
            ctk.CTkLabel(scale, text=text, height=16, text_color=theme.MUTED,
                         font=theme.mono_font(theme.SIZE_KEYCAP)).place(relx=relx, y=0, anchor=anchor)

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(side="bottom", fill="x")
        primary_button(actions, "Übernehmen", self._apply, width=120).pack(side="right")
        quiet_button(actions, "Abbrechen", self.destroy, width=104).pack(side="right", padx=(0, 8))
        quiet_button(actions, "▶  Probehören", self._preview, width=124).pack(side="left")

        self._show(self.value)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._apply())
        self.transient(master)
        self.after(50, self.grab_set)

    def _db(self, value: float) -> float:
        return min(max(gain_to_db(value), self.MIN_DB), self.max_db)

    def _show(self, value: float) -> None:
        db = self._db(value)
        self.readout_label.configure(text=format_db(db))
        self.describe_label.configure(text=describe_db(db).split(" · ", 1)[1])

    def _on_slide(self, db: float) -> None:
        self.value = round(min(max(db_to_gain(float(db)), 0.0), MAX_SOUND_VOLUME), 4)
        self._show(self.value)

    def _preview(self) -> None:
        """Restart rather than stack: stop any playback of this sound already running
        before starting the new one at the slider's value - in the headphones only."""
        self.app.stop_sound(self.sound["id"])
        self.app.preview_sound(self.sound["id"], self.value)

    def _apply(self) -> None:
        self.app.set_sound_volume(self.sound, self.value)
        self.destroy()
