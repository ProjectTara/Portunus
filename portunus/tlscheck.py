"""Self-check: can this build open the TLS-PSK channel the Wi-Fi tunnel uses?

pymobiledevice3 opens the Wi-Fi TCP tunnel with TLS 1.2 and PSK cipher suites.
On Python 3.13+ that is the standard ``ssl`` module (set_psk_client_callback);
older Pythons needed the third-party sslpsk_pmd3, whose Windows wheel links an
OpenSSL 1.1 DLL that Python does not ship, so it could never load.

psk_tls_ok() runs a real loopback handshake with the same settings as the
library (TLS 1.2 only, ciphers "PSK", no certificate, client PSK callback) and
checks that a message crosses the encrypted channel.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import ssl
import sys
import threading


def psk_tls_ok(timeout: float = 5.0) -> tuple[bool, str]:
    if sys.version_info < (3, 13):
        return False, f"Python {sys.version.split()[0]} has no built-in TLS-PSK (needs 3.13+)"
    key = secrets.token_bytes(32)
    message = secrets.token_bytes(16)

    def client_ctx() -> ssl.SSLContext:  # mirrors tunnel_service.start_tcp_tunnel on 3.13+
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("PSK")
        ctx.set_psk_client_callback(lambda hint: (None, key))
        return ctx

    def server_ctx() -> ssl.SSLContext:  # plays the iPhone's side
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("PSK")
        ctx.set_psk_server_callback(lambda identity: key)
        return ctx

    try:
        sctx, cctx = server_ctx(), client_ctx()
    except (ssl.SSLError, AttributeError, ValueError) as exc:
        return False, f"PSK not available in {ssl.OPENSSL_VERSION}: {exc}"

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(timeout)
    received: list[bytes] = []
    errors: list[str] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            conn.settimeout(timeout)
            with sctx.wrap_socket(conn, server_side=True) as tls:
                received.append(tls.recv(64))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"server: {exc}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    async def client() -> str:
        # Same call the library makes: asyncio.open_connection(sock=..., ssl=ctx, server_hostname="")
        sock = socket.create_connection(listener.getsockname(), timeout=timeout)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(sock=sock, ssl=cctx, server_hostname=""), timeout)
        cipher = writer.get_extra_info("cipher")[0]
        writer.write(message)
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
        return cipher

    try:
        cipher = asyncio.run(client())
        thread.join(timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"client: {type(exc).__name__}: {exc}"
    finally:
        listener.close()
    if errors:
        return False, errors[0]
    if received != [message]:
        return False, "message did not arrive over the PSK channel"
    return True, f"{cipher} via {ssl.OPENSSL_VERSION}"
