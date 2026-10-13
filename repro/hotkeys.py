"""System-wide hotkeys via RegisterHotKey: no admin rights or keyboard hooks needed."""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

from . import winapi
from .winapi import kernel32, user32

_MODIFIERS = {
    "ctrl": winapi.MOD_CONTROL,
    "alt": winapi.MOD_ALT,
    "shift": winapi.MOD_SHIFT,
    "win": winapi.MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "pause": 0x13, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "printscreen": 0x2C, "scrolllock": 0x91,
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+alt+r' -> (modifiers, virtual key)."""
    *modifiers, key = [part.strip().lower() for part in text.split("+")]
    flags = 0
    for modifier in modifiers:
        if modifier not in _MODIFIERS:
            raise ValueError(f"Невідомий модифікатор: {modifier}")
        flags |= _MODIFIERS[modifier]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return flags, ord(key.upper())
    if key[:1] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return flags, 0x6F + int(key[1:])
    if key in _NAMED_KEYS:
        return flags, _NAMED_KEYS[key]
    raise ValueError(f"Невідома клавіша: {key}")


_DISPLAY_NAMES = {"pageup": "PgUp", "pagedown": "PgDn", "printscreen": "PrtSc", "scrolllock": "ScrollLock"}
_KEYS_ALONE = {"pause", "scrolllock"}  # rarely used for anything else, so fine without a modifier


def format_hotkey(text: str) -> str:
    """'ctrl+alt+pageup' -> 'Ctrl+Alt+PgUp'."""
    return "+".join(_DISPLAY_NAMES.get(part.strip().lower(), part.strip().capitalize()) for part in text.split("+"))


def same_hotkey(first: str, second: str) -> bool:
    try:
        return parse_hotkey(first) == parse_hotkey(second)
    except ValueError:
        return first.strip().lower() == second.strip().lower()


def hotkey_from_key(vk: int, ctrl: bool, alt: bool, shift: bool) -> str:
    """The hotkey for a key pressed with modifiers, as settings store it ('ctrl+alt+r').
    Raises ValueError, with a message for the user, for keys a hotkey cannot use."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        key = chr(vk).lower()
    elif 0x70 <= vk <= 0x87:
        key = f"f{vk - 0x6F}"
    else:
        key = next((name for name, code in _NAMED_KEYS.items() if code == vk), "")
    if not key:
        raise ValueError("Ця клавіша не підходить: виберіть літеру, цифру, F1–F24, Home, End, PgUp, Pause…")
    function_key = key[:1] == "f" and key[1:].isdigit()
    if not (ctrl or alt) and not function_key and key not in _KEYS_ALONE:
        raise ValueError("Додайте Ctrl чи Alt: без них ця клавіша заважала б друкувати.")
    return "+".join([name for held, name in ((ctrl, "ctrl"), (alt, "alt"), (shift, "shift")) if held] + [key])


class HotkeyListener:
    """Registers hotkeys on its own thread and reports presses by name."""

    def __init__(self, bindings: dict[str, str], on_press: Callable[[str], None]) -> None:
        self._bindings = bindings
        self._on_press = on_press
        self._ready = threading.Event()
        self._failed: list[str] = []
        self._thread_id = 0
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)

    def start(self) -> list[str]:
        """Returns the hotkeys that could not be registered (taken by another program or invalid)."""
        self._thread.start()
        self._ready.wait()
        return self._failed

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, winapi.WM_QUIT, 0, 0)
            self._thread.join(timeout=1)

    def _run(self) -> None:
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, winapi.WM_USER, winapi.WM_USER, winapi.PM_NOREMOVE)
        self._thread_id = kernel32.GetCurrentThreadId()

        names = list(self._bindings)
        for hotkey_id, name in enumerate(names, start=1):
            try:
                modifiers, key = parse_hotkey(self._bindings[name])
            except ValueError:
                self._failed.append(self._bindings[name])
                continue
            if not user32.RegisterHotKey(None, hotkey_id, modifiers | winapi.MOD_NOREPEAT, key):
                self._failed.append(self._bindings[name])
        self._ready.set()

        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == winapi.WM_HOTKEY and 1 <= msg.wParam <= len(names):
                self._on_press(names[msg.wParam - 1])
        for hotkey_id in range(1, len(names) + 1):
            user32.UnregisterHotKey(None, hotkey_id)
