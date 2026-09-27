"""Native Dateidialoge auf der Python-Seite (Spec §9).

Die Oberfläche übergibt nie einen Pfad. Sie bittet um eine Datei, hier öffnet sich der
Dialog, und der Pfad bleibt in Python. Tkinter ist Standardbibliothek und auf diesem
Rechner ohnehin vorhanden (customtkinter); es läuft auf einem eigenen Thread, weil Tk
nur von dem Thread benutzt werden darf, der sein Wurzelfenster erzeugt hat.

Tests setzen eine Attrappe ein, wie `library.LibraryService(core, dialogs=…)`.
"""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger(__name__)

SOUND_TYPES = [("Audio", "*.mp3 *.mp4"), ("Alle Dateien", "*.*")]
ICON_TYPES = [("Bilder", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Alle Dateien", "*.*")]
PACK_TYPES = [("Ruckus-Paket", "*.ruckuspack"), ("Alle Dateien", "*.*")]


class FileDialogs:
    """One hidden Tk root on its own thread, answering one request at a time."""

    def __init__(self, *, timeout: float = 300.0):
        self._timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ask(self, kind: str, **kwargs):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="ruckus-dialogs",
                                                daemon=True)
                self._thread.start()
        box: queue.Queue = queue.Queue()
        self._queue.put((kind, kwargs, box))
        try:
            return box.get(timeout=self._timeout)
        except queue.Empty:
            log.warning("the file dialog did not answer within %.0f s", self._timeout)
            return None

    def _run(self) -> None:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        try:
            while True:
                kind, kwargs, box = self._queue.get()
                try:
                    box.put(self._open(filedialog, kwargs, kind))
                except Exception:  # noqa: BLE001 - a broken dialog must not kill the thread
                    log.exception("file dialog failed")
                    box.put(None)
        finally:
            root.destroy()

    @staticmethod
    def _open(filedialog, kwargs: dict, kind: str):
        if kind == "sound":
            return filedialog.askopenfilename(title="Sound auswählen",
                                              filetypes=SOUND_TYPES) or None
        if kind == "icon":
            return filedialog.askopenfilename(title="Icon auswählen",
                                              filetypes=ICON_TYPES) or None
        if kind == "pack":
            return filedialog.askopenfilename(title="Sound-Paket importieren",
                                              filetypes=PACK_TYPES) or None
        return filedialog.asksaveasfilename(
            title="Sound-Paket speichern",
            initialfile=kwargs.get("default_name") or "sounds.ruckuspack",
            defaultextension=".ruckuspack",
            filetypes=[("Ruckus-Paket", "*.ruckuspack")],
        ) or None

    # ---- the four questions the core asks ----

    def pick_sound_file(self):
        return self._ask("sound")

    def pick_icon_file(self):
        return self._ask("icon")

    def pick_pack_file(self):
        return self._ask("pack")

    def ask_export_target(self, default_name: str):
        return self._ask("target", default_name=default_name)
