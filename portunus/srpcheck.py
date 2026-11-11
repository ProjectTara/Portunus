"""Self-check: can this build complete Apple's Pair-Setup SRP with the library?

pymobiledevice3 11.45 pairs with srptools' 1.x API:
    SRPClientSession(...).process(B.hex(), salt.hex())
    assert session.verify_proof(server_proof.hex().encode())
srptools 2.0.0 changed verify_proof(), which made every Wi-Fi pair-setup fail
with a bare AssertionError. pairing_math_ok() plays the iPhone's side with the
Apple math (3072-bit group, SHA-512, user "Pair-Setup", integers as minimal
big-endian bytes, padded only inside k and u) and runs the client exactly as
the library does. Used by the CI self-test and before "Aktifkan Wi-Fi".
"""

from __future__ import annotations

import binascii
import hashlib
import secrets

try:
    from srptools import SRPClientSession, SRPContext
    from srptools.constants import PRIME_3072, PRIME_3072_GEN

    HAVE_SRPTOOLS = True
except ImportError:
    HAVE_SRPTOOLS = False

N = int(PRIME_3072, 16) if HAVE_SRPTOOLS else 0
G = 5
USER = "Pair-Setup"


def _b(v: int) -> bytes:
    return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")


def _pad(v: int) -> bytes:
    return v.to_bytes((N.bit_length() + 7) // 8, "big")


def _h(*parts: bytes) -> bytes:
    return hashlib.sha512(b"".join(parts)).digest()


def _hi(*parts: bytes) -> int:
    return int.from_bytes(_h(*parts), "big")


class AppleStyleDevice:
    """The Pair-Setup server side (what the iPhone computes), PIN 000000."""

    def __init__(self, pin: str = "000000") -> None:
        self.salt = secrets.token_bytes(16)
        x = _hi(self.salt, _h(f"{USER}:{pin}".encode()))
        self.v = pow(G, x, N)
        self.b = secrets.randbits(1024)
        k = _hi(_b(N), _pad(G))
        self.B = (k * self.v + pow(G, self.b, N)) % N

    def finish(self, A_bytes: bytes, M_client: bytes) -> tuple[bool, bytes]:
        A = int.from_bytes(A_bytes, "big")
        u = _hi(_pad(A), _pad(self.B))
        K = _h(_b(pow(A * pow(self.v, u, N), self.b, N)))
        group = _hi(_b(N)) ^ _hi(_b(G))
        M = _h(_b(group), _b(_hi(USER.encode())), self.salt, _b(A), _b(self.B), K)
        return M == M_client, _h(_b(A), M, K)



def pairing_math_ok(rounds: int = 3) -> bool:
    if not HAVE_SRPTOOLS:
        return False
    for _ in range(rounds):
        device = AppleStyleDevice()
        client = SRPClientSession(SRPContext(USER, password="000000", prime=PRIME_3072,
                                             generator=PRIME_3072_GEN, hash_func=hashlib.sha512))
        client.process(_b(device.B).hex(), device.salt.hex())
        accepted, server_proof = device.finish(binascii.unhexlify(client.public), binascii.unhexlify(client.key_proof))
        if not accepted or not client.verify_proof(server_proof.hex().encode()):
            return False
    return True
