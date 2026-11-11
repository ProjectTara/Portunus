"""Wi-Fi tunnels need TLS-PSK from the standard ssl module (Python 3.13+)."""

import asyncio
import sys
import unittest

from portunus import wifi
from portunus.tlscheck import psk_tls_ok


class TlsPskTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 13), "built-in TLS-PSK needs Python 3.13+")
    def test_loopback_handshake_like_the_library(self):
        ok, detail = psk_tls_ok()
        self.assertTrue(ok, detail)
        self.assertIn("PSK", detail)

    @unittest.skipUnless(sys.version_info < (3, 13), "only meaningful on older Pythons")
    def test_old_python_is_reported_not_crashed(self):
        ok, detail = psk_tls_ok()
        self.assertFalse(ok)
        self.assertIn("3.13", detail)


class TlsPskInsideAppLoopTests(unittest.IsolatedAsyncioTestCase):
    """The app calls the check from its running event loop (wifi.open_tunnel).
    0.3.3 used asyncio.run() there and always reported "TLS-PSK tidak tersedia"."""

    @unittest.skipIf(sys.version_info < (3, 13), "built-in TLS-PSK needs Python 3.13+")
    async def test_check_passes_from_running_loop(self):
        wifi._psk_ok = None
        ok, detail = await wifi.require_psk()
        self.assertTrue(ok, detail)

    @unittest.skipIf(sys.version_info < (3, 13), "built-in TLS-PSK needs Python 3.13+")
    async def test_direct_call_inside_loop_also_works(self):
        ok, detail = psk_tls_ok()  # even a direct call must not trip over the running loop
        self.assertTrue(ok, detail)
        self.assertTrue(asyncio.get_running_loop().is_running())
