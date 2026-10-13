"""A settings field that shows a hotkey as keycaps and records a new one when clicked."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from . import winapi
from .hotkeys import format_hotkey, hotkey_from_key
from .theme import Paint, Ui

MODIFIER_KEYS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
VK_ESCAPE = 0x1B


class HotkeyField(tk.Canvas):
    """Click, then press the new combination; Esc or clicking elsewhere keeps the old one.

    `on_listen(True/False)` brackets the listening, so the app can switch its own hotkeys off
    meanwhile (they would fire instead of reaching the field). `on_change(hotkey)` applies a
    new one and returns an error message if it could not be used; `on_message` shows hints."""

    PROMPT = "Натисніть комбінацію…"

    def __init__(
        self, parent: tk.Widget, ui: Ui, value: str, on_listen: Callable[[bool], None],
        on_change: Callable[[str], str | None], on_message: Callable[[str], None], *, bg: str, width: float = 220,
    ) -> None:
        self._width, self._height = ui.px(width), ui.px(34)
        super().__init__(parent, width=self._width, height=self._height, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=False)
        self._ui, self._bg = ui, bg
        self._value = value
        self._on_listen = on_listen
        self._on_change = on_change
        self._on_message = on_message
        self._listening = False
        self._hovered = False
        self._held: list[str] = []
        self._photo = None
        self.bind("<ButtonRelease-1>", self._listen)
        self.bind("<KeyPress>", self._key)
        self.bind("<FocusOut>", lambda _event: self.cancel())
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self._draw()

    def set(self, value: str) -> None:
        self._value = value
        self._draw()

    def cancel(self) -> None:
        """Stops listening and keeps the current hotkey."""
        if not self._listening:
            return
        self._listening = False
        self._held = []
        self._draw()
        self._on_listen(False)

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _listen(self, _event: tk.Event) -> None:
        if self._listening:
            return
        self._listening = True
        self._on_listen(True)
        self._draw()
        self.focus_set()

    def _key(self, event: tk.Event) -> str | None:
        if not self._listening:
            return None
        vk = event.keycode  # Tk on Windows reports the virtual key
        if vk == VK_ESCAPE:
            self.cancel()
            return "break"
        ctrl, alt, shift = (winapi.key_state(key)[0] for key in (0x11, 0x12, 0x10))
        if vk in MODIFIER_KEYS:  # show what is held so far: Ctrl Alt …
            self._held = [name for down, name in ((ctrl, "Ctrl"), (alt, "Alt"), (shift, "Shift")) if down]
            self._draw()
            return "break"
        try:
            hotkey = hotkey_from_key(vk, ctrl, alt, shift)
        except ValueError as error:
            self._on_message(str(error))
            self._held = []
            self._draw()
            return "break"
        error = self._on_change(hotkey)
        if error:
            self._on_message(error)
        else:
            self._value = hotkey
        self.cancel()
        return "break"

    def _draw(self) -> None:
        ui, theme = self._ui, self._ui.theme
        width, height = self._width, self._height
        self.delete("all")
        paint = Paint(width, height, self._bg)
        if self._listening:
            paint.rounded((0, 0, width, height), ui.px(7), fill=theme.surface, outline=theme.accent, line=ui.px(2))
        else:
            paint.rounded((0, 0, width, height), ui.px(7),
                          fill=theme.control_hover if self._hovered else theme.control)
        keys = self._held if self._listening else format_hotkey(self._value).split("+")
        # Held modifiers start at the left, leaving room for the key still to come.
        left = ui.px(8) if self._listening else (width - _caps_width(ui, keys)) / 2
        cap_fill = theme.control if self._listening and not theme.dark else None
        end = _draw_caps(self, paint, ui, keys, left, height, cap_fill)
        if self._listening:
            if keys:
                self.create_text(end + ui.px(2), height / 2, text="+ …", font=ui.font(13), fill=theme.muted, anchor="w")
            else:
                self.create_text(width / 2, height / 2, text=self.PROMPT, font=ui.font(13), fill=theme.accent)


class Keycaps(tk.Canvas):
    """Keys shown as keycaps, for reference."""

    def __init__(self, parent: tk.Widget, ui: Ui, keys: list[str], *, bg: str) -> None:
        width, height = round(_caps_width(ui, keys)), ui.px(24)
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        paint = Paint(width, height, bg)
        _draw_caps(self, paint, ui, keys, 0, height, ui.theme.control, cap_height=height)


CAP_GAP, CAP_PAD = 4, 8  # logical pixels


def _caps_width(ui: Ui, keys: list[str]) -> float:
    key_font = ui.font(12, "semibold")
    return sum(ui.measure(key, key_font) + 2 * ui.px(CAP_PAD) for key in keys) + ui.px(CAP_GAP) * max(0, len(keys) - 1)


def _draw_caps(
    canvas: tk.Canvas, paint: Paint, ui: Ui, keys: list[str], left: float, height: int, fill: str | None = None,
    cap_height: int | None = None,
) -> float:
    """Draws the caps into `paint`, puts it on the canvas with the key names; returns where they end."""
    theme = ui.theme
    key_font = ui.font(12, "semibold")
    cap_height = cap_height or height - ui.px(12)
    top = (height - cap_height) / 2
    x = left
    labels = []
    for key in keys:
        cap_width = ui.measure(key, key_font) + 2 * ui.px(CAP_PAD)
        paint.rounded((x, top, x + cap_width, top + cap_height), ui.px(5),
                      fill=fill or (theme.thumb if theme.dark else theme.surface),
                      outline=None if theme.dark else theme.border, line=ui.px(1))
        labels.append((x + cap_width / 2, key))
        x += cap_width + ui.px(CAP_GAP)
    canvas.photo = paint.photo()
    canvas.create_image(0, 0, anchor="nw", image=canvas.photo)
    for center, key in labels:
        canvas.create_text(center, height / 2, text=key, font=key_font, fill=theme.text)
    return x - ui.px(CAP_GAP)
