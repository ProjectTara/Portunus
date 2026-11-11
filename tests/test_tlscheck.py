"""Wi-Fi tunnels need TLS-PSK from the standard ssl module (Python 3.13+)."""

import sys
import unittest

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
