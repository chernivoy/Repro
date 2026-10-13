"""Tray icon; pystray runs its own Win32 message loop on a background thread."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile
import threading
import time
import winreg
from collections.abc import Callable

import pystray

from .icons import make_icon, save_ico
from .winapi import user32

log = logging.getLogger(__name__)

NOTIFY_ICON_SETTINGS = r"Control Panel\NotifyIconSettings"
SM_CXSMICON = 49
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
user32.LoadImageW.argtypes = [
    ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint
]
user32.LoadImageW.restype = ctypes.c_void_p


class _TrayIcon(pystray.Icon):
    """pystray gives Windows one picture to scale into the tray. This loads the tray's own size
    from the same multi-size icon as the exe, so the tray shows just what Explorer shows.
    (Overrides pystray 0.19's Windows backend, which keeps the handle in _icon_handle.)"""

    recording = False

    def _assert_icon_handle(self) -> None:
        if self._icon_handle:
            return
        size = user32.GetSystemMetrics(SM_CXSMICON)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tray.ico")
            save_ico(path, self.recording)
            self._icon_handle = user32.LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)


class Tray:
    def __init__(
        self,
        is_recording: Callable[[], bool],
        on_record: Callable[[], None],
        on_settings: Callable[[], None],
        on_open_folder: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        # Left click: stops a running recording, otherwise opens the capture overlay.
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda _item: "Зупинити запис" if is_recording() else "Записати…",
                lambda: on_record(),
                default=True,
            ),
            pystray.MenuItem("Налаштування", lambda: on_settings()),
            pystray.MenuItem("Відкрити папку з відео", lambda: on_open_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Вихід", lambda: on_quit()),
        )
        self._images = {False: make_icon(False), True: make_icon(True)}
        self._recording = False
        self._icon = _TrayIcon("Repro", self._images[False], "Repro", menu)
        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        if getattr(sys, "frozen", False):
            threading.Thread(target=_promote_icon, args=(sys.executable,), name="tray-promote", daemon=True).start()

    def set_recording(self, recording: bool) -> None:
        if recording != self._recording:
            self._recording = recording
            self._icon.recording = recording
            self._icon.icon = self._images[recording]
            self._icon.update_menu()

    def set_tooltip(self, text: str) -> None:
        self._icon.title = text

    def notify(self, message: str) -> None:
        self._icon.notify(message, "Repro")

    def stop(self) -> None:
        self._icon.stop()


def _promote_icon(executable: str) -> None:
    """Windows 11 tucks new tray icons away under the "^" overflow, where nobody finds them.
    Explorer records each icon under NotifyIconSettings shortly after it first appears; marking
    ours promoted shows it on the taskbar at once. An IsPromoted value already present is the
    user's own choice from Settings and is left alone. Only for the exe: from source the entry
    would be python.exe's, shared with other scripts."""
    for _ in range(20):
        try:
            if _promote_entry(executable):
                return
        except OSError as error:
            log.warning("Could not promote the tray icon: %s", error)
            return
        time.sleep(1)


def _promote_entry(executable: str) -> bool:
    """True once our entry exists (promoted now or earlier)."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_ICON_SETTINGS) as settings:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(settings, index)
            except OSError:
                return False
            index += 1
            with winreg.OpenKey(settings, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as entry:
                try:
                    path = winreg.QueryValueEx(entry, "ExecutablePath")[0]
                except OSError:
                    continue
                if not _same_executable(path, executable):
                    continue
                try:
                    winreg.QueryValueEx(entry, "IsPromoted")
                except FileNotFoundError:
                    winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    log.info("Tray icon promoted to the taskbar")
                return True


def _same_executable(stored: str, ours: str) -> bool:
    stored, ours = stored.lower(), ours.lower()
    if stored.startswith("{"):  # a known folder is stored as its GUID: {374DE290-...}\Repro.exe
        return ours.endswith(stored.partition("}")[2])
    return stored == ours
