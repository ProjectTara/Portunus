"""WCAG 2.1 AA contrast for the UI tokens, light and dark themes.

Text needs 4.5:1; borders of interactive controls (--edge) need 3:1 (1.4.11).
"""

import re
import unittest
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "portunus" / "ui" / "style.css").read_text()


def tokens(selector: str) -> dict[str, str]:
    start = CSS.index(selector)
    block = CSS[start:CSS.index("}", start)]
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", block))


def luminance(hex_color: str) -> float:
    c = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ContrastTests(unittest.TestCase):
    PAIRS = [("ink", "ground", 4.5), ("ink", "panel", 4.5), ("muted", "ground", 4.5), ("muted", "panel", 4.5),
             ("live", "ground", 3.0), ("live", "panel", 3.0), ("edge", "ground", 3.0), ("edge", "panel", 3.0)]

    def check(self, theme: dict[str, str], name: str) -> None:
        for fg, bg, need in self.PAIRS:
            with self.subTest(theme=name, pair=f"{fg}/{bg}"):
                self.assertGreaterEqual(ratio(theme[fg], theme[bg]), need)

    def test_light(self):
        self.check(tokens(":root {"), "terang")

    def test_dark(self):
        light = tokens(":root {")
        self.check({**light, **tokens(':root[data-theme="dark"]')}, "gelap")


if __name__ == "__main__":
    unittest.main()
