"""Anzeige-Bausteine des Hauptfensters: Navigation, Tuner-Banner, Board, Dock und die
Einstellungsseite. Sie zeigen an und melden Klicks an die App (`app`), rechnen aber
nichts Fachliches aus - das liegt im App-Kern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image, ImageDraw

from soundboard import dynamics, levels, theme
from soundboard.layout import filter_sounds, microphone_hint, slots_total
from soundboard.tuner import render_tuner
from soundboard.widgets import PlaceholderSlot, SoundTile, quiet_button

if TYPE_CHECKING:
    from soundboard.gui import RuckusRadioApp

# (key, label, glyph, enabled, kind) — kind "view": add a view in RuckusRadioApp.VIEWS;
# kind "action": add a method name in RuckusRadioApp.ACTIONS (opens something, no view swap)
NAV_ITEMS = [
    ("board", "Soundboard", "▦", True, "view"),
    ("voice", "Stimme", "◉", False, "view"),
    ("record", "Aufnahme", "●", False, "view"),
    ("setup", "Assistent", "✦", True, "action"),
    ("settings", "Einstellungen", "⚙", True, "view"),
]
BOTTOM_NAV = {"setup", "settings"}
BANNER_RADIUS = 14
CONTENT_PAD = 20


class NavRail(ctk.CTkFrame):
    def __init__(self, master, on_select):
        super().__init__(master, width=theme.RAIL_WIDTH, fg_color=theme.PANEL, corner_radius=0)
        self.pack_propagate(False)
        self._indicators: dict[str, ctk.CTkFrame] = {}
        self._glyphs: dict[str, ctk.CTkLabel] = {}

        ctk.CTkLabel(
            self, text="R", width=40, height=40, corner_radius=20, fg_color=theme.RAISED,
            text_color=theme.TEXT, font=theme.display_font(18, "bold"),
        ).pack(pady=(16, 24))

        top = [i for i in NAV_ITEMS if i[0] not in BOTTOM_NAV]
        bottom = [i for i in NAV_ITEMS if i[0] in BOTTOM_NAV]
        for key, label, glyph, enabled, _kind in top + bottom[::-1]:  # bottom packs upward
            item = ctk.CTkFrame(self, fg_color="transparent", height=56, width=theme.RAIL_WIDTH)
            item.pack_propagate(False)
            last = key == bottom[-1][0]
            item.pack(side="bottom" if key in BOTTOM_NAV else "top",
                      pady=(0, 12 if last else 6))
            bar = ctk.CTkFrame(item, width=3, height=36, corner_radius=2, fg_color="transparent")
            bar.place(x=0, rely=0.5, anchor="w")
            color = theme.MUTED
            glyph_label = ctk.CTkLabel(item, text=glyph, height=26, text_color=color,
                                       font=ctk.CTkFont(family="Segoe UI Symbol", size=20))
            glyph_label.pack(pady=(4, 0))
            caption = ctk.CTkLabel(item, text=label if enabled else "bald", height=14,
                                   text_color=color, font=theme.body_font(9 if enabled else theme.SIZE_KEYCAP))
            caption.pack()
            self._indicators[key] = bar
            self._glyphs[key] = glyph_label
            if enabled:
                for w in (item, glyph_label, caption):
                    w.configure(cursor="hand2")
                    w.bind("<Button-1>", lambda _e, k=key: on_select(k))

    def set_active(self, key: str) -> None:
        for k, bar in self._indicators.items():
            active = k == key
            bar.configure(fg_color=theme.ACCENT if active else "transparent")
            self._glyphs[k].configure(text_color=theme.ACCENT if active else theme.MUTED)


class TunerBanner(ctk.CTkFrame):
    def __init__(self, master, height: int = theme.BANNER_HEIGHT, wordmark: bool = True,
                 needle: float = 0.5, radius: int = BANNER_RADIUS):
        super().__init__(master, fg_color=master.cget("fg_color"), height=height, corner_radius=0)
        self.pack_propagate(False)
        self.needle = needle
        self._wordmark = wordmark
        self._radius = radius
        self._pending = None
        self._last_width = 0
        self._label = ctk.CTkLabel(self, text="", fg_color=master.cget("fg_color"))
        self._label.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        if event.width != self._last_width:
            self._last_width = event.width
            self._schedule(120)

    def _schedule(self, delay: int) -> None:
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(delay, self.redraw)

    def set_needle(self, value: float) -> None:
        self.needle = min(max(float(value), 0.0), 1.0)
        self._schedule(0)

    def redraw(self):
        self._pending = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        scale = self._get_widget_scaling()
        img = render_tuner(w, h, self.needle, wordmark=self._wordmark).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=round(self._radius * scale), fill=255)
        img.putalpha(mask)
        image = ctk.CTkImage(light_image=img, dark_image=img, size=(w / scale, h / scale))
        self._label.configure(image=image)
        self._label.image = image


class BoardView(ctk.CTkFrame):
    def __init__(self, master, app: "RuckusRadioApp"):
        super().__init__(master, fg_color=theme.BG, corner_radius=0)
        self.app = app
        self.tiles: dict[str, SoundTile] = {}
        self.slots: list[PlaceholderSlot] = []

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(16, 8))
        self.search = ctk.CTkEntry(
            bar, placeholder_text="Sounds durchsuchen…",
            width=300, height=36, corner_radius=10, border_width=1,
            fg_color=theme.RAISED, border_color=theme.LINE, text_color=theme.TEXT,
            placeholder_text_color=theme.MUTED, font=theme.body_font(),
        )
        self.search.pack(side="left")
        # textvariable would disable CTkEntry's placeholder, so watch keystrokes instead
        self.search.bind("<KeyRelease>", lambda _e: self.apply_filter())
        for event in ("<<Paste>>", "<<Cut>>"):
            self.search.bind(event, lambda _e: self.after_idle(self.apply_filter), add="+")
        quiet_button(bar, "Exportieren…", lambda: app.export_sounds(None)).pack(side="right")
        quiet_button(bar, "Importieren…", app.import_pack).pack(side="right", padx=(0, 8))

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=theme.BG, corner_radius=0,
            scrollbar_button_color=theme.RAISED, scrollbar_button_hover_color=theme.LINE,
        )
        self.scroll.pack(fill="both", expand=True)
        self.scroll.grid_columnconfigure(tuple(range(theme.COLUMNS)), weight=1, uniform="col")
        self.empty_label = ctk.CTkLabel(self.scroll, text="", text_color=theme.MUTED, font=theme.body_font())
        self._shown_query: str | None = None

    def rebuild(self):
        for widget in list(self.tiles.values()) + self.slots:
            widget.destroy()
        self.tiles = {s["id"]: SoundTile(self.scroll, self.app, s) for s in self.app.sounds()}
        n = len(self.tiles)
        self.slots = [PlaceholderSlot(self.scroll, self.app.open_add_dialog) for _ in range(slots_total(n) - n)]
        self._shown_query = None
        self.apply_filter()

    def apply_filter(self):
        query = self.search.get()
        if query == self._shown_query:
            return  # e.g. arrow keys: nothing changed, skip regridding
        self._shown_query = query
        sounds = self.app.sounds()
        matches = filter_sounds(sounds, query)
        searching = bool(query.strip())
        for widget in list(self.tiles.values()) + self.slots:
            widget.grid_forget()
        self.empty_label.grid_forget()

        visible = [self.tiles[s["id"]] for s in matches] + ([] if searching else self.slots)
        for i, widget in enumerate(visible):
            row, col = divmod(i, theme.COLUMNS)
            widget.grid(row=row, column=col, pady=(8, 16))
        if searching and not matches:
            self.empty_label.configure(text=f"Kein Sound passt zu „{query.strip()}“.")
            self.empty_label.grid(row=0, column=0, columnspan=theme.COLUMNS, pady=48)

    def set_query(self, text: str) -> None:
        self.search.delete(0, "end")
        if text:
            self.search.insert(0, text)
        self.apply_filter()

    def clear_search(self):
        self.set_query("")
        self.app.focus_set()


class Dock(ctk.CTkFrame):
    def __init__(self, master, app: "RuckusRadioApp"):
        super().__init__(master, height=theme.DOCK_HEIGHT, fg_color=theme.PANEL, corner_radius=0)
        self.pack_propagate(False)
        self.app = app

        status = ctk.CTkFrame(self, fg_color="transparent")
        status.pack(side="left", padx=(20, 0))
        self.dot = ctk.CTkFrame(status, width=8, height=8, corner_radius=4, fg_color=theme.MUTED)
        self.dot.pack(side="left", padx=(0, 8))
        self.status_label = ctk.CTkLabel(status, text="", text_color=theme.MUTED, font=theme.body_font())
        self.status_label.pack(side="left")
        self.hint_label = ctk.CTkLabel(status, text="", text_color=theme.MUTED, font=theme.body_font())
        self.hint_label.pack(side="left", padx=(16, 0))
        self._hint_job = None

        self.stop_button = ctk.CTkButton(
            self, text="ALLE STOPPEN", command=app.stop_all, height=38, corner_radius=12,
            fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER, text_color=theme.ACCENT_INK,
            font=theme.display_font(theme.SIZE_SECTION, "bold"),
        )
        self.stop_button.pack(side="right", padx=(0, 16))

        # Virtual-mic controls, left of the stop button: everything needed to find out
        # why the voice chat stays silent, without leaving the window.
        self.mic_button = quiet_button(self, "Mikro an", app.toggle_mic, width=96)
        self._mic_shown = False
        self.check_button = quiet_button(self, "Prüfen", app.check_signal, width=86)
        self.check_button.pack(side="right", padx=(0, 12))
        self.discord_button = quiet_button(self, "Discord: Sounds an", app.toggle_discord_sounds,
                                           width=150)
        self.discord_button.pack(side="right", padx=(0, 10))

    def show_hint(self, text: str, ms: int = 5000) -> None:
        if self._hint_job:
            self.after_cancel(self._hint_job)
        self.hint_label.configure(text=f"·  {text}" if text else "")
        self._hint_job = self.after(ms, self._clear_hint) if text else None

    def _clear_hint(self) -> None:
        self._hint_job = None
        self.hint_label.configure(text="")

    TONE_COLORS = {"ok": (theme.ACCENT, theme.TEXT),
                   "warn": (theme.DANGER, theme.TEXT),
                   "off": (theme.MUTED, theme.MUTED)}

    def set_status(self, text: str, tone: str = "off") -> None:
        dot_color, text_color = self.TONE_COLORS.get(tone, self.TONE_COLORS["off"])
        self.dot.configure(fg_color=dot_color)
        self.status_label.configure(text=text, text_color=text_color)

    def set_discord_button(self, sounds_on: bool | None) -> None:
        """None: no Discord cable found - the button then only explains that."""
        muted = sounds_on is False
        self.discord_button.configure(
            text="Discord: Sounds aus" if muted else "Discord: Sounds an",
            text_color=theme.MUTED if muted else theme.TEXT)

    def set_mic_button(self, visible: bool, muted: bool) -> None:
        """The mute toggle only exists while Ruckus Radio itself carries the microphone.
        `_mic_shown` tracks that instead of winfo_ismapped(), which is also False while
        the window is minimised and would re-pack the button on every refresh."""
        if not visible:
            if self._mic_shown:
                self.mic_button.pack_forget()
                self._mic_shown = False
            return
        self.mic_button.configure(text="Mikro aus" if muted else "Mikro an",
                                  text_color=theme.MUTED if muted else theme.TEXT)
        if not self._mic_shown:
            self.mic_button.pack(side="right", padx=(0, 12), before=self.check_button)
            self._mic_shown = True


def _db_slider(parent, low: float, high: float, value: float, on_change, describe):
    """Slider in half-dB steps plus a label with the number and an everyday comparison
    (levels.describe_db), so nobody has to know what -10 dB means."""
    label = ctk.CTkLabel(parent, text=describe(value), width=250, anchor="w",
                         text_color=theme.MUTED, font=theme.body_font(theme.SIZE_KEYCAP))
    label.pack(side="right")

    def changed(raw: float) -> None:
        db = round(float(raw) * 2.0) / 2.0
        label.configure(text=describe(db))
        on_change(db)

    slider = ctk.CTkSlider(
        parent, from_=low, to=high, number_of_steps=int(round((high - low) * 2)), width=220,
        progress_color=theme.ACCENT, button_color=theme.ACCENT, fg_color=theme.LINE,
        button_hover_color=theme.TEXT, command=changed,
    )
    slider.set(value)
    slider.pack(side="right", padx=(0, 10))
    return slider, label


class OutputRow(ctk.CTkFrame):
    """Eine Zeile der Ausgangs-Matrix: ein Ziel, zwei Schalter mit je einem Regler."""

    def __init__(self, master, app: "RuckusRadioApp", row: dict):
        super().__init__(master, fg_color=theme.RAISED, corner_radius=10)
        self.app = app
        self.key = row["key"]

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text=row["label"], text_color=theme.TEXT,
                     font=theme.display_font(theme.SIZE_SECTION)).pack(side="left")
        ctk.CTkLabel(head, text=row["subtitle"], text_color=theme.MUTED,
                     font=theme.body_font(theme.SIZE_KEYCAP)).pack(side="left", padx=(10, 0))

        for field, label in (("mic", "Mikro"), ("sounds", "Sounds")):
            self._add_control(field, label, row["settings"])

    def _add_control(self, field: str, label: str, settings: dict) -> None:
        line = ctk.CTkFrame(self, fg_color="transparent")
        line.pack(fill="x", padx=14, pady=(0, 10))

        state = ctk.BooleanVar(value=settings[field])
        switch = ctk.CTkSwitch(
            line, text=label, variable=state, width=110,
            text_color=theme.TEXT, font=theme.body_font(),
            progress_color=theme.ACCENT, button_color=theme.TEXT,
            command=lambda: self.app.set_output(self.key, **{field: state.get()}),
        )
        switch.pack(side="left")

        gain_field = f"{field}_gain"
        low, high = levels.ROW_RANGE_DB
        start_db = min(max(dynamics.gain_to_db(settings[gain_field]), low), high)
        _db_slider(
            line, low, high, start_db,
            lambda db, f=gain_field: self.app.set_output(self.key, **{f: dynamics.db_to_gain(db)}),
            levels.describe_db,
        )


class MixerPanel(ctk.CTkFrame):
    """Mikrofonwahl, Abstand der Sounds zur Stimme und Ducking - gilt fuer alle Kabel.
    Die Kopfhoerer-Lautstaerke steht in ihrer eigenen Zeile unter "Ausgänge"."""

    def __init__(self, master, app: "RuckusRadioApp", devices: dict):
        super().__init__(master, fg_color=theme.RAISED, corner_radius=10)
        self.app = app

        names = devices.get("microphones") or []
        current = devices.get("mic_name") or (app.snapshot.get("settings") or {}).get("microphone_name") or ""
        self._names = names
        mic_line = self._line("Mikrofon")
        values = names or ["Kein Mikrofon gefunden"]
        self.menu = menu = ctk.CTkOptionMenu(
            mic_line, values=values, width=340, command=self._pick_mic,
            fg_color=theme.PANEL, button_color=theme.LINE, button_hover_color=theme.ACCENT,
            text_color=theme.TEXT, font=theme.body_font())
        menu.set(current if current in names else values[0])
        menu.pack(side="left")
        ctk.CTkLabel(self, text=microphone_hint(names, current), anchor="w", justify="left",
                     text_color=theme.MUTED, font=theme.body_font(theme.SIZE_KEYCAP)).pack(
            fill="x", padx=14, pady=(0, 10))

        offset_line = self._line("Sounds unter Stimme")
        lv = devices.get("levels") or {}
        _db_slider(offset_line, *levels.OFFSET_RANGE_DB,
                   lv.get("sounds_offset_db", levels.DEFAULT_OFFSET_DB),
                   lambda db: self.app.set_levels(sounds_offset_db=db), levels.describe_offset)

        enabled = lv.get("ducking_enabled", True)
        depth = lv.get("ducking_db", levels.DEFAULT_DUCKING_DB)
        duck_line = self._line("")
        state = ctk.BooleanVar(value=enabled)
        ctk.CTkSwitch(
            duck_line, text="Ducking", variable=state, width=170,
            text_color=theme.TEXT, font=theme.body_font(),
            progress_color=theme.ACCENT, button_color=theme.TEXT,
            command=lambda: self.app.set_levels(ducking_enabled=state.get()),
        ).pack(side="left")
        _db_slider(duck_line, *levels.DUCKING_RANGE_DB, depth,
                   lambda db: self.app.set_levels(ducking_db=db),
                   lambda db: f"{levels.describe_db(db)}, während du sprichst")

    def _line(self, title: str) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self, fg_color="transparent")
        line.pack(fill="x", padx=14, pady=(12, 6))
        if title:
            ctk.CTkLabel(line, text=title, width=170, anchor="w", text_color=theme.TEXT,
                         font=theme.body_font()).pack(side="left")
        return line

    def sync(self, devices: dict) -> None:
        """Dropdown back to the microphone the core really uses (e.g. after MIC_BUSY)."""
        current = devices.get("mic_name") or (self.app.snapshot.get("settings") or {}).get("microphone_name") or ""
        if current in self._names:
            self.menu.set(current)

    def _pick_mic(self, name: str) -> None:
        if name != "Kein Mikrofon gefunden":
            self.app.set_microphone(name)


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, app: "RuckusRadioApp"):
        super().__init__(master, fg_color=theme.BG, corner_radius=0)
        self.app = app

        ctk.CTkLabel(self, text="Mischpult", text_color=theme.TEXT,
                     font=theme.display_font(theme.SIZE_SECTION)).pack(anchor="w", pady=(16, 2))
        ctk.CTkLabel(
            self, text="Filter und Angleichen wirken nur auf deine Stimme. Sounds werden nur "
                       "in der Lautstärke angepasst, nie im Klang.",
            text_color=theme.MUTED, font=theme.body_font(), justify="left").pack(
            anchor="w", pady=(0, 8))
        self.mixer_host = ctk.CTkFrame(self, fg_color="transparent")
        self.mixer_host.pack(fill="x")

        ctk.CTkLabel(self, text="Ausgänge", text_color=theme.TEXT,
                     font=theme.display_font(theme.SIZE_SECTION)).pack(
            anchor="w", pady=(16, 2))
        ctk.CTkLabel(
            self, text="Bestimme je Ziel, was dort ankommt. Der Name unter jedem Ziel ist "
                       "das Gerät, das du im Sprachchat als Mikrofon auswählst.",
            text_color=theme.MUTED, font=theme.body_font(), justify="left").pack(
            anchor="w", pady=(0, 12))

        updates_row = ctk.CTkFrame(self, fg_color="transparent")
        updates_row.pack(side="bottom", fill="x", pady=(12, 12))
        self.update_label = ctk.CTkLabel(updates_row, text="", text_color=theme.MUTED,
                                         font=theme.body_font())
        self.update_label.pack(side="left")
        self.update_button = quiet_button(updates_row, "Nach Updates suchen",
                                          app.check_updates, width=170)
        self.update_button.pack(side="left", padx=(12, 0))
        self.sync_updates(app.snapshot.get("updates") or {})

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=theme.BG, corner_radius=0,
            scrollbar_button_color=theme.RAISED, scrollbar_button_hover_color=theme.LINE)
        self.scroll.pack(fill="both", expand=True)
        self.refresh()

    UPDATE_BUSY = {"checking": "sucht …", "downloading": "lädt …"}

    def sync_updates(self, updates: dict) -> None:
        current = updates.get("current")
        self.update_label.configure(text=f"Version {current}" if current else "")
        busy = self.UPDATE_BUSY.get(updates.get("status"))
        self.update_button.configure(text=busy or "Nach Updates suchen",
                                     state="disabled" if busy else "normal")

    def refresh(self) -> None:
        # destroy(), not pack_forget(): every device change calls this, and pack_forget()
        # alone would leak an OutputRow (and its switches/sliders) per refresh instead of
        # freeing it - same pattern as BoardView.rebuild().
        devices = self.app.snapshot.get("devices") or {}
        for child in self.mixer_host.winfo_children():
            child.destroy()
        self.mixer = MixerPanel(self.mixer_host, self.app, devices)
        self.mixer.pack(fill="x")
        for child in self.scroll.winfo_children():
            child.destroy()
        rows = devices.get("output_rows") or []
        if not rows:
            ctk.CTkLabel(
                self.scroll,
                text="Kein virtuelles Mikrofon gefunden. Installiere VB-CABLE und starte neu.",
                text_color=theme.MUTED, font=theme.body_font()).pack(pady=24)
            return
        for row in rows:
            OutputRow(self.scroll, self.app, row).pack(fill="x", pady=(0, 10))

    def sync(self) -> None:
        self.mixer.sync(self.app.snapshot.get("devices") or {})
