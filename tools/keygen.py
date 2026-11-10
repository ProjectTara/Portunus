"""Create the update signing key pair. Run once, on your own PC.

    python tools/keygen.py

Writes the PUBLIC key into portunus/update_key.py (commit this file) and
prints the PRIVATE key once. Put the private key in the GitHub secret
PORTUNUS_SIGNING_KEY and keep an offline backup (a password manager).
Never commit it. Losing it means already-installed copies can no longer
be updated automatically.
"""

import base64
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_FILE = Path(__file__).resolve().parent.parent / "portunus" / "update_key.py"


def main() -> None:
    text = KEY_FILE.read_text(encoding="utf-8")
    if re.search(r'PUBLIC_KEY = "[^"]+"', text):
        raise SystemExit("update_key.py sudah berisi kunci. Hapus manual dulu jika memang mau mengganti kunci.")
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    KEY_FILE.write_text(
        text.replace('PUBLIC_KEY = ""', f'PUBLIC_KEY = "{base64.b64encode(raw_public).decode()}"'),
        encoding="utf-8",
    )
    print("Kunci publik ditulis ke portunus/update_key.py")
    print()
    print("KUNCI PRIVAT (salin ke GitHub secret PORTUNUS_SIGNING_KEY, lalu simpan cadangan offline):")
    print(base64.b64encode(raw_private).decode())


if __name__ == "__main__":
    main()
