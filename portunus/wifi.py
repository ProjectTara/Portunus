"""Wi-Fi transport: RemotePairing over the local network, no admin rights.

How it stays safe:

* Opt-in per device. The Wi-Fi pair record is created only when the user
  presses "Aktifkan Wi-Fi" while the iPhone is plugged in and already trusted
  over USB (pymobiledevice3 performs RemotePairing pair-setup over lockdown,
  so iOS shows no extra prompt). No lockdown setting on the iPhone is changed.
* Devices on the network are matched to a pair record by their ``authTag``
  before anything is sent (pymobiledevice3 ``get_remote_pairing_tunnel_services``),
  so a spoofed Bonjour answer is ignored and the tunnel itself is mutually
  authenticated with the pair record keys.
* The tunnel is userspace (pmd-pytcp): no virtual network adapter, no admin
  rights, and its relays are socket pairs, so no port is opened on the PC.
* "Cabut Wi-Fi" deletes the pair record; this PC can no longer reach the
  iPhone over the network.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

log = logging.getLogger("portunus.wifi")

BONJOUR_TIMEOUT = 6.0


class WifiError(RuntimeError):
    """A failure with a message that is safe to show the user."""


def paired_devices() -> dict[str, Path]:
    """UDIDs that have a usable Wi-Fi pair record on this PC, with the record path."""
    found: dict[str, Path] = {}
    try:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
        from pymobiledevice3.remote.tunnel_service import PEER_ALT_IRK_KEY

        for identifier, path, record in iter_remote_pair_records_by_identifier():
            # A record without the altIRK cannot be matched to its device over
            # Bonjour, so it is not usable for Wi-Fi; "Aktifkan Wi-Fi" renews it.
            if record.get(PEER_ALT_IRK_KEY) is not None:
                found[identifier] = Path(path)
    except Exception as exc:  # noqa: BLE001 - a broken store just means "none"
        log.warning("cannot read Wi-Fi pair records: %s", type(exc).__name__)
    return found


def _all_records(udid: str) -> list[Path]:
    try:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier

        return [Path(p) for ident, p, _ in iter_remote_pair_records_by_identifier() if ident == udid]
    except Exception:  # noqa: BLE001
        return []


async def enable(udid: str) -> None:
    """Create the Wi-Fi pair record over the trusted USB connection."""
    from .srpcheck import pairing_math_ok

    if not pairing_math_ok():
        raise WifiError("Komponen pairing Wi-Fi di build ini tidak cocok (srptools). Perbarui Portunus ke versi terbaru.")
    if udid not in paired_devices():
        for stale in _all_records(udid):  # an old record without altIRK would be reused as-is
            stale.unlink(missing_ok=True)
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService

    lockdown = await create_using_usbmux(serial=udid, connection_type="USB", autopair=True, pair_timeout=60)
    try:
        service = await RemotePairingLockdownService.create(lockdown)
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            pass  # the device closes the channel once pair-setup has written the record
        finally:
            await service.close()
    finally:
        await lockdown.close()
    if udid not in paired_devices():
        raise WifiError("iPhone tidak memberikan izin Wi-Fi. Buka kunci iPhone lalu coba lagi.")


def revoke(udid: str) -> bool:
    """Delete this PC's Wi-Fi pair record for the device. Returns True if one was removed."""
    paths = _all_records(udid)
    for path in paths:
        path.unlink(missing_ok=True)
    return bool(paths)


def _userspace_tunnel_class():
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote import userspace_tunnel as ut
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

    class WifiRsdTunnel(ut.UserspaceRsdTunnel):
        """UserspaceRsdTunnel whose provider is the RemotePairing service found on the
        network, instead of the USB CoreDeviceProxy. Teardown, the transport watcher
        and the one-tunnel-per-process guard are inherited unchanged."""

        async def _aopen_locked(self) -> RemoteServiceDiscoveryService:
            if ut._active_tunnel is not None:  # noqa: SLF001 - same guard as the base class
                raise WifiError("Sudah ada sambungan aktif. Putuskan dulu.")
            tunnel_service.USE_USERSPACE_TUNNEL = True
            stack = AsyncExitStack()
            try:
                services = await tunnel_service.get_remote_pairing_tunnel_services(
                    bonjour_timeout=BONJOUR_TIMEOUT, udid=self.serial
                )
                if not services:
                    raise WifiError(
                        "iPhone tidak ditemukan di jaringan. Pastikan iPhone dan PC di Wi-Fi yang sama "
                        "dan layar iPhone menyala."
                    )
                provider = services[0]
                for extra in services[1:]:
                    await extra.close()
                stack.push_async_callback(provider.close)
                result = await stack.enter_async_context(provider.start_tcp_tunnel())
                self.tun = result.client.tun
                self.tun.set_peer(result.address)
                dial_plane = await stack.enter_async_context(ut.UserspaceDialPlane(self.tun, result.address))
                rsd = RemoteServiceDiscoveryService(
                    (result.address, result.port),
                    open_connection=dial_plane.dial,
                    auxiliary_metadata=result.auxiliary_metadata,
                )
                stack.push_async_callback(rsd.close)
                await rsd.connect()
            except BaseException:
                await stack.aclose()
                tunnel_service.USE_USERSPACE_TUNNEL = False
                self.tun = None
                raise
            self._exit_stack = stack
            self.rsd = rsd
            ut._active_tunnel = self  # noqa: SLF001
            ut.USERSPACE_ACTIVE = True
            import asyncio

            self._transport_watcher = asyncio.create_task(self._watch_transport_closed(result.client))
            return rsd

    return WifiRsdTunnel


async def open_tunnel(udid: str):
    """Open a Wi-Fi tunnel. Returns (handle with aclose(), rsd)."""
    cls = _userspace_tunnel_class()
    tunnel = cls(serial=udid, autopair=False)
    rsd = await tunnel.aopen()
    return tunnel, rsd


def device_name(rsd) -> Optional[str]:
    info = getattr(rsd, "peer_info", None) or {}
    props = info.get("Properties", {}) if isinstance(info, dict) else {}
    return props.get("DeviceName") or None
