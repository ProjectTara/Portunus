"""Minimal HTTP/1.1 client for the private stream engine on loopback.

The engine (pymobiledevice3's ScreenStreamServer) listens on a random
loopback port and requires an HTTP Basic password that only this process
knows. Nothing outside Portunus is given that port's password.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field


MAX_RESPONSE = 8 * 1024 * 1024


class UpstreamError(ConnectionError):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class Upstream:
    port: int
    password: str
    host: str = "127.0.0.1"
    _lane_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _lane: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None

    @property
    def _auth(self) -> str:
        return "Basic " + base64.b64encode(f"portunus:{self.password}".encode()).decode()

    def _head(self, method: str, path: str, body: bytes, keep_alive: bool, ctype: str) -> bytes:
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            f"Authorization: {self._auth}",
            f"Connection: {'keep-alive' if keep_alive else 'close'}",
        ]
        if method == "POST":
            lines.append(f"Content-Type: {ctype}")
            lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    async def read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
        status_line = await reader.readline()
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise UpstreamError("bad status line from stream engine")
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        return int(parts[1]), headers

    async def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        ctype: str = "application/json",
        timeout: float = 15.0,
    ) -> Response:
        """One-shot request; the engine closes the connection after replying."""

        async def run() -> Response:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            try:
                writer.write(self._head(method, path, body, False, ctype) + body)
                await writer.drain()
                status, headers = await self.read_head(reader)
                length = headers.get("content-length")
                if length is not None and length.isdigit():
                    if int(length) > MAX_RESPONSE:
                        raise UpstreamError("engine response too large")
                    payload = await reader.readexactly(int(length))
                else:
                    payload = await reader.read(MAX_RESPONSE + 1)
                    if len(payload) > MAX_RESPONSE:
                        raise UpstreamError("engine response too large")
                return Response(status, headers, payload)
            finally:
                writer.close()

        try:
            return await asyncio.wait_for(run(), timeout)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            raise UpstreamError(str(exc) or exc.__class__.__name__) from exc

    async def open_stream(self, path: str) -> tuple[int, dict[str, str], asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a long-lived GET (video stream). Caller owns the writer."""
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            writer.write(self._head("GET", path, b"", False, ""))
            await writer.drain()
            status, headers = await asyncio.wait_for(self.read_head(reader), 30)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            raise UpstreamError(str(exc) or exc.__class__.__name__) from exc
        return status, headers, reader, writer

    async def send_input(self, path: str, body: bytes) -> int:
        """Touch/key/button events over one persistent keep-alive connection.

        Keeps events strictly ordered and avoids a TCP handshake per pointer
        move. Reconnects once if the engine dropped the connection.
        """
        async with self._lane_lock:
            for attempt in (1, 2):
                try:
                    if self._lane is None:
                        self._lane = await asyncio.open_connection(self.host, self.port)
                    reader, writer = self._lane
                    writer.write(self._head("POST", path, body, True, "application/json") + body)
                    await writer.drain()
                    status, headers = await asyncio.wait_for(self.read_head(reader), 5)
                    length = int(headers.get("content-length") or 0)
                    if length:
                        await reader.readexactly(length)
                    if headers.get("connection", "").lower() == "close":
                        self._drop_lane()
                    return status
                except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, UpstreamError):
                    self._drop_lane()
                    if attempt == 2:
                        raise UpstreamError("input channel lost")
        return 503

    def _drop_lane(self) -> None:
        if self._lane is not None:
            self._lane[1].close()
            self._lane = None

    def close(self) -> None:
        self._drop_lane()
