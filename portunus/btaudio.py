"""Play the iPhone's audio on the PC through Bluetooth (A2DP sink).

Uses Windows' AudioPlaybackConnection in-process. The iPhone must already be
paired with the PC in Windows Bluetooth settings. All WinRT calls run on a
single worker thread so the connection object always lives on one thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

try:
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.media.audio import (
        AudioPlaybackConnection,
        AudioPlaybackConnectionOpenResultStatus,
    )

    AVAILABLE = True
except Exception:  # noqa: BLE001
    AVAILABLE = False


class AudioError(RuntimeError):
    pass


class BluetoothAudio:
    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="btaudio")
        self._connection = None
        self._device_id: str | None = None

    # -- worker-thread side ------------------------------------------------
    def _list(self) -> list[dict]:
        async def run() -> list[dict]:
            selector = AudioPlaybackConnection.get_device_selector()
            found = await DeviceInformation.find_all_async_aqs_filter(selector)
            return [{"id": d.id, "name": d.name} for d in found]

        return asyncio.run(run())

    def _start(self, device_id: str) -> None:
        self._stop()

        async def run():
            connection = AudioPlaybackConnection.try_create_from_id(device_id)
            if connection is None:
                raise AudioError("Perangkat Bluetooth tidak ditemukan. Pasangkan iPhone di Settings Bluetooth Windows.")
            await connection.start_async()
            result = await connection.open_async()
            if result.status != AudioPlaybackConnectionOpenResultStatus.SUCCESS:
                connection.close()
                raise AudioError("Windows menolak sambungan audio. Pastikan Bluetooth iPhone menyala dan dekat PC.")
            return connection

        self._connection = asyncio.run(run())
        self._device_id = device_id

    def _stop(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
                self._device_id = None

    # -- loop side ---------------------------------------------------------
    async def _call(self, fn, *args):
        if not AVAILABLE:
            raise AudioError("Audio Bluetooth butuh paket winrt (lihat requirements.txt).")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    async def devices(self) -> list[dict]:
        return await self._call(self._list)

    async def start(self, device_id: str) -> None:
        await self._call(self._start, device_id)

    async def stop(self) -> None:
        if AVAILABLE:
            await self._call(self._stop)

    @property
    def active_device(self) -> str | None:
        return self._device_id

    def shutdown(self) -> None:
        if AVAILABLE:
            try:
                self._pool.submit(self._stop).result(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)
