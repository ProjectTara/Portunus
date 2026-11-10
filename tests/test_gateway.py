"""Tests for the gateway's security gate and engine proxy.

They run on any OS without an iPhone: a fake engine stands in for
pymobiledevice3's ScreenStreamServer.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

import socket
import subprocess
import sys

from portunus import gateway as gateway_mod
from portunus.core import Core, SessionError
from portunus.gateway import Gateway
from portunus.ocr import Line, build_text
from portunus.settings import Settings
from portunus.upstream import Upstream

TOKEN = "t" * 43
ENGINE_PASSWORD = "engine-secret"


class FakeEngine:
    """Records what reaches it and checks the Basic password like the real one."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bytes]] = []
        self.port = 0

    async def start(self) -> None:
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.server = server
        self.port = server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            method, path, _ = line.decode().split()
            headers = {}
            while True:
                raw = await reader.readline()
                if raw in (b"\r\n", b""):
                    break
                k, _, v = raw.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            expected = "Basic " + base64.b64encode(f"portunus:{ENGINE_PASSWORD}".encode()).decode()
            if headers.get("authorization") != expected:
                writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                break
            self.seen.append((method, path, body))
            if path == "/stream.bin":
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                frame = b"\x00\x00\x00\x03\x00ab"
                writer.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n0\r\n\r\n")
                break
            if path in ("/touch", "/key", "/button"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
                await writer.drain()
                continue
            payload = json.dumps({"codec": "hev1.1.6.L153.B0", "description": "AA=="}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
            )
            break
        await writer.drain()
        writer.close()


async def raw_request(port: int, text: str, body: bytes = b"") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(text.replace("\n", "\r\n").encode() + body)
    await writer.drain()
    data = await reader.read(-1)
    writer.close()
    status = int(data.split(b" ", 2)[1])
    return status, data


def req(port, method, path, *, host=None, token=TOKEN, ctype="application/json", body=b"", extra=""):
    host = host or f"127.0.0.1:{port}"
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
    if token is not None:
        lines.append(f"X-Portunus-Token: {token}")
    if method == "POST":
        lines += [f"Content-Type: {ctype}", f"Content-Length: {len(body)}"]
    if extra:
        lines.append(extra)
    return raw_request(port, "\n".join(lines) + "\n\n", body)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        ui = Path(__file__).resolve().parent.parent / "portunus" / "ui"
        self.core = Core()  # loop thread not started; the test loop drives the gateway
        self.engine = FakeEngine()
        await self.engine.start()
        self.core.upstream = Upstream(port=self.engine.port, password=ENGINE_PASSWORD)
        settings = Settings(Path(self.tmp.name) / "s.json")
        self.gw = Gateway(self.core, ui, settings, token=TOKEN)
        self.port = await self.gw.start()

    async def asyncTearDown(self) -> None:
        self.core.upstream.close()
        await self.gw.stop()
        self.engine.server.close()
        self.tmp.cleanup()

    # --- the gate ---------------------------------------------------------
    async def test_static_ui_needs_no_token(self):
        status, data = await req(self.port, "GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Content-Security-Policy", data)

    async def test_dns_rebinding_host_refused(self):
        status, _ = await req(self.port, "GET", "/", host=f"evil.example:{self.port}", token=None)
        self.assertEqual(status, 421)
        status, _ = await req(self.port, "GET", "/api/state", host=f"localhost:{self.port}")
        self.assertEqual(status, 421)

    async def test_foreign_origin_refused(self):
        status, _ = await req(self.port, "GET", "/api/state", extra="Origin: https://evil.example")
        self.assertEqual(status, 403)

    async def test_cross_site_fetch_metadata_refused(self):
        status, _ = await req(self.port, "GET", "/api/state", extra="Sec-Fetch-Site: cross-site")
        self.assertEqual(status, 403)

    async def test_missing_or_wrong_token_refused(self):
        self.assertEqual((await req(self.port, "GET", "/api/state", token=None))[0], 403)
        self.assertEqual((await req(self.port, "GET", "/api/state", token="nope"))[0], 403)

    async def test_simple_form_post_refused(self):
        # What a CSRF page could send without a preflight: text/plain and no token.
        status, _ = await req(self.port, "POST", "/m/key", token=None, ctype="text/plain", body=b'{"usages":[4]}')
        self.assertEqual(status, 403)
        status, _ = await req(self.port, "POST", "/m/key", ctype="text/plain", body=b'{"usages":[4]}')
        self.assertEqual(status, 415)
        self.assertEqual(self.engine.seen, [])

    async def test_duplicate_header_refused(self):
        status, _ = await req(self.port, "GET", "/api/state", extra=f"Host: 127.0.0.1:{self.port}")
        self.assertEqual(status, 400)

    async def test_chunked_request_refused(self):
        status, _ = await req(self.port, "GET", "/api/state", extra="Transfer-Encoding: chunked")
        self.assertEqual(status, 411)

    async def test_oversized_body_refused(self):
        status, _ = await req(self.port, "POST", "/m/touch", body=b"x" * 5000)
        self.assertEqual(status, 413)

    async def test_engine_debug_routes_unreachable(self):
        for path in ("/m/debug/idr-off", "/m/accessibility", "/m/clipboard", "/m/audio.bin", "/debug-metrics"):
            status, _ = await req(self.port, "GET", path)
            self.assertEqual(status, 404, path)
        self.assertEqual(self.engine.seen, [])

    # --- proxy ------------------------------------------------------------
    async def test_input_reaches_engine_with_engine_password(self):
        status, _ = await req(self.port, "POST", "/m/touch", body=b'{"type":"tap","x":1,"y":2}')
        self.assertEqual(status, 200)
        status, _ = await req(self.port, "POST", "/m/key", body=b'{"usages":[4]}')
        self.assertEqual(status, 200)
        self.assertEqual([p for _, p, _ in self.engine.seen], ["/touch", "/key"])

    async def test_codec_proxied(self):
        status, data = await req(self.port, "GET", "/m/codec")
        self.assertEqual(status, 200)
        self.assertIn(b"hev1", data)

    async def test_stream_relayed_chunked(self):
        status, data = await req(self.port, "GET", "/m/stream.bin")
        self.assertEqual(status, 200)
        self.assertIn(b"Transfer-Encoding: chunked", data)
        self.assertIn(b"\x00\x00\x00\x03\x00ab", data)

    async def test_no_session_means_503(self):
        self.core.upstream = None
        status, _ = await req(self.port, "POST", "/m/touch", body=b"{}")
        self.assertEqual(status, 503)
        self.core.upstream = Upstream(port=self.engine.port, password=ENGINE_PASSWORD)

    async def test_settings_schema_enforced(self):
        body = json.dumps({"theme": "dark", "scrollSpeed": 9999, "pin": "1234", "ctrlAsCmd": "yes"}).encode()
        status, data = await req(self.port, "POST", "/api/settings", body=body)
        self.assertEqual(status, 200)
        saved = json.loads(data.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(saved["scrollSpeed"], 250)
        self.assertNotIn("pin", saved)
        self.assertIs(saved["ctrlAsCmd"], True)


    # --- QA regressions ---------------------------------------------------
    async def test_gate_runs_before_body_is_read(self):
        # Huge declared body, no token, no body bytes sent: must be refused at once.
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(
            f"POST /api/ocr HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 15000000\r\n\r\n".encode()
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(200), 1.0)
        self.assertTrue(data.startswith(b"HTTP/1.1 403"), data[:40])
        writer.close()

    async def test_cross_site_big_body_refused_before_read(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(
            f"POST /api/ocr HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: https://evil.example\r\n"
            "Content-Type: text/plain\r\nContent-Length: 15000000\r\n\r\n".encode()
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(200), 1.0)
        self.assertTrue(data.startswith(b"HTTP/1.1 403"), data[:40])
        writer.close()

    async def test_slow_headers_dropped(self):
        old = gateway_mod.HEADER_TIMEOUT
        gateway_mod.HEADER_TIMEOUT = 0.3
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.write(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(100), 2.0)
            self.assertEqual(data, b"")  # server closed the connection
            writer.close()
        finally:
            gateway_mod.HEADER_TIMEOUT = old

    async def test_connection_cap(self):
        conns = [await asyncio.open_connection("127.0.0.1", self.port) for _ in range(gateway_mod.MAX_CONNECTIONS)]
        await asyncio.sleep(0.05)
        extra_reader, extra_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(extra_reader.read(10), 1.0), b"")
        extra_writer.close()
        for _, w in conns:
            w.close()
        await asyncio.sleep(0.05)
        status, _ = await req(self.port, "GET", "/api/state")
        self.assertEqual(status, 200)  # slots are released


class EnginePortOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """The engine port must be ours before any password is sent to it."""

    async def test_foreign_listener_is_refused(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        squatter = subprocess.Popen([sys.executable, "-c", (
            "import socket,time;s=socket.socket();"
            f"s.bind(('127.0.0.1',{port}));s.listen();print('up',flush=True);time.sleep(30)"
        )], stdout=subprocess.PIPE)
        try:
            squatter.stdout.readline()
            never_ready = asyncio.get_running_loop().create_future()
            core = Core()
            with self.assertRaises(SessionError):
                await core._wait_for_port(port, asyncio.ensure_future(never_ready), timeout=3)
        finally:
            squatter.kill()
            squatter.wait()
            squatter.stdout.close()

    async def test_own_listener_is_accepted(self):
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        pending = asyncio.ensure_future(asyncio.sleep(60))
        try:
            await Core()._wait_for_port(port, pending, timeout=3)
        finally:
            pending.cancel()
            server.close()


class OcrLayoutTests(unittest.TestCase):
    def test_paragraphs_and_hyphenation(self):
        lines = [
            Line("Halo semua,", 0, 20, 0),
            Line("ini baris ke-", 24, 44, 0),
            Line("dua yang lanjut.", 48, 68, 0),
            Line("Paragraf baru.", 110, 130, 0),
        ]
        self.assertEqual(build_text(lines), "Halo semua,\nini baris kedua yang lanjut.\n\nParagraf baru.")

    def test_hyphen_join_lowercase(self):
        lines = [Line("infor-", 0, 20, 0), Line("masi", 24, 44, 0)]
        self.assertEqual(build_text(lines), "informasi")


if __name__ == "__main__":
    unittest.main()
