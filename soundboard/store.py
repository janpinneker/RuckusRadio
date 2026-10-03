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

from . import config, dpapi

log = logging.getLogger(__name__)

SAVE_DEBOUNCE_S = 0.3  # slider drags fire on every pixel; their write waits this long
SECRETS_NAME = "secrets.dat"  # DPAPI-encrypted JSON
LEGACY_SECRETS_NAME = "secrets.json"  # clear text up to 1.4, taken over once


def _only_strings(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        log.warning("the secrets are not an object; starting with none")
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


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
        self.legacy_secrets_path: Path = self.data_dir / LEGACY_SECRETS_NAME
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

    # On disk the file is a Windows DPAPI blob (secrets.dat), bound to the Windows
    # account: another user, or a copy of the file on another machine, cannot read it.
    # Up to 1.4 it was clear text (secrets.json); that file is taken over once and then
    # deleted. If it shows up again, an older build still running wrote it later, so it
    # is the newer one and wins.

    def _load_secrets(self) -> dict[str, str]:
        if self._secrets is None:
            legacy = self._read_legacy_secrets()
            if legacy is not None:
                self._secrets = legacy
                if self._write_secrets():  # only then: a failed write keeps the clear text
                    try:
                        self.legacy_secrets_path.unlink()
                        log.info("secrets.json taken over into the encrypted store")
                    except OSError:
                        log.warning("could not delete the old secrets.json", exc_info=True)
                return self._secrets
            raw = self._read_secret_blob()
            self._secrets = _only_strings(raw)
        return self._secrets

    def _read_legacy_secrets(self) -> dict[str, str] | None:
        try:
            raw = json.loads(self.legacy_secrets_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("the old secrets.json is unreadable; ignoring it", exc_info=True)
            return None
        return _only_strings(raw)

    def _read_secret_blob(self) -> object:
        try:
            blob = self.secrets_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError:
            log.warning("%s is unreadable; starting with none", SECRETS_NAME, exc_info=True)
            return {}
        try:
            return json.loads(dpapi.unprotect(blob).decode("utf-8"))
        except (OSError, ValueError):
            log.warning("%s cannot be decrypted; starting with none", SECRETS_NAME)
            return {}

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

    def _write_secrets(self) -> bool:
        """Atomic like config.json: a half-written file would lock the user out of the
        service. On a failed write the previous file stays in place (False)."""
        tmp = self.secrets_path.with_name(self.secrets_path.name + ".tmp")
        try:
            plain = json.dumps(self._secrets or {}, ensure_ascii=False).encode("utf-8")
            tmp.write_bytes(dpapi.protect(plain))
            os.replace(tmp, self.secrets_path)
        except OSError:
            log.exception("could not write %s; the previous file stays", SECRETS_NAME)
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
