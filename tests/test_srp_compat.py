"""Wi-Fi pairing regression: the installed srptools must pass pymobiledevice3 11.45's checks.

See portunus/srpcheck.py. With srptools 2.0.0 this fails exactly like the
"Aktifkan Wi-Fi" AssertionError users saw; with 1.0.1 it passes.
"""

import unittest

from portunus import srpcheck


@unittest.skipUnless(srpcheck.HAVE_SRPTOOLS, "srptools not installed")
class SrpCompatTests(unittest.TestCase):
    def test_library_pairing_math_round_trip(self):
        self.assertTrue(srpcheck.pairing_math_ok(rounds=10),
                        "srptools rejects a valid device proof: Wi-Fi pairing would fail")


if __name__ == "__main__":
    unittest.main()
