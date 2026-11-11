"""The only HTTP surface Portunus exposes, bound to a random loopback port.

Every request passes the same gate, in this order:

1. ``Host`` must be exactly ``127.0.0.1:<port>``. A DNS-rebinding page sends
   its own host name here, so it is refused before anything else runs.
2. ``Origin``, when present, must be this server's own origin.
3. ``Sec-Fetch-Site``, when present, must be ``same-origin`` or ``none``.
4. The route must be in the fixed table below. Unknown paths are 404, and
   the stream engine's research/debug endpoints are never reachable.
5. Every route except the static UI files needs ``X-Portunus-Token``, a
   random per-run secret handed to the UI through pywebview's JS bridge.
   A custom header also forces a CORS preflight, which this server never
   approves, so cross-site pages cannot send it.
6. POST bodies must be ``application/json`` and under a per-route size cap.

Responses carry a strict Content-Security-Policy and no-store caching.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import __version__, btaudio, ocr, updater, winclip
from .core import Core, SessionError, explain
from .settings import Settings
from .upstream import UpstreamError

log = logging.getLogger("portunus.gateway")

TOKEN_HEADER = "x-portunus-token"
MAX_HEADERS = 64
MAX_LINE = 8192
MAX_CONNECTIONS = 48      # the UI needs a handful; anything more is abuse
IDLE_TIMEOUT = 120.0      # keep-alive wait for the next request line
HEADER_TIMEOUT = 10.0     # whole header block, defeats slowloris
BODY_TIMEOUT = 30.0

SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
     "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

# Engine routes the UI may reach, with the gateway path, the engine path,
# the HTTP method, and the body cap. Anything else on the engine is unreachable.
INPUT_ROUTES = {"/m/touch": "/touch", "/m/key": "/key", "/m/button": "/button"}
SIMPLE_ROUTES = {
    ("GET", "/m/codec"): "/codec",
    ("POST", "/m/pli"): "/pli",
    ("POST", "/m/restart"): "/restart",
    ("POST", "/m/rotate"): "/rotate",
}


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""

    def json(self) -> dict:
        try:
            data = json.loads(self.body or b"{}")
        except ValueError as exc:
            raise HttpError(400, "Body bukan JSON.") from exc
        if not isinstance(data, dict):
            raise HttpError(400, "Body harus objek JSON.")
        return data


class HttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


REASONS = {
    200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 411: "Length Required",
    413: "Payload Too Large", 415: "Unsupported Media Type", 421: "Misdirected Request",
    431: "Request Header Fields Too Large", 500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable",
}


def _head(status: int, headers: list[tuple[str, str]]) -> bytes:
    lines = [f"HTTP/1.1 {status} {REASONS.get(status, 'Status')}"]
    lines += [f"{k}: {v}" for k, v in SECURITY_HEADERS]
    lines += [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


Handler = Callable[[Request], Awaitable[dict]]


class Gateway:
    def __init__(
        self,
        core: Core,
        ui_dir: Path,
        settings: Settings,
        audio: Optional[btaudio.BluetoothAudio] = None,
        token: Optional[str] = None,
        on_update_ready: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.core = core
        self.settings = settings
        self.audio = audio
        self.token = token or secrets.token_urlsafe(32)
        self.port = 0
        self._connections = 0
        self._on_update_ready = on_update_ready
        self._release: Optional[updater.Release] = None
        self._update_busy = False
        self.core.allow_wifi = bool(self.settings.snapshot().get("allowWifi"))
        self._server: Optional[asyncio.base_events.Server] = None
        self._static = self._load_static(ui_dir)
        # (method, path) -> (handler, max_body)
        self._api: dict[tuple[str, str], tuple[Handler, int]] = {
            ("GET", "/api/state"): (self._state, 0),
            ("POST", "/api/connect"): (self._connect, 1024),
            ("POST", "/api/wifi/enable"): (self._wifi_enable, 1024),
            ("POST", "/api/wifi/revoke"): (self._wifi_revoke, 1024),
            ("POST", "/api/disconnect"): (self._disconnect, 64),
            ("POST", "/api/reveal-devmode"): (self._reveal, 1024),
            ("GET", "/api/settings"): (self._get_settings, 0),
            ("POST", "/api/settings"): (self._set_settings, 4096),
            ("POST", "/api/clipboard/to-phone"): (self._clip_to_phone, 64),
            ("POST", "/api/clipboard/from-phone"): (self._clip_from_phone, 64),
            ("POST", "/api/ocr"): (self._ocr, 16 * 1024 * 1024),
            ("GET", "/api/audio/devices"): (self._audio_devices, 0),
            ("POST", "/api/audio"): (self._audio_toggle, 2048),
            ("GET", "/api/update"): (self._update_status, 0),
            ("POST", "/api/update/check"): (self._update_check, 64),
            ("POST", "/api/update/install"): (self._update_install, 64),
        }

    # ---- lifecycle ---------------------------------------------------------
    @staticmethod
    def _load_static(ui_dir: Path) -> dict[str, tuple[bytes, str]]:
        # Files are read once into a fixed map, so request paths are never
        # joined onto the filesystem (no traversal possible).
        files: dict[str, tuple[bytes, str]] = {}
        for item in ui_dir.iterdir():
            ctype = STATIC_TYPES.get(item.suffix)
            if item.is_file() and ctype:
                files["/" + item.name] = (item.read_bytes(), ctype)
        if "/index.html" in files:
            files["/"] = files["/index.html"]
        return files

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", port, limit=MAX_LINE * 2)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- connection loop ---------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connections >= MAX_CONNECTIONS:
            writer.close()
            return
        self._connections += 1
        try:
            while True:
                try:
                    request = await self._read_request(reader)
                except HttpError as err:
                    # The body (if any) was not read, so the connection cannot be reused.
                    await self._send_json(writer, err.status, {"error": err.message}, keep_alive=False)
                    return
                if request is None:
                    return
                keep_alive = request.headers.get("connection", "").lower() != "close"
                if not await self._dispatch(request, writer, keep_alive):
                    return
                if not keep_alive:
                    return
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, ValueError):
            pass
        finally:
            self._connections -= 1
            writer.close()

    async def _read_head(self, reader: asyncio.StreamReader) -> Optional[tuple[str, str, dict[str, str]]]:
        # Long wait for the next request line on an idle keep-alive connection,
        # then a short deadline for the whole header block (slowloris guard).
        line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
        if not line:
            return None
        if len(line) > MAX_LINE:
            raise HttpError(431, "Baris permintaan terlalu panjang.")
        parts = line.decode("latin-1").split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise HttpError(400, "Permintaan tidak sah.")
        headers = await asyncio.wait_for(self._read_headers(reader), HEADER_TIMEOUT)
        return parts[0].upper(), parts[1], headers

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            raw = await reader.readline()
            if raw in (b"\r\n", b"\n", b""):
                return headers
            if len(headers) >= MAX_HEADERS:
                raise HttpError(431, "Header terlalu banyak.")
            name, sep, value = raw.decode("latin-1").partition(":")
            if not sep:
                raise HttpError(400, "Header tidak sah.")
            key = name.strip().lower()
            if key in headers:
                # Duplicate Host/Origin/token headers are a smuggling signal.
                raise HttpError(400, "Header ganda.")
            headers[key] = value.strip()

    async def _read_request(self, reader: asyncio.StreamReader) -> Optional[Request]:
        head = await self._read_head(reader)
        if head is None:
            return None
        method, target, headers = head
        if "transfer-encoding" in headers:
            raise HttpError(411, "Gunakan Content-Length.")
        path = target.split("?", 1)[0].split("#", 1)[0]
        request = Request(method, path, headers)
        # The gate runs on headers alone, BEFORE any body byte is read, so an
        # unauthenticated or cross-site sender can never make us buffer data.
        self.check(request)
        length_text = headers.get("content-length", "0")
        if not length_text.isdigit():
            raise HttpError(400, "Content-Length tidak sah.")
        length = int(length_text)
        if length:
            if length > self._body_limit(method, path):
                raise HttpError(413, "Data terlalu besar.")
            request.body = await asyncio.wait_for(reader.readexactly(length), BODY_TIMEOUT)
        return request

    def _body_limit(self, method: str, path: str) -> int:
        if method == "POST" and path in INPUT_ROUTES:
            return 4096
        if (method, path) in SIMPLE_ROUTES:
            return 1024
        api = self._api.get((method, path))
        return api[1] if api else 0

    # ---- the gate ----------------------------------------------------------
    def check(self, request: Request) -> None:
        """Raise HttpError unless the request passes the gate. Public for tests."""
        if request.headers.get("host", "") != f"127.0.0.1:{self.port}":
            raise HttpError(421, "Host tidak dikenal.")
        origin = request.headers.get("origin")
        if origin is not None and origin != self.origin:
            raise HttpError(403, "Origin ditolak.")
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            raise HttpError(403, "Permintaan lintas situs ditolak.")
        if request.method == "GET" and request.path in self._static:
            return
        token = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            raise HttpError(403, "Token sesi salah.")
        if request.method == "POST":
            ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, "Hanya application/json.")
        elif request.method != "GET":
            raise HttpError(405, "Metode tidak didukung.")

    async def _dispatch(self, request: Request, writer: asyncio.StreamWriter, keep_alive: bool) -> bool:
        try:
            key = (request.method, request.path)
            if request.method == "GET" and request.path in self._static:
                body, ctype = self._static[request.path]
                await self._send(writer, 200, body, ctype, keep_alive)
                return True
            if key == ("GET", "/m/stream.bin"):
                await self._proxy_stream(writer)
                return False
            if request.method == "POST" and request.path in INPUT_ROUTES:
                status = await self._require_upstream().send_input(INPUT_ROUTES[request.path], request.body)
                await self._send(writer, status, b"ok", "text/plain", keep_alive)
                return True
            if key in SIMPLE_ROUTES:
                await self._proxy_simple(request, SIMPLE_ROUTES[key], writer, keep_alive)
                return True
            api = self._api.get(key)
            if api is None:
                raise HttpError(404, "Tidak ada.")
            payload = await api[0](request)
            await self._send_json(writer, 200, payload, keep_alive)
            return True
        except HttpError as err:
            await self._send_json(writer, err.status, {"error": err.message}, keep_alive)
            return True
        except UpstreamError:
            await self._send_json(writer, 503, {"error": "Siaran belum siap."}, keep_alive)
            return True
        except Exception:  # noqa: BLE001
            log.exception("handler failed: %s %s", request.method, request.path)
            await self._send_json(writer, 500, {"error": "Terjadi kesalahan internal."}, keep_alive)
            return True

    # ---- writing -----------------------------------------------------------
    async def _send(self, writer, status: int, body: bytes, ctype: str, keep_alive: bool) -> None:
        writer.write(_head(status, [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Connection", "keep-alive" if keep_alive else "close"),
        ]) + body)
        await writer.drain()

    async def _send_json(self, writer, status: int, payload: dict, keep_alive: bool) -> None:
        await self._send(writer, status, json.dumps(payload).encode(), "application/json", keep_alive)

    # ---- engine proxy ------------------------------------------------------
    def _require_upstream(self):
        upstream = self.core.upstream
        if upstream is None:
            raise HttpError(503, "Belum terhubung ke iPhone.")
        return upstream

    async def _proxy_simple(self, request: Request, engine_path: str, writer, keep_alive: bool) -> None:
        response = await self._require_upstream().request(request.method, engine_path, request.body)
        ctype = response.headers.get("content-type", "application/octet-stream")
        await self._send(writer, response.status, response.body, ctype, keep_alive)

    async def _proxy_stream(self, writer: asyncio.StreamWriter) -> None:
        status, headers, up_reader, up_writer = await self._require_upstream().open_stream("/stream.bin")
        try:
            if status != 200 or headers.get("transfer-encoding", "").lower() != "chunked":
                body = await up_reader.read(64 * 1024)
                await self._send(writer, status if status != 200 else 502, body, "text/plain", False)
                return
            writer.write(_head(200, [
                ("Content-Type", "application/octet-stream"),
                ("Transfer-Encoding", "chunked"),
                ("Connection", "close"),
            ]))
            # The engine's body is already chunk-encoded; relay it untouched.
            while True:
                chunk = await up_reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            up_writer.close()

    # ---- API handlers ------------------------------------------------------
    async def _state(self, _: Request) -> dict:
        state = self.core.state.public()
        state["features"] = {"ocr": ocr.AVAILABLE, "audio": btaudio.AVAILABLE}
        state["audioDevice"] = self.audio.active_device if self.audio else None
        return state

    async def _connect(self, request: Request) -> dict:
        body = request.json()
        udid = self._udid(body)
        transport = body.get("transport", "usb")
        if transport not in ("usb", "wifi"):
            raise HttpError(400, "Jenis sambungan tidak sah.")
        try:
            await self.core.connect(udid, transport)
        except SessionError as exc:
            raise HttpError(409, str(exc)) from exc
        return {"ok": True}

    @staticmethod
    def _udid(body: dict) -> str:
        udid = body.get("udid")
        if not isinstance(udid, str) or not udid or len(udid) > 64 or not all(c.isalnum() or c == "-" for c in udid):
            raise HttpError(400, "UDID tidak sah.")
        return udid

    async def _wifi_enable(self, request: Request) -> dict:
        if not self.core.allow_wifi:
            raise HttpError(409, "Izinkan sambungan Wi-Fi di Pengaturan dulu.")
        udid = self._udid(request.json())
        try:
            await self.core.enable_wifi(udid)
        except Exception as exc:  # noqa: BLE001
            log.warning("wifi enable failed: %s", type(exc).__name__, exc_info=True)
            raise HttpError(409, explain(exc)) from exc
        return {"ok": True}

    async def _wifi_revoke(self, request: Request) -> dict:
        udid = self._udid(request.json())
        removed = await self.core.revoke_wifi(udid)
        return {"ok": True, "removed": removed}

    async def _disconnect(self, _: Request) -> dict:
        self.core.state.phase = "idle"
        await self.core.disconnect()
        return {"ok": True}

    async def _reveal(self, request: Request) -> dict:
        udid = request.json().get("udid")
        if not isinstance(udid, str) or not any(d["udid"] == udid for d in self.core.state.devices):
            raise HttpError(400, "Pilih iPhone yang tersambung.")
        try:
            await self.core.reveal_developer_mode(udid)
        except Exception as exc:  # noqa: BLE001
            raise HttpError(409, explain(exc)) from exc
        return {"ok": True}

    async def _get_settings(self, _: Request) -> dict:
        return self.settings.snapshot()

    async def _set_settings(self, request: Request) -> dict:
        try:
            saved = self.settings.update(request.json())
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        self.core.allow_wifi = bool(saved.get("allowWifi"))
        if not self.core.allow_wifi and self.core.transport == "wifi" and self.core.state.phase != "idle":
            await self.core.disconnect()  # turning Wi-Fi off ends a Wi-Fi session at once
        return saved

    async def _clip_to_phone(self, _: Request) -> dict:
        try:
            text = winclip.get_text()
        except winclip.ClipboardUnavailable as exc:
            raise HttpError(409, str(exc)) from exc
        if not text:
            raise HttpError(409, "Clipboard PC kosong atau bukan teks.")
        body = json.dumps({"text": text}).encode()
        response = await self._require_upstream().request("POST", "/clipboard", body)
        if response.status != 200:
            raise HttpError(502, "iPhone menolak isi clipboard.")
        return {"ok": True, "chars": len(text)}

    async def _clip_from_phone(self, _: Request) -> dict:
        response = await self._require_upstream().request("GET", "/clipboard")
        if response.status != 200:
            raise HttpError(502, "Clipboard iPhone tidak bisa dibaca.")
        try:
            text = json.loads(response.body).get("text")
        except (ValueError, AttributeError):
            text = None
        if not isinstance(text, str) or not text:
            raise HttpError(409, "Clipboard iPhone kosong atau berisi gambar.")
        try:
            winclip.set_text(text)
        except winclip.ClipboardUnavailable as exc:
            raise HttpError(409, str(exc)) from exc
        return {"ok": True, "chars": len(text)}

    async def _ocr(self, request: Request) -> dict:
        data = request.json().get("png")
        if not isinstance(data, str):
            raise HttpError(400, "Gambar tidak ada.")
        try:
            png = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HttpError(400, "Gambar rusak.") from exc
        try:
            text = await ocr.recognize_png(png)
        except ocr.OcrUnavailable as exc:
            raise HttpError(409, str(exc)) from exc
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        if not text:
            return {"ok": True, "chars": 0}
        try:
            winclip.set_text(text)
        except winclip.ClipboardUnavailable as exc:
            raise HttpError(409, str(exc)) from exc
        return {"ok": True, "chars": len(text), "lines": text.count("\n") + 1}

    async def _audio_devices(self, _: Request) -> dict:
        if not self.audio:
            raise HttpError(409, "Audio Bluetooth tidak tersedia.")
        try:
            return {"devices": await self.audio.devices(), "active": self.audio.active_device}
        except btaudio.AudioError as exc:
            raise HttpError(409, str(exc)) from exc

    async def _audio_toggle(self, request: Request) -> dict:
        if not self.audio:
            raise HttpError(409, "Audio Bluetooth tidak tersedia.")
        device_id = request.json().get("device")
        try:
            if device_id is None:
                await self.audio.stop()
            elif isinstance(device_id, str) and len(device_id) < 512:
                await self.audio.start(device_id)
            else:
                raise HttpError(400, "Perangkat tidak sah.")
        except btaudio.AudioError as exc:
            raise HttpError(409, str(exc)) from exc
        return {"ok": True, "active": self.audio.active_device}

    # ---- self-update -------------------------------------------------------
    async def _update_status(self, _: Request) -> dict:
        supported, reason = updater.support()
        return {
            "current": __version__,
            "supported": supported,
            "reason": reason,
            "available": self._release.version if self._release else None,
        }

    async def _update_check(self, _: Request) -> dict:
        supported, reason = updater.support()
        if not supported:
            raise HttpError(409, reason)
        try:
            self._release = await asyncio.to_thread(updater.check)
        except updater.UpdateError as exc:
            raise HttpError(502, str(exc)) from exc
        return await self._update_status(_)

    async def _update_install(self, _: Request) -> dict:
        if self._release is None:
            raise HttpError(409, "Periksa pembaruan dulu.")
        if self._update_busy:
            raise HttpError(409, "Pembaruan sedang berjalan.")
        self._update_busy = True
        try:
            staged = await asyncio.to_thread(updater.stage, self._release)
        except updater.UpdateError as exc:
            raise HttpError(502, str(exc)) from exc
        except OSError as exc:
            raise HttpError(500, "File pembaruan tidak bisa disimpan.") from exc
        finally:
            self._update_busy = False
        self.core.allow_wifi = bool(self.settings.snapshot().get("allowWifi"))
        await self.core.disconnect()
        if self._on_update_ready:
            # Reply first, then hand over; the window closes a moment later.
            asyncio.get_running_loop().call_later(0.5, self._on_update_ready, staged)
        return {"ok": True, "version": self._release.version}
