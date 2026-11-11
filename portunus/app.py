"""Start Portunus: one process, one window, one loopback gateway."""

from __future__ import annotations

import base64
import binascii
import ctypes
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME, __version__, btaudio, updater
from .core import Core
from .gateway import Gateway
from .settings import Settings

log = logging.getLogger("portunus")


def resource_dir() -> Path:
    # PyInstaller unpacks data next to sys._MEIPASS; source runs use the package dir.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "portunus" / "ui"


def setup_logging() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(base / "portunus.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # The library logs at DEBUG/INFO very chattily; keep only warnings.
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
    return base


def single_instance() -> object | None:
    """Return a mutex handle, or None if Portunus is already running."""
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}-single-instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


class JsBridge:
    """Methods callable from the page as window.pywebview.api.*.

    Only the page loaded in Portunus's own window can reach this object, which
    is how the session token reaches the UI without ever appearing in a URL.
    """

    def __init__(self, token: str, origin: str) -> None:
        self._token = token
        self._origin = origin
        self._window = None
        self._fullscreen = False

    def _on_own_page(self) -> bool:
        # pywebview exposes this object to whatever page the window shows. The
        # UI never navigates away, but if anything ever did, a foreign page
        # must not receive the token or the file-save capability.
        try:
            url = self._window.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return url == self._origin or url.startswith(self._origin + "/")

    def session(self) -> dict:
        if not self._on_own_page():
            log.warning("token request from a foreign page refused")
            return {"token": "", "version": __version__}
        return {"token": self._token, "version": __version__}

    def set_fullscreen(self, on: bool) -> dict:
        """Focus mode: make the window full screen (or restore it)."""
        if not self._on_own_page() or not isinstance(on, bool):
            return {"ok": False}
        if on != self._fullscreen:
            self._window.toggle_fullscreen()
            self._fullscreen = on
        return {"ok": True, "fullscreen": self._fullscreen}

    def save_screenshot(self, data_url: str) -> dict:
        import webview

        if not self._on_own_page():
            return {"ok": False, "error": "Ditolak."}

        prefix = "data:image/png;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            return {"ok": False, "error": "Format gambar tidak dikenal."}
        try:
            png = base64.b64decode(data_url[len(prefix):], validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "Gambar rusak."}
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            return {"ok": False, "error": "Gambar rusak."}
        target = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="portunus.png",
            file_types=("PNG (*.png)",),
        )
        if not target:
            return {"ok": False, "cancelled": True}
        path = Path(target if isinstance(target, str) else target[0])
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        path.write_bytes(png)
        return {"ok": True, "name": path.name}


def main() -> None:
    import webview

    setup_logging()
    mutex = single_instance()
    if mutex is None:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, "Portunus sudah terbuka.", APP_NAME, 0x40)
        return

    updater.cleanup_after_update()
    settings = Settings()
    core = Core()
    core.start()
    audio = btaudio.BluetoothAudio() if btaudio.AVAILABLE else None
    window_ref: dict = {}

    def restart_into_update(staged) -> None:
        # Called after the new build is verified and unpacked. The helper waits
        # for this process to exit, so close the window to let it proceed.
        updater.launch_helper(staged)
        window_ref["w"].destroy()

    gateway = Gateway(core, resource_dir(), settings, audio, on_update_ready=restart_into_update)
    port = core.submit(gateway.start()).result(10)
    log.info("Portunus %s gateway on 127.0.0.1:%d", __version__, port)

    bridge = JsBridge(gateway.token, f"http://127.0.0.1:{port}")
    window = webview.create_window(
        APP_NAME,
        url=f"http://127.0.0.1:{port}/",
        js_api=bridge,
        width=1120,
        height=900,
        min_size=(560, 640),
        background_color="#D9DEE2",
        text_select=False,
    )
    bridge._window = window
    window_ref["w"] = window

    try:
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            private_mode=True,  # no cookies, cache, or localStorage persisted to disk
            debug=bool(os.environ.get("PORTUNUS_DEBUG")),
        )
    finally:
        if audio:
            audio.shutdown()
        core.submit(gateway.stop())
        core.shutdown()
