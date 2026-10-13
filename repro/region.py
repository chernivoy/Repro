"""Geometry of the recorded area and the red frame that marks it while recording."""

from __future__ import annotations

import tkinter as tk

from . import winapi
from .capture import Rect
from .recorder import output_size
from .winapi import Monitor

MIN_DRAG = 32  # physical pixels; anything smaller is treated as a stray click
FRAME_COLOR = "#ff3b30"
PAUSED_COLOR = "#ff9f0a"
FRAME_WIDTH = 3  # logical pixels


def place(window: tk.Misc, x: int, y: int, width: int, height: int) -> None:
    # Tk spells a negative offset as "+-2304"; a bare "-2304" would anchor to the far edge.
    window.geometry(f"{width}x{height}+{x}+{y}")


def fit_to_video(divisor: float, x: int, y: int, width: int, height: int) -> Rect:
    """Trims the size to what the video's pixels cover exactly (see recorder.video_divisor),
    so a 200% session at ½ downscales by exactly 2 and text stays crisp."""
    out_width, out_height = output_size(width, height, divisor)
    return x, y, round(out_width * divisor), round(out_height * divisor)


def recording_layout(monitor: Monitor, rect: Rect, divisor: float) -> tuple[Rect, int]:
    """The area to record and the frame thickness. The frame sits just outside the recorded area;
    on sides with no room for it (a whole monitor, a region at the edge) the recorded area gives
    way instead, so the frame is always visible yet never ends up in the video."""
    thickness = max(2, round(FRAME_WIDTH * monitor.scale))
    x, y, width, height = rect
    left = x if x >= thickness else x + thickness
    top = y if y >= thickness else y + thickness
    right = x + width if monitor.width - (x + width) >= thickness else x + width - thickness
    bottom = y + height if monitor.height - (y + height) >= thickness else y + height - thickness
    return fit_to_video(divisor, left, top, right - left, bottom - top), thickness


class RecordingFrame:
    """Red frame around the recorded area (see recording_layout). Clicks pass through it, so it
    does not block the Start button, tabs or scrollbars at the screen edge."""

    def __init__(self, root: tk.Tk, monitor: Monitor, rect: Rect, thickness: int) -> None:
        x, y, width, height = rect
        x, y = monitor.left + x, monitor.top + y
        edges = (
            (x - thickness, y - thickness, width + 2 * thickness, thickness),
            (x - thickness, y + height, width + 2 * thickness, thickness),
            (x - thickness, y, thickness, height),
            (x + width, y, thickness, height),
        )
        self._windows = []
        for left, top, edge_width, edge_height in edges:
            window = tk.Toplevel(root, bg=FRAME_COLOR)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            place(window, left, top, edge_width, edge_height)
            window.update_idletasks()
            winapi.make_click_through(window.winfo_id())
            self._windows.append(window)

    def set_paused(self, paused: bool) -> None:
        for window in self._windows:
            window.configure(bg=PAUSED_COLOR if paused else FRAME_COLOR)

    def keep_on_top(self) -> None:
        """The taskbar and other topmost windows cover the frame once they are clicked."""
        for window in self._windows:
            winapi.bring_to_top(window.winfo_id())

    def close(self) -> None:
        for window in self._windows:
            window.destroy()
        self._windows = []
