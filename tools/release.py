"""Package dist/Portunus into a signed release. Used by the GitHub workflow.

    python tools/release.py <tag> <dist_folder> <out_folder>

Reads the private key from the environment variable PORTUNUS_SIGNING_KEY.
Fails if the tag does not match portunus.__version__.
"""

import base64
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from portunus import __version__  # noqa: E402
from portunus.updater import MANIFEST_NAME, SIG_NAME, ZIP_NAME, verify_manifest  # noqa: E402
from portunus.update_key import PUBLIC_KEY  # noqa: E402


def main(tag: str, dist: Path, out: Path) -> None:
    version = tag.lstrip("v")
    if version != __version__:
        raise SystemExit(f"Tag {tag} tidak sama dengan __version__ {__version__}. Samakan dulu.")
    key_b64 = os.environ.get("PORTUNUS_SIGNING_KEY", "").strip()
    if not key_b64:
        raise SystemExit("Secret PORTUNUS_SIGNING_KEY belum diisi di GitHub.")
    if not PUBLIC_KEY:
        raise SystemExit("portunus/update_key.py masih kosong. Jalankan tools/keygen.py lalu commit.")
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))

    out.mkdir(parents=True, exist_ok=True)
    archive = out / ZIP_NAME
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                zf.write(path, Path("Portunus") / path.relative_to(dist))
    data = archive.read_bytes()
    manifest = json.dumps(
        {"version": version, "file": ZIP_NAME, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        indent=2,
    ).encode()
    signature = private.sign(manifest)
    verify_manifest(manifest, signature, PUBLIC_KEY)  # the committed key must match the secret
    (out / MANIFEST_NAME).write_bytes(manifest)
    (out / SIG_NAME).write_bytes(signature)
    print(f"OK {ZIP_NAME} {len(data)} bytes, ditandatangani untuk versi {version}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
