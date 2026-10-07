"""A picture of another program's window, taken through the Windows API (ctypes, no dependencies).

Used for IPGMovie while a simulation runs: IPGMovie's own export stops it following the run, reading
the window's pixels does not. ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` also works when the window
is covered by others; a minimised window is restored first, without giving it the focus.
"""

from __future__ import annotations

import struct
import sys
import time
import zlib
from pathlib import Path


class CaptureError(Exception):
    pass


def png(bgra: bytes, width: int, height: int) -> bytes:
    """Encode top-down BGRA pixels as an RGB PNG."""
    rows = bytearray()
    for y in range(height):
        row = bgra[y * width * 4:(y + 1) * width * 4]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows += b"\x00" + rgb

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetDC.restype = wintypes.HDC
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _BitmapInfoHeader(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    def _exe_of(pid: int) -> str:
        handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            _kernel32.CloseHandle(handle)

    def _main_window(exe_name: str) -> int:
        """The largest visible top-level window of a program, by the name of its executable."""
        found: list[tuple[int, int]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            if _user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if Path(_exe_of(pid.value)).name.lower() == exe_name.lower():
                    r = wintypes.RECT()
                    _user32.GetWindowRect(hwnd, ctypes.byref(r))
                    found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            return True

        _user32.EnumWindows(each, 0)
        if not found:
            raise CaptureError(f"no window of {exe_name} was found")
        return max(found)[1]

    def capture_window(exe_name: str) -> tuple[bytes, int, int]:
        """PNG data, width and height of the program's main window (its content, without the frame)."""
        hwnd = _main_window(exe_name)
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: restore, the focus stays where it is
            time.sleep(0.8)
        r = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(r))
        width, height = r.right - r.left, r.bottom - r.top
        if width < 16 or height < 16:
            raise CaptureError(f"the window of {exe_name} has no visible content ({width} x {height})")
        wdc = _user32.GetDC(hwnd)
        mdc = _gdi32.CreateCompatibleDC(wdc)
        bmp = _gdi32.CreateCompatibleBitmap(wdc, width, height)
        old = _gdi32.SelectObject(mdc, bmp)
        try:
            ok = _user32.PrintWindow(hwnd, mdc, 1 | 2)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
            size = ctypes.sizeof(_BitmapInfoHeader)
            header = _BitmapInfoHeader(size, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down rows
            buf = ctypes.create_string_buffer(width * height * 4)
            rows = _gdi32.GetDIBits(mdc, bmp, 0, height, buf, ctypes.byref(header), 0)
        finally:
            _gdi32.SelectObject(mdc, old)
            _gdi32.DeleteObject(bmp)
            _gdi32.DeleteDC(mdc)
            _user32.ReleaseDC(hwnd, wdc)
        data = bytes(buf)
        if not ok or rows != height or not any(data[::997]):
            raise CaptureError(f"Windows returned no picture of the window of {exe_name}")
        return png(data, width, height), width, height

else:

    def capture_window(exe_name: str) -> tuple[bytes, int, int]:
        raise CaptureError("window pictures need Windows")
