"""Device discovery and the mirroring session.

Everything device-related runs on one asyncio loop in a background thread.
The session is a small state machine:

    idle -> pairing -> tunnel -> (mounting) -> starting -> live
                                   any step -> error

Device-side state is never changed silently. The only device writes are the
ones the user starts: pairing (iOS shows its own Trust prompt), mounting the
Developer Disk Image when the display service is missing, and revealing the
Developer Mode switch from the setup screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from .upstream import Upstream

log = logging.getLogger("portunus.core")

DISPLAY_SERVICE = "com.apple.coredevice.displayservice"
MIN_IOS_MAJOR = 27


class SessionError(RuntimeError):
    """An error with a message that is safe and useful to show the user."""


@dataclass
class State:
    phase: str = "idle"
    message: str = ""
    udid: Optional[str] = None
    device: dict[str, Any] = field(default_factory=dict)
    devices: list[dict[str, Any]] = field(default_factory=list)
    usbmux_ok: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "message": self.message,
            "udid": self.udid,
            "device": self.device,
            "devices": self.devices,
            "usbmuxOk": self.usbmux_ok,
        }


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def explain(exc: BaseException) -> str:
    """Map library exceptions to an actionable sentence."""
    name = type(exc).__name__
    table = {
        "PairingDialogResponsePendingError": "Buka kunci iPhone, ketuk Percayai, lalu masukkan kode sandi iPhone.",
        "UserDeniedPairingError": "iPhone menolak PC ini. Cabut kabel, colok lagi, lalu ketuk Percayai.",
        "PasswordRequiredError": "Buka kunci iPhone dulu, lalu coba lagi.",
        "NotPairedError": "PC belum dipercaya iPhone. Buka kunci iPhone lalu ketuk Percayai.",
        "FatalPairingError": "Pairing gagal. Cabut kabel, colok lagi, lalu coba lagi.",
        "DeveloperModeIsNotEnabledError": "Developer Mode belum aktif. Ikuti langkah persiapan di layar awal.",
        "DeveloperModeError": "Developer Mode belum aktif. Ikuti langkah persiapan di layar awal.",
        "DeviceNotFoundError": "iPhone tidak terdeteksi lagi. Periksa kabel USB.",
        "NoDeviceConnectedError": "iPhone tidak terdeteksi. Periksa kabel USB.",
        "ConnectionFailedToUsbmuxdError": "Layanan Apple Mobile Device tidak berjalan. Pasang aplikasi Apple Devices dari Microsoft Store.",
        "UserspaceTunnelUnavailableError": "iOS ini belum bisa dibuatkan terowongan. Perbarui iPhone ke iOS terbaru.",
        "DeveloperDiskImageNotFoundError": "Developer Disk Image tidak bisa diunduh. Periksa koneksi internet, lalu coba lagi.",
    }
    if name in table:
        return table[name]
    if isinstance(exc, SessionError):
        return str(exc)
    if "in use" in str(exc).lower():
        return "Kamera atau mikrofon iPhone sedang dipakai aplikasi lain. Tutup aplikasi itu, lalu coba lagi."
    return f"Sambungan gagal ({name}). Cabut kabel, colok lagi, lalu coba lagi."


class Core:
    """Owns the background loop, the device list, and the live session."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.state = State()
        self.upstream: Optional[Upstream] = None
        self._thread = threading.Thread(target=self._run_loop, name="portunus-core", daemon=True)
        self._session_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._names: dict[str, dict[str, Any]] = {}

    # ---- thread / loop plumbing -------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self.submit(self._start_watch())

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _start_watch(self) -> None:
        self._watch_task = asyncio.create_task(self._watch_devices())

    def shutdown(self, timeout: float = 6.0) -> None:
        async def stop() -> None:
            if self._watch_task:
                self._watch_task.cancel()
            await self.disconnect()

        with contextlib.suppress(Exception):
            self.submit(stop()).result(timeout)
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ---- device list -------------------------------------------------------
    async def _watch_devices(self) -> None:
        from pymobiledevice3.usbmux import list_devices

        while True:
            try:
                found = await asyncio.wait_for(list_devices(), 5)
                devices = []
                for dev in found:
                    if dev.connection_type != "USB":
                        continue
                    info = self._names.get(dev.serial, {})
                    devices.append({"udid": dev.serial, "name": info.get("name"), "ios": info.get("ios")})
                self.state.devices = devices
                self.state.usbmux_ok = True
            except Exception as exc:  # noqa: BLE001
                log.debug("device scan failed: %s", exc)
                self.state.devices = []
                self.state.usbmux_ok = False
            await asyncio.sleep(2.0)

    # ---- session -----------------------------------------------------------
    def _set(self, phase: str, message: str = "") -> None:
        self.state.phase = phase
        self.state.message = message
        log.info("phase=%s", phase)

    async def connect(self, udid: str) -> None:
        if not any(d["udid"] == udid for d in self.state.devices):
            raise SessionError("iPhone itu tidak tersambung lewat USB.")
        await self.disconnect()
        self.state.udid = udid
        self._session_task = asyncio.create_task(self._run_session(udid))

    async def disconnect(self) -> None:
        task, self._session_task = self._session_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 8)
        if self.upstream:
            self.upstream.close()
            self.upstream = None
        if self.state.phase != "error":
            self._set("idle")

    async def reveal_developer_mode(self, udid: str) -> None:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.amfi import AmfiService

        lockdown = await create_using_usbmux(serial=udid, connection_type="USB", autopair=True, pair_timeout=60)
        try:
            await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
        finally:
            await lockdown.close()

    async def _identify(self, udid: str) -> dict[str, Any]:
        from pymobiledevice3.lockdown import create_using_usbmux

        lockdown = await create_using_usbmux(serial=udid, connection_type="USB", autopair=True, pair_timeout=90)
        try:
            version = str(lockdown.product_version or "")
            name = lockdown.short_info.get("DeviceName") or lockdown.display_name or "iPhone"
        finally:
            await lockdown.close()
        info = {"name": name, "ios": version}
        self._names[udid] = info
        return info

    async def _dial_tunnel(self, udid: str):
        from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel

        tunnel = PreferredRsdTunnel(serial=udid, autopair=True)
        rsd = await asyncio.wait_for(tunnel.aopen(), 45)
        return tunnel, rsd

    @staticmethod
    def _has_display(rsd) -> bool:
        services = (getattr(rsd, "peer_info", None) or {}).get("Services", {})
        return DISPLAY_SERVICE in services

    async def _run_session(self, udid: str) -> None:
        from pymobiledevice3.remote.core_device.screen_stream import ScreenStreamServer

        tunnel = None
        upstream: Optional[Upstream] = None
        serve_task: Optional[asyncio.Task] = None
        try:
            self._set("pairing", "Menyapa iPhone. Jika muncul pertanyaan Percayai komputer ini, ketuk Percayai.")
            info = await self._identify(udid)
            self.state.device = info
            major = info["ios"].split(".")[0]
            if major.isdigit() and int(major) < MIN_IOS_MAJOR:
                raise SessionError(
                    f"iPhone ini memakai iOS {info['ios']}. Mirroring layar butuh iOS {MIN_IOS_MAJOR} atau lebih baru."
                )

            self._set("tunnel", "Membuka jalur data ke iPhone.")
            tunnel, rsd = await self._dial_tunnel(udid)

            if not self._has_display(rsd):
                self._set("mounting", "Memasang Developer Disk Image. Ini sekali saja dan perlu internet.")
                from pymobiledevice3.services.mobile_image_mounter import auto_mount

                await auto_mount(rsd)
                await tunnel.aclose()
                tunnel, rsd = await self._dial_tunnel(udid)
                if not self._has_display(rsd):
                    raise SessionError("iPhone tidak menyediakan layanan layar. Pastikan Developer Mode aktif.")

            self._set("starting", "Menyalakan siaran layar.")
            port = free_loopback_port()
            password = secrets.token_urlsafe(32)
            server = ScreenStreamServer(
                rsd,
                bind="127.0.0.1",
                http_port=port,
                password=password,
                audio_default_on=False,
            )
            serve_task = asyncio.create_task(server.serve())
            upstream = Upstream(port=port, password=password)
            await self._wait_for_port(port, serve_task)
            self.upstream = upstream
            self._set("live")
            await serve_task
            raise SessionError("Siaran layar berhenti. Colok ulang kabel lalu hubungkan lagi.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("session failed: %s: %s", type(exc).__name__, exc)
            self._set("error", explain(exc))
        finally:
            if serve_task is not None and not serve_task.done():
                # Never leave the engine running without its session.
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(serve_task, 5)
            if upstream:
                upstream.close()
            if self.upstream is upstream:
                self.upstream = None
            if tunnel is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tunnel.aclose(), 5)

    @staticmethod
    def _listeners(port: int) -> Optional[set[int]]:
        """PIDs listening on TCP ``port``, or None if the OS will not say."""
        import psutil

        try:
            conns = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, OSError):
            return None
        return {c.pid for c in conns if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port}

    @staticmethod
    def _we_listen(port: int) -> bool:
        import psutil

        me = psutil.Process()
        conns = me.net_connections(kind="tcp") if hasattr(me, "net_connections") else me.connections(kind="tcp")
        return any(
            c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.ip == "127.0.0.1" and c.laddr.port == port
            for c in conns
        )

    async def _wait_for_port(self, port: int, serve_task: asyncio.Task, timeout: float = 45.0) -> None:
        """Wait until the engine is listening, and prove the listener is ours.

        The engine binds only after it has started the device stream, which can
        take many seconds. Another local program could grab the free port in
        that gap; connecting to it would hand that program the engine password
        and everything typed. So we never connect until the operating system
        confirms that this process, and only this process, owns the port.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if serve_task.done():
                serve_task.result()  # re-raise the engine's error
                raise SessionError("Mesin siaran berhenti sebelum siap.")
            owners = await asyncio.to_thread(self._listeners, port)
            ours = await asyncio.to_thread(self._we_listen, port)
            if owners is not None and owners - {os.getpid()}:
                log.warning("foreign listener on engine port %d: pids %s", port, sorted(owners))
                raise SessionError("Program lain memakai port siaran. Tutup program itu atau restart PC, lalu coba lagi.")
            if ours:
                return
            await asyncio.sleep(0.25)
        raise SessionError("iPhone tidak mulai menyiarkan layar dalam 45 detik. Buka kunci iPhone lalu coba lagi.")
