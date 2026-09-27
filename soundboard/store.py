"""Einziger Zugriff auf die gespeicherten Nutzerdaten.

Heute liegt alles in config.json (config.py liest und schreibt atomar). Kommen spaeter
Musik und Playlists dazu (SQLite, Standardbibliothek), passiert das nur hinter dieser
Klasse. Laeuft ausschliesslich auf dem Kern-Thread.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger(__name__)

SAVE_DEBOUNCE_S = 0.3  # slider drags fire on every pixel; their write waits this long
SECRETS_NAME = "secrets.json"


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
        self.secrets_path: Path = self.data_dir / SECRETS_NAME
        self._secrets: dict[str, str] | None = None  # read on first use
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

    # ---- service secrets ----
    # A third-party token (a Spotify refresh token, later others) never belongs in
    # config.json: that file is the user's, it gets copied into bug reports, and it is
    # part of every state snapshot. It lives in its own file, is read on first use, and
    # is never logged - only its name and length are.

    def _load_secrets(self) -> dict[str, str]:
        if self._secrets is None:
            try:
                raw = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = {}
            except (OSError, ValueError):
                log.warning("secrets.json is unreadable; starting with none", exc_info=True)
                raw = {}
            if not isinstance(raw, dict):
                log.warning("secrets.json is not an object; starting with none")
                raw = {}
            self._secrets = {k: v for k, v in raw.items()
                             if isinstance(k, str) and isinstance(v, str)}
        return self._secrets

    def secret(self, name: str) -> str | None:
        return self._load_secrets().get(name)

    def set_secret(self, name: str, value: str) -> None:
        """Writes at once: a lost refresh token is a new login, not a lost setting."""
        self._load_secrets()[name] = value
        self._write_secrets()
        log.info("secret %r stored (%d characters)", name, len(value))

    def forget_secret(self, name: str) -> None:
        if self._load_secrets().pop(name, None) is None:
            return
        self._write_secrets()
        log.info("secret %r removed", name)

    def _write_secrets(self) -> None:
        """Atomic like config.json: a half-written file would lock the user out of the
        service. On a failed write the previous file stays in place."""
        tmp = self.secrets_path.with_name(self.secrets_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._secrets or {}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.secrets_path)
        except OSError:
            log.exception("could not write secrets.json; the previous file stays")

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
