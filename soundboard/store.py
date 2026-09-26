"""Einziger Zugriff auf die gespeicherten Nutzerdaten.

Heute liegt alles in config.json (config.py liest und schreibt atomar). Kommen spaeter
Musik und Playlists dazu (SQLite, Standardbibliothek), passiert das nur hinter dieser
Klasse. Laeuft ausschliesslich auf dem Kern-Thread.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger(__name__)

SAVE_DEBOUNCE_S = 0.3  # slider drags fire on every pixel; their write waits this long


class Store:
    def __init__(self, scheduler, data: dict | None = None):
        self._scheduler = scheduler
        if data is None:
            self.data = config.load_config()
            self.was_reset = config.last_load_was_reset
        else:
            self.data = data
            self.was_reset = False
        self.data_dir: Path = config.get_app_data_dir()
        self.on_save_failed: Callable[[OSError], None] | None = None
        self._pending = None

    def save_now(self) -> bool:
        """Write at once (cancelling a pending debounced write). False when the disk
        refused; config.save_config leaves the previous file intact then."""
        self._cancel_pending()
        try:
            config.save_config(self.data)
        except OSError as exc:
            log.warning("saving config.json failed", exc_info=True)
            if self.on_save_failed is not None:
                self.on_save_failed(exc)
            return False
        return True

    def save_soon(self) -> None:
        self._cancel_pending()
        self._pending = self._scheduler.call_later(SAVE_DEBOUNCE_S, self._write_pending)

    def flush(self) -> None:
        """Write a pending debounced change now; called on shutdown."""
        if self._pending is not None:
            self.save_now()

    def backup(self, tag: str) -> Path | None:
        """Copy config.json aside before a migration: config.json.<tag>-<unix time>.bak."""
        source = config.config_path()
        if not source.exists():
            return None
        dest = source.with_name(f"config.json.{tag}-{int(time.time())}.bak")
        shutil.copy2(source, dest)
        return dest

    def _write_pending(self) -> None:
        self._pending = None
        self.save_now()

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
