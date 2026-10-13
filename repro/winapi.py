"""ctypes bindings for the handful of Win32 APIs Repro relies on."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
shell32 = ctypes.WinDLL("shell32")
ole32 = ctypes.WinDLL("ole32")

# --- DPI ---------------------------------------------------------------------

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_PER_MONITOR_DPI_AWARE = 2

user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL


def enable_dpi_awareness() -> None:
    """Work in physical pixels; otherwise a 200% (Retina) session reports half sizes."""
    if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        return
    try:
        shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except OSError:
        user32.SetProcessDPIAware()


# --- Monitors ----------------------------------------------------------------

MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
shcore.GetDpiForMonitor.argtypes = [
    wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)
]
shcore.GetDpiForMonitor.restype = ctypes.HRESULT


@dataclass(frozen=True)
class Monitor:
    # 1-based EnumDisplayMonitors order, the same numbering windows-capture uses.
    index: int
    left: int
    top: int
    width: int
    height: int
    dpi: int
    primary: bool
    # The part not covered by the taskbar: left, top, right, bottom in screen coordinates.
    work: tuple[int, int, int, int] | None = None

    @property
    def scale(self) -> float:
        """2.0 for a Retina RDP session (200%), 1.0 for a regular one."""
        return self.dpi / 96


def list_monitors() -> list[Monitor]:
    monitors: list[Monitor] = []

    def add(handle, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(handle, ctypes.byref(info))
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        try:
            shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            dpi = dpi_x.value
        except OSError:
            dpi = 96
        rect, work = info.rcMonitor, info.rcWork
        monitors.append(Monitor(
            index=len(monitors) + 1,
            left=rect.left,
            top=rect.top,
            width=rect.right - rect.left,
            height=rect.bottom - rect.top,
            dpi=dpi,
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            work=(work.left, work.top, work.right, work.bottom),
        ))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(add), 0)
    return monitors


# --- GDI drawing surface -----------------------------------------------------

BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]


class Dib:
    """A 32-bit top-down DIB section in a memory DC, exposed as a BGRA numpy array."""

    def __init__(self, width: int, height: int) -> None:
        screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        header = BITMAPINFOHEADER(
            biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height, biPlanes=1,
            biBitCount=32, biCompression=BI_RGB,
        )
        bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not self.bitmap:
            gdi32.DeleteDC(self.dc)
            raise ctypes.WinError(ctypes.get_last_error())
        self._previous = gdi32.SelectObject(self.dc, self.bitmap)
        buffer = (ctypes.c_uint8 * (width * height * 4)).from_address(bits.value)
        self.pixels = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 4)

    def copy_from_screen(self, x: int, y: int) -> None:
        height, width = self.pixels.shape[:2]
        screen = user32.GetDC(None)
        try:
            gdi32.BitBlt(self.dc, 0, 0, width, height, screen, x, y, SRCCOPY | CAPTUREBLT)
        finally:
            user32.ReleaseDC(None, screen)
        gdi32.GdiFlush()

    def close(self) -> None:
        gdi32.SelectObject(self.dc, self._previous)
        gdi32.DeleteObject(self.bitmap)
        gdi32.DeleteDC(self.dc)

    def __enter__(self) -> Dib:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --- Mouse cursor ------------------------------------------------------------

CURSOR_SHOWING = 0x1
DI_NORMAL = 0x3


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
user32.DrawIconEx.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
    wintypes.UINT, wintypes.HBRUSH, wintypes.UINT,
]


def cursor_state() -> tuple[int, int, int] | None:
    """(handle, x, y) of the visible pointer in physical screen coordinates."""
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & CURSOR_SHOWING or not info.hCursor:
        return None
    return info.hCursor, info.ptScreenPos.x, info.ptScreenPos.y


def draw_cursor(handle: int) -> tuple[np.ndarray, np.ndarray, int, int] | None:
    """Renders the cursor over black and over white; the pair reveals its alpha and XOR pixels."""
    info = ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(info)):
        return None
    try:
        bitmap = BITMAP()
        gdi32.GetObjectW(info.hbmColor or info.hbmMask, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
        width, height = bitmap.bmWidth, bitmap.bmHeight
        if not info.hbmColor:
            height //= 2  # monochrome cursors stack the AND mask over the XOR mask
    finally:
        for bitmap_handle in (info.hbmMask, info.hbmColor):
            if bitmap_handle:
                gdi32.DeleteObject(bitmap_handle)
    if width <= 0 or height <= 0:
        return None

    layers = []
    for background in (0, 255):
        with Dib(width, height) as dib:
            dib.pixels[:] = background
            user32.DrawIconEx(dib.dc, 0, 0, handle, width, height, 0, None, DI_NORMAL)
            gdi32.GdiFlush()
            layers.append(dib.pixels.copy())
    return layers[0], layers[1], info.xHotspot, info.yHotspot


# --- Window styles -----------------------------------------------------------

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2
GA_ROOT = 2
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOACTIVATE = 0x10

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
]


def make_click_through(tk_window_id: int) -> None:
    """Lets mouse clicks fall through a Tk toplevel to whatever lies beneath it."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(
        hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    )
    # A layered window stays invisible until its attributes are set.
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)


def bring_to_top(tk_window_id: int) -> None:
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# --- Per-pixel alpha windows -------------------------------------------------

AC_SRC_OVER = 0x0
AC_SRC_ALPHA = 0x1
ULW_ALPHA = 0x2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class UPDATELAYEREDWINDOWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hdcDst", wintypes.HDC),
        ("pptDst", ctypes.POINTER(wintypes.POINT)),
        ("psize", ctypes.POINTER(wintypes.SIZE)),
        ("hdcSrc", wintypes.HDC),
        ("pptSrc", ctypes.POINTER(wintypes.POINT)),
        ("crKey", wintypes.COLORREF),
        ("pblend", ctypes.POINTER(BLENDFUNCTION)),
        ("dwFlags", wintypes.DWORD),
        ("prcDirty", ctypes.POINTER(wintypes.RECT)),
    ]


user32.UpdateLayeredWindowIndirect.argtypes = [wintypes.HWND, ctypes.POINTER(UPDATELAYEREDWINDOWINFO)]
dwmapi = ctypes.WinDLL("dwmapi")
dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def make_layered(tk_window_id: int) -> int:
    """Turns a Tk toplevel into a per-pixel alpha window drawn by update_layered(). Tk still
    gets its mouse events; clicks do not activate it, so keyboard focus stays where it was."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    return hwnd


def update_layered(hwnd: int, dib: Dib, x: int, y: int, dirty: tuple[int, int, int, int] | None = None) -> None:
    """Shows the DIB's premultiplied BGRA pixels at (x, y). With `dirty` (left, top, right,
    bottom) only that part is sent, which is several times faster for a 4K window."""
    height, width = dib.pixels.shape[:2]
    position, size, origin = wintypes.POINT(x, y), wintypes.SIZE(width, height), wintypes.POINT(0, 0)
    blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    info = UPDATELAYEREDWINDOWINFO(
        ctypes.sizeof(UPDATELAYEREDWINDOWINFO), None, ctypes.pointer(position), ctypes.pointer(size), dib.dc,
        ctypes.pointer(origin), 0, ctypes.pointer(blend), ULW_ALPHA,
        ctypes.pointer(wintypes.RECT(*dirty)) if dirty else None,
    )
    gdi32.GdiFlush()
    user32.UpdateLayeredWindowIndirect(hwnd, ctypes.byref(info))


def make_no_activate(tk_window_id: int) -> None:
    """Clicks on the window work but do not take focus from the app being recorded."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


WDA_EXCLUDEFROMCAPTURE = 0x11
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]


def exclude_from_capture(tk_window_id: int) -> bool:
    """Keeps the window out of screen capture (Windows 10 2004+); False where unsupported."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
WS_EX_APPWINDOW = 0x40000
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT


def visible_windows() -> list[tuple[int, int, int, int]]:
    """Bounds (left, top, right, bottom) of the windows a user can see, front to back; not
    this app's own windows, nor the desktop and taskbars."""
    own_pid = kernel32.GetCurrentProcessId()
    found = []

    def add(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if name.value in SHELL_CLASSES or (style & WS_EX_TOOLWINDOW and not style & WS_EX_APPWINDOW):
            return True
        cloaked = wintypes.DWORD()
        try:  # hidden UWP windows and windows on other virtual desktops are "cloaked"
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        except OSError:
            pass
        if cloaked.value:
            return True
        rect = wintypes.RECT()
        try:  # the visible frame, without the invisible resize borders of GetWindowRect
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
        except OSError:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left >= 20 and rect.bottom - rect.top >= 20:
            found.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumWindows(WNDENUMPROC(add), 0)
    return found


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


# --- Keyboard and mouse state ------------------------------------------------

SM_SWAPBUTTON = 23
MAPVK_VK_TO_VSC = 0
TOUNICODE_KEEP_STATE = 0x4  # Windows 10 1607+: leave the dead-key state alone

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.MapVirtualKeyExW.restype = wintypes.UINT
user32.ToUnicodeEx.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR, ctypes.c_int, wintypes.UINT,
    ctypes.c_void_p,
]
user32.ToUnicodeEx.restype = ctypes.c_int


def key_state(vk: int) -> tuple[bool, bool]:
    """(down now, pressed since the previous poll) for a key or physical mouse button."""
    state = user32.GetAsyncKeyState(vk)
    return bool(state & 0x8000), bool(state & 1)


def caps_lock_on() -> bool:
    return bool(user32.GetKeyState(0x14) & 1)


def mouse_buttons_swapped() -> bool:
    return bool(user32.GetSystemMetrics(SM_SWAPBUTTON))


def window_process(hwnd: int | None) -> int:
    pid = wintypes.DWORD()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_process() -> int:
    return window_process(user32.GetForegroundWindow())


def process_at(x: int, y: int) -> int:
    return window_process(user32.WindowFromPoint(wintypes.POINT(x, y)))


def foreground_layout() -> int | None:
    """The keyboard layout of the window being typed into (Ukrainian, English, ...)."""
    hwnd = user32.GetForegroundWindow()
    thread = user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
    return user32.GetKeyboardLayout(thread)


def key_text(vk: int, shift: bool, caps: bool, altgr: bool, layout: int | None) -> str:
    """What the key types in that layout, or "" for dead and non-character keys."""
    state = (ctypes.c_ubyte * 256)()
    if shift:
        state[0x10] = state[0xA0] = 0x80
    if altgr:
        state[0x11] = state[0xA2] = state[0x12] = state[0xA5] = 0x80
    if caps:
        state[0x14] = 0x01
    buffer = ctypes.create_unicode_buffer(8)
    scan = user32.MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, layout)
    count = user32.ToUnicodeEx(vk, scan, state, buffer, len(buffer), TOUNICODE_KEEP_STATE, layout)
    return buffer.value[:count] if count > 0 else ""


# --- Low-level keyboard hook -------------------------------------------------

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),  # the character itself for VK_PACKET
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]


def round_corners(tk_window_id: int, border: str | None = None) -> None:
    """Windows 11 rounds popup windows only on request, `border` colours the thin line DWM draws
    around them; older systems ignore both."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    if border:
        _dwm_set(hwnd, DWMWA_BORDER_COLOR, _colorref(border))


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
SWP_NOZORDER = 0x4
SWP_FRAMECHANGED = 0x20


def style_title_bar(tk_window_id: int, dark: bool, caption: str, text: str, border: str) -> None:
    """A title bar in the window's own colours (Windows 11), or at least dark or light (Windows 10)."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark))
    for attribute, color in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text), (DWMWA_BORDER_COLOR, border)):
        _dwm_set(hwnd, attribute, _colorref(color))
    # Windows 10 repaints the frame only when told it changed.
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)


def _dwm_set(hwnd: int, attribute: int, value: int) -> None:
    data = ctypes.c_int(value)
    dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))


def _colorref(color: str) -> int:
    """'#rrggbb' as a COLORREF (0x00bbggrr)."""
    return int(color[5:7] + color[3:5] + color[1:3], 16)


# --- Scale of a window or a point ----------------------------------------------

MONITOR_DEFAULTTONEAREST = 2
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.AdjustWindowRectExForDpi.argtypes = [
    ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, wintypes.UINT
]


def window_scale(tk_window_id: int) -> float:
    """1.0 at 100%, 2.0 at 200%: the scale of the monitor the window is on."""
    dpi = user32.GetDpiForWindow(user32.GetAncestor(tk_window_id, GA_ROOT))
    return dpi / 96 if dpi else 1.0


def frame_size(tk_window_id: int, width: int, height: int, scale: float) -> tuple[int, int]:
    """The outer size of a window with a width × height client area on a monitor at this scale:
    with its title bar and borders, as Tk's `wm geometry` positions it."""
    hwnd = user32.GetAncestor(tk_window_id, GA_ROOT)
    rect = wintypes.RECT(0, 0, width, height)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    user32.AdjustWindowRectExForDpi(ctypes.byref(rect), style, False, ex_style, round(96 * scale))
    return rect.right - rect.left, rect.bottom - rect.top


def monitor_at(x: int, y: int) -> tuple[tuple[int, int, int, int], float]:
    """The work area (left, top, right, bottom) and the scale of the monitor nearest a point."""
    handle = user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(handle, ctypes.byref(info))
    dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
    try:
        shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    except OSError:
        pass
    work = info.rcWork
    return (work.left, work.top, work.right, work.bottom), dpi_x.value / 96


# --- Global hotkeys ----------------------------------------------------------

MOD_ALT = 0x1
MOD_CONTROL = 0x2
MOD_SHIFT = 0x4
MOD_WIN = 0x8
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_USER = 0x0400
PM_NOREMOVE = 0x0

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

# --- Process helpers ---------------------------------------------------------

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x2
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_instance_mutex = None


def claim_single_instance(name: str) -> bool:
    """False when another copy already runs in this session (its hotkeys would clash)."""
    global _instance_mutex
    _instance_mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def create_event(name: str) -> int:
    """An auto-reset named event that another process can fire with set_event()."""
    return kernel32.CreateEventW(None, False, False, name)


def wait_event(handle: int) -> bool:
    return kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0


ASFW_ANY = 0xFFFFFFFF
user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]


def set_event(name: str) -> bool:
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    # This process was just launched by the user, so it may hand the right to take the
    # foreground (and keyboard focus) to the instance that will react to the event.
    user32.AllowSetForegroundWindow(ASFW_ANY)
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


FOLDERID_VIDEOS = GUID(0x18989B1D, 0x99B5, 0x455B, (ctypes.c_ubyte * 8)(0x84, 0x1C, 0xAB, 0x7C, 0x74, 0xE4, 0xDD, 0xFC))
shell32.SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)
]
shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_wchar_p]


def videos_folder() -> str:
    """The user's Videos folder, following OneDrive or GPO redirection."""
    path = ctypes.c_wchar_p()
    try:
        shell32.SHGetKnownFolderPath(ctypes.byref(FOLDERID_VIDEOS), 0, None, ctypes.byref(path))
        return path.value
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Videos")
    finally:
        ole32.CoTaskMemFree(path)
