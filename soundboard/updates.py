"""Updates ueber GitHub Releases: pruefen, laden, Pruefsumme. Installieren (Installer
starten, beenden) macht die Oberflaeche nach UpdateReady - ueber `launch_installer`,
das sowohl das Tk-Fenster als auch der Web-Host (webmain.py) aufrufen.

Thread-Regeln wie ueberall im Kern: Netz und Dateien nur auf Worker-Threads, Zustand nur
auf dem Kern-Thread. Kein automatisches Pruefen - nur auf CheckForUpdates."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .protocol import CheckForUpdates, InstallUpdate, UpdateAvailable, UpdateReady
from .version import __version__

log = logging.getLogger(__name__)

REPO = "janpinneker/RuckusRadio"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALLER_ASSET = "RuckusRadioSetup.exe"
CHECKSUM_ASSET = INSTALLER_ASSET + ".sha256"
TIMEOUT_S = 10
BLOCK_BYTES = 256 * 1024
NOTES_MAX = 1500
PROGRESS_INTERVAL_S = 0.1

UPDATE_CHECKING = "Suche nach Updates …"
UPDATE_CURRENT = "Ruckus Radio ist aktuell ({version})."
UPDATE_QUESTION = ("Version {version} ist da – jetzt installieren?\n\n"
                   "Ruckus Radio schließt sich dafür kurz und startet danach neu.")
UPDATE_DOWNLOADING = "Update wird geladen … {percent} %"
UPDATE_NO_NETWORK = ("Keine Verbindung zu GitHub. Prüf die Internetverbindung und versuch "
                     "es noch einmal.")
UPDATE_BROKEN = ("Das Update ist beschädigt angekommen und wurde verworfen. Versuch es "
                 "später noch einmal.")
UPDATE_INCOMPLETE = ("Auf GitHub fehlt die Installationsdatei für diese Version. Versuch es "
                     "später noch einmal.")
UPDATE_LAUNCH_FAILED = "Das Update konnte nicht gestartet werden. Installationsdatei: {path}"
UPDATE_DONE = "Ruckus Radio wurde auf {version} aktualisiert."
#: Shown once after a silent in-app update to that version (the installer passes --updated).
WHATS_NEW = {
    "1.2.0": ("Neu: die Web-Oberfläche ist jetzt Standard. Die gewohnte Oberfläche startest "
              "du über „Ruckus Radio (klassisch)“ im Startmenü."),
}


def update_notice(version: str) -> str:
    news = WHATS_NEW.get(version)
    done = UPDATE_DONE.format(version=version)
    return f"{done} {news}" if news else done
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


def launch_installer(path: str) -> bool:
    """Start the verified installer detached, with a clean env and an install log next
    to it. Shared by the Tk window and the web host (webmain.py) - both quit right
    after this succeeds; the caller decides how. Returns False (and logs) when the OS
    refuses to start it."""
    log_path = Path(path).with_name("install.log")
    # No quotes inside /LOG=: Popen quotes the whole argument when needed, while an inner
    # quote would reach Inno as \" - it then cannot create the log and aborts the setup.
    try:
        subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE",
                          f"/LOG={log_path}"],
                         close_fds=True, creationflags=DETACHED_FLAGS, env=clean_child_env())
    except OSError:
        log.exception("starting the update installer failed")
        return False
    return True


class UpdateError(Exception):
    text = UPDATE_NO_NETWORK


class NetworkError(UpdateError):
    text = UPDATE_NO_NETWORK


class IncompleteRelease(UpdateError):
    text = UPDATE_INCOMPLETE


class BrokenDownload(UpdateError):
    text = UPDATE_BROKEN


class UpdateCancelled(Exception):
    """Ruckus is shutting down: stop quietly, leave nothing behind."""


def parse_version(text) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def is_newer(candidate, current) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and cur is not None and new > cur


def release_info(data: dict) -> dict:
    """The parts of a GitHub 'latest release' answer Ruckus needs."""
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or []}
    installer_url, checksum_url = assets.get(INSTALLER_ASSET), assets.get(CHECKSUM_ASSET)
    if not installer_url or not checksum_url:
        raise IncompleteRelease(f"release {data.get('tag_name')!r} lacks its assets")
    if not installer_url.startswith("https://") or not checksum_url.startswith("https://"):
        raise IncompleteRelease(f"release {data.get('tag_name')!r} has non-https assets")
    tag = parse_version(data.get("tag_name"))
    return {"version": ".".join(map(str, tag)) if tag else None,
            "notes": (data.get("body") or "")[:NOTES_MAX],
            "installer_url": installer_url,
            "checksum_url": checksum_url}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


class GitHubReleases:
    """The real ReleaseSource: GitHub's REST API over HTTPS (certificates checked by urllib)."""

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(url, headers={
            "User-Agent": f"RuckusRadio/{__version__}",
            "Accept": "application/vnd.github+json"})

    def latest(self) -> dict:
        try:
            with urllib.request.urlopen(self._request(API_LATEST), timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        # URLError/HTTPError/timeouts are OSErrors; bad JSON is ValueError; a truncated
        # or malformed HTTP response from http.client is HTTPException.
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise NetworkError(str(exc)) from exc

    def download(self, url: str, dest: Path, on_progress, should_stop) -> None:
        try:
            with urllib.request.urlopen(self._request(url), timeout=TIMEOUT_S) as resp, \
                    open(dest, "wb") as out:
                # bad/non-numeric Content-Length -> ValueError; unsupported url scheme
                # (e.g. a tampered file:// url) -> ValueError from urlopen itself.
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if should_stop():
                        raise UpdateCancelled()
                    block = resp.read(BLOCK_BYTES)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    on_progress(done, total)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise NetworkError(str(exc)) from exc


class UpdateService:
    def __init__(self, core, source=None, download_dir=None):
        self._core = core
        self._source = source if source is not None else GitHubReleases()
        self.download_dir = Path(download_dir) if download_dir else \
            Path(tempfile.gettempdir()) / "RuckusRadio-update"
        self.status = "idle"
        self.latest: str | None = None
        self.notes: str | None = None
        self.progress: float | None = None
        self._release: dict | None = None
        self._stop = threading.Event()
        self._last_progress = 0.0
        core.handle(CheckForUpdates, self.check)
        core.handle(InstallUpdate, self.install)
        core.add_state("updates", self.snapshot)
        core.before_shutdown(self._stop.set)

    def snapshot(self) -> dict:
        return {"current": __version__, "status": self.status, "latest": self.latest,
                "notes": self.notes, "progress": self.progress}

    # ---- check: core -> worker -> core ----

    def check(self, _cmd: CheckForUpdates | None = None) -> None:
        if self.status in ("checking", "downloading"):
            return
        self.status = "checking"
        self._core.notice(UPDATE_CHECKING, "hint")
        self._core.state_changed()
        self._core.workers.submit(self._check_worker)

    def _check_worker(self) -> None:  # worker
        try:
            info, error = release_info(self._source.latest()), None
        except UpdateError as exc:
            info, error = None, exc
        except Exception as exc:  # an unexpected error must not leave the button stuck
            log.exception("checking for updates failed unexpectedly")
            info, error = None, NetworkError(str(exc))
        self._core.executor.submit(self._checked, info, error)

    def _checked(self, info: dict | None, error: UpdateError | None) -> None:  # core
        if error is not None:
            self._failed(error)
            return
        self.latest = info["version"]
        if is_newer(info["version"], __version__):
            self.status = "available"
            self.notes = info["notes"]
            self._release = info
            self._core.state_changed()
            self._core.emit(UpdateAvailable(info["version"], info["notes"]))
            return
        self.status = "current"
        self._core.state_changed()
        self._core.notice(UPDATE_CURRENT.format(version=__version__), "info")

    # ---- install: core -> worker -> core ----

    def install(self, _cmd: InstallUpdate | None = None) -> None:
        if self.status != "available" or self._release is None:
            return
        self.status = "downloading"
        self.progress = 0.0
        self._core.state_changed()
        self._core.workers.submit(self._install_worker, dict(self._release))

    def _install_worker(self, release: dict) -> None:  # worker
        dest = self.download_dir / f"RuckusRadioSetup-{release['version']}.exe"
        sums = self.download_dir / (dest.name + ".sha256")
        success = False
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            # a worker killed at shutdown (before_shutdown only sets the stop flag, the
            # process may still exit before it unwinds) can leave a part file behind.
            for leftover in self.download_dir.glob("RuckusRadioSetup-*.exe"):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:  # still locked (old installer running): not our problem now
                    log.warning("could not remove old update file %s", leftover)
            self._source.download(release["checksum_url"], sums, lambda _d, _t: None,
                                  self._stop.is_set)
            parts = sums.read_text(encoding="utf-8", errors="replace").split()
            if not parts:
                raise BrokenDownload("empty checksum file")
            expected = parts[0].strip().lower()
            self._source.download(release["installer_url"], dest, self._on_progress,
                                  self._stop.is_set)
            if sha256_of(dest) != expected:
                raise BrokenDownload("checksum mismatch")
            success = True
        except UpdateCancelled:
            return
        except UpdateError as exc:
            self._core.executor.submit(self._failed, exc)
            return
        except OSError as exc:  # disk full, temp dir not writable
            log.warning("saving the update failed", exc_info=True)
            self._core.executor.submit(self._failed, BrokenDownload(str(exc)))
            return
        except Exception as exc:  # an unexpected error must not leave the button stuck
            log.exception("installing the update failed unexpectedly")
            self._core.executor.submit(self._failed, BrokenDownload(str(exc)))
            return
        finally:
            # every non-success path leaves no trace: the part file is always removed.
            sums.unlink(missing_ok=True)
            if not success:
                dest.unlink(missing_ok=True)
        self._core.executor.submit(self._ready, str(dest))

    def _on_progress(self, done: int, total: int) -> None:  # worker
        now = time.monotonic()
        if (not total or done < total) and now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        self._core.executor.submit(self._set_progress, done / total if total else None)

    def _set_progress(self, fraction: float | None) -> None:  # core
        if self.status != "downloading" or fraction is None:
            return
        self.progress = fraction
        self._core.notice(UPDATE_DOWNLOADING.format(percent=int(fraction * 100)), "hint")
        self._core.state_changed()

    def _ready(self, path: str) -> None:  # core
        self.status = "ready"
        self.progress = 1.0
        self._core.state_changed()
        self._core.emit(UpdateReady(path))

    def _failed(self, error: UpdateError) -> None:  # core
        log.warning("update failed: %s", error)
        self.status = "error"
        self.progress = None
        self._core.state_changed()
        self._core.notice(error.text, "error")
