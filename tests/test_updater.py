"""Tests for the signed self-updater. No network: GitHub is simulated."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from portunus import updater
from portunus.updater import UpdateError


def keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, base64.b64encode(public).decode()


def build_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def fake_github(private, version: str, zip_bytes: bytes, *, manifest_override=None):
    manifest = manifest_override or {
        "version": version,
        "file": updater.ZIP_NAME,
        "size": len(zip_bytes),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
    }
    raw = json.dumps(manifest).encode()
    files = {
        "https://github.com/o/r/m": raw,
        "https://github.com/o/r/s": private.sign(raw),
        "https://github.com/o/r/z": zip_bytes,
    }
    release = json.dumps({"assets": [
        {"name": updater.MANIFEST_NAME, "browser_download_url": "https://github.com/o/r/m"},
        {"name": updater.SIG_NAME, "browser_download_url": "https://github.com/o/r/s"},
        {"name": updater.ZIP_NAME, "browser_download_url": "https://github.com/o/r/z"},
    ]}).encode()

    def get(url, limit, accept="application/octet-stream"):
        updater._check_url(url)
        data = release if "api.github.com" in url else files[url]
        if len(data) > limit:
            raise UpdateError("too big")
        return data

    return get


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        for bad in ("1.2", "1.2.3.4", "1.x.3", "", "v1.2.-3"):
            with self.assertRaises(UpdateError):
                updater.parse_version(bad)


class UrlTests(unittest.TestCase):
    def test_only_https_github_hosts(self):
        updater._check_url("https://objects.githubusercontent.com/x")
        for bad in ("http://github.com/x", "https://github.com.evil.example/x", "https://evil.example/x",
                    "file:///C:/x", "https://user@evil.example/x"):
            with self.assertRaises(UpdateError, msg=bad):
                updater._check_url(bad)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = keypair()
        self.raw = json.dumps({"version": "1.0.0", "file": updater.ZIP_NAME, "size": 10, "sha256": "a" * 64}).encode()

    def test_valid(self):
        self.assertEqual(updater.verify_manifest(self.raw, self.private.sign(self.raw), self.public)["version"], "1.0.0")

    def test_wrong_key(self):
        other, _ = keypair()
        with self.assertRaises(UpdateError):
            updater.verify_manifest(self.raw, other.sign(self.raw), self.public)

    def test_tampered(self):
        signature = self.private.sign(self.raw)
        with self.assertRaises(UpdateError):
            updater.verify_manifest(self.raw.replace(b"1.0.0", b"9.0.0"), signature, self.public)

    def test_signed_but_malformed(self):
        raw = json.dumps({"version": "1.0.0", "file": "evil.exe", "size": 10, "sha256": "a" * 64}).encode()
        with self.assertRaises(UpdateError):
            updater.verify_manifest(raw, self.private.sign(raw), self.public)

    def test_no_key_means_no_updates(self):
        with mock.patch.object(updater, "PUBLIC_KEY", ""):
            with self.assertRaises(UpdateError):
                updater.verify_manifest(self.raw, self.private.sign(self.raw))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = keypair()
        self.zip = build_zip({"Portunus/Portunus.exe": b"MZ"})
        patcher = mock.patch.object(updater, "repository", return_value="o/r")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, remote: str, current: str, **kw):
        with mock.patch.object(updater, "_get", fake_github(self.private, remote, self.zip, **kw)):
            return updater.check(current, self.public)

    def test_newer_is_offered(self):
        release = self.run_check("0.2.0", "0.1.1")
        self.assertEqual(release.version, "0.2.0")

    def test_same_or_older_is_not(self):
        self.assertIsNone(self.run_check("0.1.1", "0.1.1"))
        self.assertIsNone(self.run_check("0.1.0", "0.1.1"))  # no downgrade

    def test_unsigned_relabel_rejected(self):
        # Attacker swaps in a manifest with a higher version but cannot re-sign it.
        good = fake_github(self.private, "0.2.0", self.zip)
        evil_raw = json.dumps({"version": "9.9.9", "file": updater.ZIP_NAME, "size": 2, "sha256": "b" * 64}).encode()

        def get(url, limit, accept="application/octet-stream"):
            return evil_raw if url.endswith("/m") else good(url, limit, accept)

        with mock.patch.object(updater, "_get", get):
            with self.assertRaises(UpdateError):
                updater.check("0.1.1", self.public)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def release_for(self, data: bytes, **override) -> updater.Release:
        values = {"version": "0.2.0", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "zip_url": "https://github.com/z"}
        values.update(override)
        return updater.Release(**values)

    def test_good_zip_staged(self):
        data = build_zip({"Portunus/Portunus.exe": b"MZ", "Portunus/_internal/a.dll": b"x"})
        with mock.patch.object(updater, "_get", return_value=data):
            app = updater.stage(self.release_for(data))
        self.assertTrue((app / "Portunus.exe").is_file())
        self.assertTrue(str(app).startswith(self.tmp.name))

    def test_hash_mismatch_rejected(self):
        data = build_zip({"Portunus/Portunus.exe": b"MZ"})
        with mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(UpdateError):
                updater.stage(self.release_for(data, sha256="0" * 64))

    def test_zip_slip_rejected(self):
        for evil in ("../evil.exe", "Portunus/../../evil.exe", "/abs.exe", "C:evil.exe", "Portunus/x.exe:ads"):
            data = build_zip({"Portunus/Portunus.exe": b"MZ", evil: b"x"})
            with mock.patch.object(updater, "_get", return_value=data):
                with self.assertRaises(UpdateError, msg=evil):
                    updater.stage(self.release_for(data))
            self.assertFalse((Path(self.tmp.name).parent / "evil.exe").exists())

    def test_missing_exe_rejected(self):
        data = build_zip({"Portunus/readme.txt": b"x"})
        with mock.patch.object(updater, "_get", return_value=data):
            with self.assertRaises(UpdateError):
                updater.stage(self.release_for(data))


class ApplyUpdateTests(unittest.TestCase):
    """The helper must never leave the user without a running app."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "Portunus"
        self.target.mkdir()
        (self.target / "Portunus.exe").write_bytes(b"old")
        self.staged = self.tmp / "staged" / "Portunus"
        self.staged.mkdir(parents=True)
        (self.staged / "Portunus.exe").write_bytes(b"new")
        import psutil
        patches = [
            mock.patch.object(psutil, "Process", side_effect=psutil.NoSuchProcess(1)),
            mock.patch.object(updater.sys, "executable", str(self.staged / "Portunus.exe")),
            mock.patch.object(updater.subprocess, "Popen"),
            mock.patch.object(updater.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_success_swaps_and_relaunches(self):
        self.assertEqual(updater.apply_update(self.target, 1), 0)
        self.assertEqual((self.target / "Portunus.exe").read_bytes(), b"new")
        updater.subprocess.Popen.assert_called_once()

    def test_locked_folder_relaunches_old_version(self):
        with mock.patch.object(updater.os, "replace", side_effect=PermissionError("locked")):
            self.assertEqual(updater.apply_update(self.target, 1), 3)
        self.assertEqual((self.target / "Portunus.exe").read_bytes(), b"old")
        updater.subprocess.Popen.assert_called_once()

    def test_failed_copy_rolls_back(self):
        with mock.patch.object(updater.shutil, "copytree", side_effect=OSError("disk full")):
            self.assertEqual(updater.apply_update(self.target, 1), 2)
        self.assertEqual((self.target / "Portunus.exe").read_bytes(), b"old")
        updater.subprocess.Popen.assert_called_once()


class UiStaticTests(unittest.TestCase):
    def test_hidden_attribute_wins_over_display_rules(self):
        css = (Path(__file__).resolve().parent.parent / "portunus" / "ui" / "style.css").read_text()
        self.assertIn("[hidden] { display: none !important; }", css)

    def test_screen_has_definite_height(self):
        # Without this the canvas ignores max-height and shows at native size.
        css = (Path(__file__).resolve().parent.parent / "portunus" / "ui" / "style.css").read_text()
        self.assertIn(".live { position: absolute; inset: 0;", css)

    def test_reflection_fits_below_canvas(self):
        import re
        root = Path(__file__).resolve().parent.parent / "portunus" / "ui"
        js = (root / "app.js").read_text()
        css = (root / "style.css").read_text()
        share = float(re.search(r"REFLECTION_SHARE = ([0-9.]+)", js).group(1))
        block = css[css.index("body.reflect #canvas {"):]
        visible = 1 - int(re.search(r"transparent (\d+)%", block).group(1)) / 100
        # space under a centered canvas must hold the visible part of the reflection
        self.assertLessEqual(share * visible, (1 - share) / 2)

    def test_canvas_size_not_left_to_css_clamping(self):
        css = (Path(__file__).resolve().parent.parent / "portunus" / "ui" / "style.css").read_text()
        block = css[css.index("#canvas {"):css.index("}", css.index("#canvas {"))]
        self.assertNotIn("max-height", block)
        self.assertNotIn("max-width", block)


class SupportTests(unittest.TestCase):
    def test_source_runs_do_not_self_update(self):
        supported, reason = updater.support()
        self.assertFalse(supported)
        self.assertIn("git pull", reason)


if __name__ == "__main__":
    unittest.main()
