"""Plain-text access to the Windows clipboard through the Win32 API.

Kept in-process (no PowerShell, no browser clipboard permission). On other
platforms both functions raise ClipboardUnavailable so the rest of the app
can still be imported and tested.
"""

from __future__ import annotations

import ctypes
import sys
import time

MAX_CHARS = 1_000_000
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


class ClipboardUnavailable(RuntimeError):
    pass


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _open() -> None:
    # Another program may hold the clipboard briefly; retry for ~0.5 s.
    for _ in range(25):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise ClipboardUnavailable("Clipboard Windows sedang dipakai program lain.")


def get_text() -> str | None:
    if sys.platform != "win32":
        raise ClipboardUnavailable("Clipboard hanya tersedia di Windows.")
    _open()
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)[:MAX_CHARS]
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def set_text(text: str) -> None:
    if sys.platform != "win32":
        raise ClipboardUnavailable("Clipboard hanya tersedia di Windows.")
    text = text[:MAX_CHARS]
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    _open()
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            raise ClipboardUnavailable("Memori untuk clipboard tidak cukup.")
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            _kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("Clipboard tidak bisa dikunci.")
        ctypes.memmove(pointer, data, size)
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
            # Ownership only passes to the system on success.
            _kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("Clipboard menolak data.")
    finally:
        _user32.CloseClipboard()
