"""Text recognition with the Windows OCR engine, run in-process via WinRT.

The browser sends a PNG of the region the user selected. Paragraphs are
rebuilt from line geometry: a vertical gap clearly larger than the usual
line spacing starts a new paragraph, and a line ending in a hyphen is
joined to the next one.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

try:  # Optional: only present on Windows with the winrt packages installed.
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure just disables the feature
    AVAILABLE = False


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class Line:
    text: str
    top: float
    bottom: float
    left: float


def build_text(lines: list[Line]) -> str:
    """Join OCR lines into text with paragraph breaks. Pure function, testable."""
    lines = [line for line in lines if line.text.strip()]
    if not lines:
        return ""
    lines.sort(key=lambda line: (round(line.top), line.left))
    heights = [line.bottom - line.top for line in lines if line.bottom > line.top]
    typical = statistics.median(heights) if heights else 16.0

    paragraphs: list[str] = []
    current = lines[0].text.strip()
    for previous, line in zip(lines, lines[1:]):
        gap = line.top - previous.bottom
        text = line.text.strip()
        if gap > typical * 0.9:
            paragraphs.append(current)
            current = text
        elif current.endswith("-") and text[:1].islower():
            current = current[:-1] + text
        else:
            current = f"{current}\n{text}"
    paragraphs.append(current)
    return "\n\n".join(paragraphs)


async def _recognize(png: bytes) -> list[Line]:
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrUnavailable(
            "Windows belum punya paket OCR untuk bahasa yang dipakai. "
            "Tambahkan lewat Settings > Time & language > Language."
        )
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)

    lines: list[Line] = []
    for ocr_line in result.lines:
        rects = [word.bounding_rect for word in ocr_line.words]
        if not rects:
            continue
        lines.append(
            Line(
                text=ocr_line.text,
                top=min(r.y for r in rects),
                bottom=max(r.y + r.height for r in rects),
                left=min(r.x for r in rects),
            )
        )
    return lines


def _run_blocking(png: bytes) -> str:
    # WinRT async objects are awaited on a private loop in a worker thread so
    # the device loop never blocks on OCR.
    return build_text(asyncio.run(_recognize(png)))


async def recognize_png(png: bytes) -> str:
    if not AVAILABLE:
        raise OcrUnavailable("OCR butuh paket winrt (lihat requirements.txt).")
    if not png.startswith(PNG_MAGIC):
        raise ValueError("Gambar harus PNG.")
    return await asyncio.to_thread(_run_blocking, png)
