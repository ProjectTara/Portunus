"""Signed self-update from GitHub Releases.

Each release carries three assets:

    Portunus-win64.zip           the app folder
    portunus-manifest.json       {"version", "file", "size", "sha256"}
    portunus-manifest.json.sig   Ed25519 signature of the manifest bytes

The public key is compiled into the app (update_key.py). An update is only
installed when the manifest signature verifies, its version is strictly
newer than the running one (no downgrade), and the zip matches the signed
size and SHA-256. A GitHub account takeover alone cannot ship an update
without the private signing key.

Installing: the new build is unpacked into a staging folder, then the new
Portunus.exe is started with ``--apply-update``. It waits for this process to
exit, renames the install folder aside, copies the new build in, restarts
the app, and rolls back if the copy fails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from .update_key import PUBLIC_KEY

ZIP_NAME = "Portunus-win64.zip"
MANIFEST_NAME = "portunus-manifest.json"
SIG_NAME = MANIFEST_NAME + ".sig"
EXE_NAME = "Portunus.exe"
MAX_ZIP = 400 * 1024 * 1024
MAX_SMALL = 256 * 1024
ALLOWED_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    """A failure with a message that is safe to show the user."""


# ---------------------------------------------------------------- versions
def parse_version(text: str) -> tuple[int, int, int]:
    parts = text.strip().lstrip("vV").split(".")
    if len(parts) != 3 or not all(p.isdigit() and len(p) <= 6 for p in parts):
        raise UpdateError(f"Versi tidak sah: {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def repository() -> Optional[str]:
    """owner/name of the GitHub repo, written into the build by CI."""
    try:
        from ._build_info import REPOSITORY  # type: ignore[import-not-found]
    except ImportError:
        return None
    if REPOSITORY.count("/") != 1 or not all(c.isalnum() or c in "-_./" for c in REPOSITORY):
        return None
    return REPOSITORY


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def updates_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
    return base / APP_NAME / "updates"


def support() -> tuple[bool, str]:
    if not getattr(sys, "frozen", False):
        return False, "Pembaruan otomatis hanya untuk versi .exe. Dari kode sumber, pakai git pull."
    if not PUBLIC_KEY:
        return False, "Build ini tidak punya kunci verifikasi, jadi pembaruan otomatis dimatikan."
    if not repository():
        return False, "Build ini tidak tahu alamat repo GitHub-nya."
    probe = install_dir() / ".write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False, (
            "Folder Portunus tidak bisa ditulis (misalnya di Program Files). "
            "Pindahkan ke %LOCALAPPDATA%\\Programs\\Portunus agar bisa diperbarui otomatis."
        )
    return True, ""


# ---------------------------------------------------------------- network
class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect())


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError("Alamat unduhan tidak dikenal, pembaruan dibatalkan.")


def _get(url: str, limit: int, accept: str = "application/octet-stream") -> bytes:
    _check_url(url)
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP_NAME}/{__version__}"})
    try:
        with _opener.open(request, timeout=30) as response:
            data = response.read(limit + 1)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("Tidak bisa menghubungi GitHub. Periksa koneksi internet.") from exc
    if len(data) > limit:
        raise UpdateError("File pembaruan lebih besar dari yang diizinkan.")
    return data


# ---------------------------------------------------------------- manifest
@dataclass
class Release:
    version: str
    size: int
    sha256: str
    zip_url: str


def verify_manifest(raw: bytes, signature: bytes, public_key_b64: str = "") -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_b64 = public_key_b64 or PUBLIC_KEY
    if not key_b64:
        raise UpdateError("Kunci verifikasi tidak ada.")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
        key.verify(signature, raw)
    except (InvalidSignature, ValueError) as exc:
        raise UpdateError("Tanda tangan pembaruan tidak sah. Pembaruan ditolak.") from exc
    try:
        manifest = json.loads(raw)
        parse_version(manifest["version"])
        if manifest["file"] != ZIP_NAME:
            raise ValueError
        if not (0 < int(manifest["size"]) <= MAX_ZIP):
            raise ValueError
        if len(manifest["sha256"]) != 64 or not all(c in "0123456789abcdef" for c in manifest["sha256"]):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("Isi manifest pembaruan rusak.") from exc
    return manifest


def check(current: str = __version__, public_key_b64: str = "") -> Optional[Release]:
    """Return the newer signed release, or None if already up to date."""
    repo = repository()
    if not repo:
        raise UpdateError("Repo GitHub tidak diketahui.")
    raw = _get(f"https://api.github.com/repos/{repo}/releases/latest", MAX_SMALL, "application/vnd.github+json")
    try:
        assets = {a["name"]: a["browser_download_url"] for a in json.loads(raw).get("assets", [])}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError("Jawaban GitHub tidak bisa dibaca.") from exc
    if not {ZIP_NAME, MANIFEST_NAME, SIG_NAME} <= assets.keys():
        raise UpdateError("Rilis terbaru belum lengkap. Coba lagi beberapa menit lagi.")
    manifest_raw = _get(assets[MANIFEST_NAME], MAX_SMALL)
    signature = _get(assets[SIG_NAME], 1024)
    manifest = verify_manifest(manifest_raw, signature, public_key_b64)
    if parse_version(manifest["version"]) <= parse_version(current):
        return None
    return Release(manifest["version"], int(manifest["size"]), manifest["sha256"], assets[ZIP_NAME])


# ---------------------------------------------------------------- staging
def safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """Extract, refusing absolute paths, '..', drive letters and links."""
    dest = dest.resolve()
    total = 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ":" in name or any(part == ".." for part in name.split("/")):
            raise UpdateError("Arsip pembaruan berisi path berbahaya.")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise UpdateError("Arsip pembaruan berisi symlink.")
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise UpdateError("Arsip pembaruan berisi path berbahaya.")
        total += info.file_size
        if total > MAX_ZIP * 3:
            raise UpdateError("Arsip pembaruan terlalu besar saat dibuka.")
    archive.extractall(dest)


def stage(release: Release) -> Path:
    data = _get(release.zip_url, release.size)
    if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
        raise UpdateError("File pembaruan tidak cocok dengan tanda tangan. Pembaruan ditolak.")
    root = updates_dir()
    shutil.rmtree(root, ignore_errors=True)
    folder = root / release.version
    folder.mkdir(parents=True)
    archive_path = root / ZIP_NAME
    archive_path.write_bytes(data)
    with zipfile.ZipFile(archive_path) as archive:
        safe_extract(archive, folder)
    archive_path.unlink()
    app = folder / APP_NAME
    if not (app / EXE_NAME).is_file():
        raise UpdateError("Arsip pembaruan tidak berisi Portunus.exe.")
    return app


def launch_helper(staged_app: Path) -> None:
    """Start the NEW build so it can replace this one after we exit."""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(staged_app / EXE_NAME), "--apply-update", str(install_dir()), str(os.getpid())],
        cwd=str(staged_app),
        creationflags=flags if sys.platform == "win32" else 0,
        close_fds=True,
    )


# ---------------------------------------------------------------- helper side
def _retry(action, attempts: int = 40, delay: float = 0.25) -> None:
    # Antivirus scanners and Explorer often hold files for a moment.
    for i in range(attempts):
        try:
            action()
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def apply_update(target: Path, old_pid: int) -> int:
    """Run from the staged build: swap the install folder, then relaunch."""
    import psutil

    try:
        psutil.Process(old_pid).wait(timeout=60)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        return 1
    staged = Path(sys.executable).resolve().parent
    target = target.resolve()
    backup = target.with_name(target.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    _retry(lambda: os.replace(target, backup))
    try:
        shutil.copytree(staged, target)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        _retry(lambda: os.replace(backup, target))  # roll back to the old build
        subprocess.Popen([str(target / EXE_NAME)], cwd=str(target), close_fds=True)
        return 2
    subprocess.Popen([str(target / EXE_NAME)], cwd=str(target), close_fds=True)
    return 0


def cleanup_after_update() -> None:
    """On a normal start, remove the previous build and the staging folder."""
    if not getattr(sys, "frozen", False):
        return
    here = install_dir()
    shutil.rmtree(here.with_name(here.name + ".old"), ignore_errors=True)
    if updates_dir() not in here.parents:
        shutil.rmtree(updates_dir(), ignore_errors=True)
