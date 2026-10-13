"""CleanShot-style capture overlay.

Every monitor gets a transparent layer that catches the mouse: drag anywhere for a new area,
click a monitor to record all of it, press Space to pick a window instead. The selected area
shows corner and edge handles (drag them to resize, drag inside the area to move it) and
everything outside it is dimmed; the other monitors show a hint. A control panel floats in the
middle of the selection."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import theme, winapi
from .capture import Rect
from .recorder import SPEEDS, output_size, speed_text, video_divisor
from .region import MIN_DRAG, fit_to_video, place, recording_layout
from .theme import Ui, mix, pil_font, rgb
from .widgets import Rounded, StepSlider, Tile, Tooltip
from .winapi import Dib, Monitor

Box = tuple[int, int, int, int]  # left, top, right, bottom

# Premultiplied BGRA words for the layers.
CATCH = 1 << 24  # alpha 1 of 255: invisible, yet the layer still receives the mouse
DIM = 110 << 24  # black at ~43%
BORDER = 0x80808080  # white at 50%
TINT = 38 | 23 << 8 | 9 << 16 | 38 << 24  # light blue at 15% over the window under the mouse
ACCENT = 230 | 140 << 8 | 55 << 16 | 230 << 24  # its outline, at 90%

HINT_TEXT = (
    "Потягніть мишею, щоб записати частину екрана. Клацніть, щоб записувати весь цей монітор. "
    "Пробіл — вибрати вікно."
)
WINDOW_HINT_TEXT = "Клацніть вікно, щоб записати його. Пробіл або Esc — назад до виділення області."

# Handle geometry in logical pixels.
ARM = 18
THICKNESS = 4
INSET = 3
GRIP = 12
DRAG_THRESHOLD = 4

CURSORS = {
    "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw",
    "w": "sb_h_double_arrow", "e": "sb_h_double_arrow", "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
}


def video_size(monitor: Monitor, rect: Rect, divisor: float) -> tuple[int, int]:
    """What the video will measure, with room made for the recording frame."""
    capture_rect, _ = recording_layout(monitor, rect, divisor)
    return output_size(capture_rect[2], capture_rect[3], divisor)


def _union(a: Box | None, b: Box | None) -> Box | None:
    if a is None or b is None:
        return a or b
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _premultiplied(image: Image.Image) -> np.ndarray:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32)
    alpha = rgba[..., 3:4] / 255
    return np.round(np.concatenate([rgba[..., 2::-1] * alpha, rgba[..., 3:4]], axis=2)).astype(np.uint8)


def _with_shadow(shape: Image.Image, blur: float) -> Image.Image:
    """White shape over a soft dark shadow, so it shows on light and dark backgrounds alike."""
    shadow = Image.new("RGBA", shape.size, (0, 0, 0, 0))
    shadow.putalpha(shape.getchannel("A").point(lambda a: a * 0.5).filter(ImageFilter.GaussianBlur(blur)))
    return Image.alpha_composite(shadow, shape)


def _handle_sprites(scale: float) -> dict[str, np.ndarray]:
    """Corner L-shapes and edge bars, keyed by grip name."""
    factor = 4  # drawn large and scaled down for smooth edges
    arm, thick, pad = round(ARM * scale), round(THICKNESS * scale), round(3 * scale)
    size = arm + 2 * pad

    def draw(boxes: list[tuple[int, int, int, int]], width: int, height: int) -> Image.Image:
        big = Image.new("RGBA", (width * factor, height * factor), (0, 0, 0, 0))
        pen = ImageDraw.Draw(big)
        for left, top, right, bottom in boxes:
            pen.rounded_rectangle(
                (left * factor, top * factor, right * factor - 1, bottom * factor - 1),
                radius=thick * factor // 2, fill=(255, 255, 255, 255),
            )
        return _with_shadow(big.resize((width, height), Image.Resampling.LANCZOS), blur=1.2 * scale)

    corner = draw([(pad, pad, pad + arm, pad + thick), (pad, pad, pad + thick, pad + arm)], size, size)
    bar = draw([(pad, pad, pad + arm, pad + thick)], size, thick + 2 * pad)
    return {
        "nw": _premultiplied(corner),
        "ne": _premultiplied(corner.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
        "sw": _premultiplied(corner.transpose(Image.Transpose.FLIP_TOP_BOTTOM)),
        "se": _premultiplied(corner.transpose(Image.Transpose.ROTATE_180)),
        "n": _premultiplied(bar),
        "s": _premultiplied(bar),
        "w": _premultiplied(bar.transpose(Image.Transpose.ROTATE_90)),
        "e": _premultiplied(bar.transpose(Image.Transpose.ROTATE_90)),
    }


def _pill(text: str, scale: float, size: int) -> np.ndarray:
    """A rounded label like CleanShot's hint, in the theme's colours, drawn at physical pixels."""
    font = pil_font(round(size * scale))
    left, top, right, bottom = font.getbbox(text)
    pad_x, pad_y = round(size * 1.1 * scale), round(size * 0.55 * scale)
    width, height = right - left + 2 * pad_x, bottom - top + 2 * pad_y
    margin = round(6 * scale)
    image = Image.new("RGBA", (width + 2 * margin, height + 2 * margin), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    colors = theme.current()
    fill = (*(int(value) for value in rgb(colors.surface)), 238)
    ink = (*(int(value) for value in rgb(colors.text)), 255)
    pen.rounded_rectangle((margin, margin, margin + width, margin + height), radius=height // 2, fill=fill)
    pen.text((margin + pad_x - left, margin + pad_y - top), text, font=font, fill=ink)
    return _premultiplied(_with_shadow(image, blur=3 * scale))


def _blit(pixels: np.ndarray, sprite: np.ndarray, x: int, y: int, box: Box) -> None:
    """Premultiplied 'over' of a sprite onto the layer, clipped to box."""
    height, width = sprite.shape[:2]
    x0, y0 = max(x, box[0]), max(y, box[1])
    x1, y1 = min(x + width, box[2]), min(y + height, box[3])
    if x0 >= x1 or y0 >= y1:
        return
    source = sprite[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16)
    target = pixels[y0:y1, x0:x1]
    keep = 255 - source[..., 3:4]
    target[:] = (source + (target.astype(np.uint16) * keep + 127) // 255).astype(np.uint8)


def _fill(words: np.ndarray, area: Box, box: Box, value: int) -> None:
    left, top = max(area[0], box[0]), max(area[1], box[1])
    right, bottom = min(area[2], box[2]), min(area[3], box[3])
    if left < right and top < bottom:
        words[top:bottom, left:right] = value


class _Layer:
    """A per-pixel alpha window covering one monitor."""

    def __init__(self, root: tk.Tk, monitor: Monitor) -> None:
        self.monitor = monitor
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        place(self.window, monitor.left, monitor.top, monitor.width, monitor.height)
        self.window.configure(cursor="crosshair")
        self.window.update_idletasks()
        self.hwnd = winapi.make_layered(self.window.winfo_id())
        self.dib = Dib(monitor.width, monitor.height)
        self.words = self.dib.pixels.view(np.uint32)[..., 0]
        self.cursor = "crosshair"

    def present(self, box: Box) -> None:
        winapi.update_layered(self.hwnd, self.dib, self.monitor.left, self.monitor.top, box)

    def set_cursor(self, cursor: str) -> None:
        if cursor != self.cursor:
            self.cursor = cursor
            self.window.configure(cursor=cursor)

    def close(self) -> None:
        self.window.destroy()
        self.dib.close()


@dataclass
class OverlayActions:
    on_change: Callable[[Monitor, Rect], None]  # selection settled (saved as settings)
    on_record: Callable[[Monitor, Rect], None]
    on_close: Callable[[], None]
    on_settings: Callable[[], None]
    on_toggle: Callable[[str, bool], None]  # "cursor" or "keys" switched on the panel
    on_speed: Callable[[float], None]  # video speed picked on the panel
    on_theme: Callable[[], None]  # the panel's light/dark switch
    remembered_area: Callable[[Monitor], Rect | None]


class CaptureOverlay:
    def __init__(
        self, root: tk.Tk, monitors: list[Monitor], monitor: Monitor, rect: Rect, toggles: dict[str, bool],
        speed: float, zoom: float, actions: OverlayActions,
    ) -> None:
        self.root = root
        self.monitor = monitor
        self._zoom = zoom  # video pixels per logical pixel
        self.rect = rect
        self._actions = actions
        self._mode: tuple[str, str] | None = None
        self._press: tuple[_Layer, int, int] | None = None
        self._origin: Rect = rect
        self._before: tuple[Monitor, Rect] = (monitor, rect)
        self._badge: tuple[np.ndarray, int, int] | None = None
        self._dirty: dict[_Layer, Box] = {}
        self._flush_pending = False
        self._window_mode = False
        self._windows: list[Box] = []  # screen bounds of the visible windows, front to back
        self._hovered: tuple[Monitor, Rect] | None = None

        self._sprites = {m.scale: _handle_sprites(m.scale) for m in monitors}
        self._scales = {m.scale for m in monitors}
        self._make_hints()
        self._layers = [_Layer(root, m) for m in monitors]
        for layer in self._layers:
            self._bind(layer)
            self._render(layer, (0, 0, layer.monitor.width, layer.monitor.height))
        self.panel = ControlPanel(root, toggles, speed, self)
        self._show_panel()

    # --- Public ------------------------------------------------------------------

    def close(self) -> None:
        self.panel.close()
        for layer in self._layers:
            layer.close()
        self._layers = []

    def record(self) -> None:
        self._actions.on_record(self.monitor, self.rect)

    def cancel(self) -> None:
        self._actions.on_close()

    def escape(self) -> None:
        """Esc leaves window picking first, then closes the overlay."""
        if self._window_mode:
            self._leave_window_mode(restore=True)
        else:
            self.cancel()

    def toggle_window_mode(self) -> None:
        """Space: pick a window instead of dragging an area, like CleanShot."""
        if self._window_mode:
            self._leave_window_mode(restore=True)
            return
        self._window_mode = True
        self._before = (self.monitor, self.rect)
        self._windows = winapi.visible_windows()
        self._hovered = self._window_at(*winapi.cursor_position())
        self._badge = None
        self._invalidate_all()

    def open_settings(self) -> None:
        self._actions.on_settings()

    def set_toggle(self, name: str, enabled: bool) -> None:
        self._actions.on_toggle(name, enabled)

    def set_speed(self, speed: float) -> None:
        self._actions.on_speed(speed)

    def toggle_theme(self) -> None:
        self._actions.on_theme()

    def set_theme(self) -> None:
        """The theme changed: hints and the panel in the new colours."""
        self._make_hints()
        self._badge = None
        self.panel.rebuild()
        self._invalidate_all()
        self._show_panel()

    def _make_hints(self) -> None:
        self._hints = {scale: _pill(HINT_TEXT, scale, 15) for scale in self._scales}
        self._window_hints = {scale: _pill(WINDOW_HINT_TEXT, scale, 15) for scale in self._scales}

    def set_zoom(self, zoom: float) -> None:
        """The resolution changed in the settings window: the panel shows the new video size."""
        self._zoom = zoom
        self._show_panel()

    def select_full(self) -> None:
        self._select(self.monitor, self._full(self.monitor))

    def restore_area(self) -> None:
        area = self._actions.remembered_area(self.monitor)
        if area:
            self._select(self.monitor, area)

    def resize_to(self, width: int, height: int) -> None:
        """Width and height of the video in its pixels; keeps the selection's centre."""
        m = self.monitor
        divisor = self._divisor(m)
        width = min(m.width, max(MIN_DRAG, round(width * divisor)))
        height = min(m.height, max(MIN_DRAG, round(height * divisor)))
        x, y, w, h = self.rect
        left = min(max(0, x + (w - width) // 2), m.width - width)
        top = min(max(0, y + (h - height) // 2), m.height - height)
        self._select(m, fit_to_video(divisor, left, top, width, height))

    def _divisor(self, monitor: Monitor) -> float:
        return video_divisor(monitor, self._zoom)

    # --- Mouse ---------------------------------------------------------------------

    def _bind(self, layer: _Layer) -> None:
        window = layer.window
        window.bind("<ButtonPress-1>", lambda event: self._on_press(layer, event))
        window.bind("<B1-Motion>", lambda event: self._on_drag(layer, event))
        window.bind("<ButtonRelease-1>", lambda event: self._on_release(layer, event))
        window.bind("<Motion>", lambda event: self._on_hover(layer, event))
        window.bind("<ButtonPress-3>", lambda _event: self.escape())

    def _on_press(self, layer: _Layer, event: tk.Event) -> None:
        self._press = (layer, event.x, event.y)
        if self._window_mode:
            self._mode = ("window", "")
            return
        self._before = (self.monitor, self.rect)
        self._origin = self.rect
        self._mode = ("pending", "")
        if layer.monitor is self.monitor:
            grip = self._grip_at(event.x, event.y)
            if grip:
                self._mode = ("resize", grip)
            elif not self._is_full() and self._inside(event.x, event.y):
                self._mode = ("move", "")

    def _on_drag(self, layer: _Layer, event: tk.Event) -> None:
        if self._mode is None or self._press is None:
            return
        press_layer, start_x, start_y = self._press
        monitor = press_layer.monitor
        kind, grip = self._mode
        if kind in ("pending", "window"):
            threshold = DRAG_THRESHOLD * monitor.scale
            if abs(event.x - start_x) < threshold and abs(event.y - start_y) < threshold:
                return
            if kind == "window":  # dragging while picking a window draws an area after all
                self._leave_window_mode(restore=False)
            kind = "create"
            self._mode = (kind, "")
            if monitor is not self.monitor:
                self._switch_monitor(monitor)
        self.panel.hide()

        x = min(max(event.x, 0), monitor.width)
        y = min(max(event.y, 0), monitor.height)
        ox, oy, ow, oh = self._origin
        if kind == "create":
            rect = (min(start_x, x), min(start_y, y), abs(x - start_x), abs(y - start_y))
        elif kind == "move":
            left = min(max(0, ox + event.x - start_x), monitor.width - ow)
            top = min(max(0, oy + event.y - start_y), monitor.height - oh)
            rect = (left, top, ow, oh)
        else:
            left, top, right, bottom = ox, oy, ox + ow, oy + oh
            if "w" in grip:
                left = min(x, right - MIN_DRAG)
            if "e" in grip:
                right = max(x, left + MIN_DRAG)
            if "n" in grip:
                top = min(y, bottom - MIN_DRAG)
            if "s" in grip:
                bottom = max(y, top + MIN_DRAG)
            rect = (left, top, right - left, bottom - top)
        self._set_rect(rect, dragging=True)

    def _on_release(self, layer: _Layer, event: tk.Event) -> None:
        mode, self._mode = self._mode, None
        if mode is None or self._press is None:
            return
        press_layer = self._press[0]
        self._press = None
        if mode[0] == "window":  # a click picks the window under the mouse
            m = press_layer.monitor
            picked = self._window_at(m.left + event.x, m.top + event.y)
            self._leave_window_mode(restore=False)
            if picked:
                self._select(picked[0], fit_to_video(self._divisor(picked[0]), *picked[1]))
            else:
                self._select(m, self._full(m))
            return
        if mode[0] == "pending":  # a click: record the whole monitor
            self._select(press_layer.monitor, self._full(press_layer.monitor))
            return
        rect = fit_to_video(self._divisor(self.monitor), *self.rect)
        if mode[0] == "create" and (rect[2] < MIN_DRAG or rect[3] < MIN_DRAG):
            monitor, rect = self._before
            self._switch_monitor(monitor)
        self._set_rect(rect, dragging=False)
        self._settled()

    def _on_hover(self, layer: _Layer, event: tk.Event) -> None:
        if self._window_mode:
            hovered = self._window_at(layer.monitor.left + event.x, layer.monitor.top + event.y)
            if hovered != self._hovered:
                for item in (self._hovered, hovered):
                    if item:
                        x, y, w, h = item[1]
                        self._invalidate(self._layer(item[0]), (x, y, x + w, y + h))
                self._hovered = hovered
            return
        cursor = "crosshair"
        if layer.monitor is self.monitor:
            grip = self._grip_at(event.x, event.y)
            if grip:
                cursor = CURSORS[grip]
            elif not self._is_full() and self._inside(event.x, event.y):
                cursor = "fleur"
        layer.set_cursor(cursor)

    def _grip_at(self, x: int, y: int) -> str | None:
        left, top, width, height = self.rect
        right, bottom = left + width, top + height
        reach = GRIP * self.monitor.scale
        within_x = left - reach <= x <= right + reach
        within_y = top - reach <= y <= bottom + reach
        vertical = "n" if abs(y - top) <= reach and within_x else "s" if abs(y - bottom) <= reach and within_x else ""
        horizontal = "w" if abs(x - left) <= reach and within_y else "e" if abs(x - right) <= reach and within_y else ""
        return (vertical + horizontal) or None

    def _inside(self, x: int, y: int) -> bool:
        left, top, width, height = self.rect
        return left <= x < left + width and top <= y < top + height

    def _window_at(self, x: int, y: int) -> tuple[Monitor, Rect] | None:
        """The window under a screen point, cut to the monitor the point is on."""
        monitor = next(
            (layer.monitor for layer in self._layers
             if layer.monitor.left <= x < layer.monitor.left + layer.monitor.width
             and layer.monitor.top <= y < layer.monitor.top + layer.monitor.height),
            None,
        )
        if monitor is None:
            return None
        for left, top, right, bottom in self._windows:
            if left <= x < right and top <= y < bottom:
                left, top = max(left, monitor.left) - monitor.left, max(top, monitor.top) - monitor.top
                right = min(right, monitor.left + monitor.width) - monitor.left
                bottom = min(bottom, monitor.top + monitor.height) - monitor.top
                if right - left < MIN_DRAG or bottom - top < MIN_DRAG:
                    return None
                return monitor, (left, top, right - left, bottom - top)
        return None

    def _leave_window_mode(self, restore: bool) -> None:
        self._window_mode = False
        self._hovered = None
        if restore:
            self.monitor, self.rect = self._before
        self._invalidate_all()

    # --- Selection state -------------------------------------------------------------

    def _full(self, monitor: Monitor) -> Rect:
        return 0, 0, monitor.width, monitor.height

    def _is_full(self) -> bool:
        return self.rect == self._full(self.monitor)

    def _select(self, monitor: Monitor, rect: Rect) -> None:
        self._switch_monitor(monitor)
        self._set_rect(rect, dragging=False)
        self._settled()

    def _switch_monitor(self, monitor: Monitor) -> None:
        if monitor is self.monitor:
            return
        previous = self._layer(self.monitor)
        self.monitor = monitor
        self.rect = self._full(monitor)
        self._badge = None
        self._invalidate(previous, (0, 0, previous.monitor.width, previous.monitor.height))
        self._invalidate(self._layer(monitor), (0, 0, monitor.width, monitor.height))

    def _set_rect(self, rect: Rect, dragging: bool) -> None:
        old = self._decor_box(self.rect)
        self.rect = rect
        self._badge = self._make_badge() if dragging else None
        self._invalidate(self._layer(self.monitor), _union(old, self._decor_box(rect)))

    def _settled(self) -> None:
        self._flush()
        self._actions.on_change(self.monitor, self.rect)
        self._show_panel()

    def _make_badge(self) -> tuple[np.ndarray, int, int]:
        m = self.monitor
        divisor = self._divisor(m)
        width, height = video_size(m, fit_to_video(divisor, *self.rect), divisor)
        sprite = _pill(f"{width} × {height}", m.scale, 11)
        x, y, w, h = self.rect
        sprite_h, sprite_w = sprite.shape[:2]
        left = min(max(0, x + w - sprite_w), m.width - sprite_w)
        top = y + h if y + h + sprite_h <= m.height else max(0, y + h - sprite_h)
        return sprite, left, top

    def _decor_box(self, rect: Rect) -> Box:
        """Everything drawn for a selection: the area, its handles and the size badge."""
        x, y, w, h = rect
        margin = round((ARM + 6) * self.monitor.scale)
        box = (x - margin, y - margin, x + w + margin, y + h + margin)
        if self._badge:
            sprite, left, top = self._badge
            box = _union(box, (left, top, left + sprite.shape[1], top + sprite.shape[0]))
        return box

    # --- Rendering -------------------------------------------------------------------

    def _layer(self, monitor: Monitor) -> _Layer:
        return next(layer for layer in self._layers if layer.monitor is monitor)

    def _invalidate_all(self) -> None:
        for layer in self._layers:
            self._invalidate(layer, (0, 0, layer.monitor.width, layer.monitor.height))

    def _invalidate(self, layer: _Layer, box: Box) -> None:
        self._dirty[layer] = _union(self._dirty.get(layer), box)
        if not self._flush_pending:
            self._flush_pending = True
            self.root.after_idle(self._flush)

    def _flush(self) -> None:
        """Mouse moves arrive faster than 4K layers redraw; render once per idle moment."""
        self._flush_pending = False
        dirty, self._dirty = self._dirty, {}
        for layer, box in dirty.items():
            if layer in self._layers:
                self._render(layer, box)

    def _render(self, layer: _Layer, box: Box) -> None:
        m = layer.monitor
        box = (max(0, box[0]), max(0, box[1]), min(m.width, box[2]), min(m.height, box[3]))
        if box[0] >= box[2] or box[1] >= box[3]:
            return
        words, pixels = layer.words, layer.dib.pixels
        if self._window_mode:
            self._render_window_mode(layer, box)
            return
        if m is not self.monitor:
            _fill(words, (0, 0, m.width, m.height), box, CATCH)
            hint = self._hints[m.scale]
            _blit(pixels, hint, (m.width - hint.shape[1]) // 2, (m.height - hint.shape[0]) // 2, box)
            layer.present(box)
            return

        x, y, w, h = self.rect
        full = self._is_full()
        _fill(words, (0, 0, m.width, m.height), box, CATCH if full else DIM)
        if not full:
            _fill(words, (x, y, x + w, y + h), box, CATCH)
            line = max(1, round(m.scale))
            for edge in ((x, y - line, x + w, y), (x, y + h, x + w, y + h + line),
                         (x - line, y - line, x, y + h + line), (x + w, y - line, x + w + line, y + h + line)):
                _fill(words, edge, box, BORDER)
        for grip, (left, top) in self._handle_positions().items():
            _blit(pixels, self._sprites[m.scale][grip], left, top, box)
        if self._badge:
            sprite, left, top = self._badge
            _blit(pixels, sprite, left, top, box)
        layer.present(box)

    def _render_window_mode(self, layer: _Layer, box: Box) -> None:
        """Everything dimmed except the window under the mouse, which gets a blue tint and outline."""
        m = layer.monitor
        _fill(layer.words, (0, 0, m.width, m.height), box, DIM)
        if self._hovered and self._hovered[0] is m:
            x, y, w, h = self._hovered[1]
            _fill(layer.words, (x, y, x + w, y + h), box, TINT)
            line = max(2, round(3 * m.scale))
            for edge in ((x, y, x + w, y + line), (x, y + h - line, x + w, y + h),
                         (x, y, x + line, y + h), (x + w - line, y, x + w, y + h)):
                _fill(layer.words, edge, box, ACCENT)
        hint = self._window_hints[m.scale]
        _blit(layer.dib.pixels, hint, (m.width - hint.shape[1]) // 2, round(24 * m.scale), box)
        layer.present(box)

    def _handle_positions(self) -> dict[str, tuple[int, int]]:
        """Top-left corners of the handle sprites, drawn just inside the selection."""
        sprites = self._sprites[self.monitor.scale]
        inset = round(INSET * self.monitor.scale)
        x, y, w, h = self.rect
        right, bottom = x + w, y + h
        corner = sprites["nw"].shape[0]
        bar_h, bar_w = sprites["n"].shape[:2]
        pad = (corner - round(ARM * self.monitor.scale)) // 2
        positions = {
            "nw": (x + inset - pad, y + inset - pad),
            "ne": (right - inset + pad - corner, y + inset - pad),
            "sw": (x + inset - pad, bottom - inset + pad - corner),
            "se": (right - inset + pad - corner, bottom - inset + pad - corner),
        }
        # Edge bars only once the selection is big enough not to crowd the corners.
        if w > 4 * corner:
            positions["n"] = (x + (w - bar_w) // 2, y + inset - pad)
            positions["s"] = (x + (w - bar_w) // 2, bottom - inset + pad - bar_h)
        if h > 4 * corner:
            positions["w"] = (x + inset - pad, y + (h - bar_w) // 2)
            positions["e"] = (right - inset + pad - bar_h, y + (h - bar_w) // 2)
        return positions

    def _show_panel(self) -> None:
        m = self.monitor
        x, y, w, h = self.rect
        self.panel.set_size(*video_size(m, self.rect, self._divisor(m)))
        self.panel.show(m, (x, y, w, h))


class ControlPanel:
    """The floating HUD: settings, video size, whole monitor, remembered area and theme; camera,
    cursor and keystroke toggles; video speed; Record Video. The camera is a placeholder for later.
    Built at the scale of the monitor it is shown on, and again when that or the theme changes."""

    SETTINGS, FULLSCREEN, CROP, CAMERA, POINTER, KEYBOARD, ENTER, RECORD, SUN, MOON = (
        "\ue9e9", "\ue740", "\ue7a8", "\ue714", "\ue8b0", "\ue765", "\ue751", "\ue7c8", "\ue706", "\ue708"
    )
    TOGGLES = (  # name, glyph, caption, tooltip
        ("camera", CAMERA, "Камера", "Камера — буде в наступних версіях"),
        ("cursor", POINTER, "Курсор", "Курсор миші у відео"),
        ("keys", KEYBOARD, "Клавіші", "Натискання клавіш у відео"),
    )

    def __init__(self, root: tk.Tk, toggles: dict[str, bool], speed: float, overlay: CaptureOverlay) -> None:
        self._overlay = overlay
        self._toggles = dict(toggles)
        self._speed = speed
        self._shown_size = (0, 0)
        self._scale = 0.0
        self._body: tk.Frame | None = None
        self._tiles: dict[str, tuple[Tile, int, int]] = {}
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.bind("<Return>", lambda _event: overlay.record())
        self.window.bind("<KP_Enter>", lambda _event: overlay.record())
        self.window.bind("<Escape>", lambda _event: overlay.escape())
        self.window.bind("<space>", lambda _event: overlay.toggle_window_mode())

    def rebuild(self) -> None:
        """The theme changed."""
        if self._body:
            self._build(self._scale)

    def _build(self, scale: float) -> None:
        self._scale = scale
        ui = self._ui = Ui(scale)
        theme = ui.theme
        gap = ui.px(6)
        self.window.configure(bg=theme.panel)
        body = tk.Frame(self.window, bg=theme.panel)

        top = tk.Frame(body, bg=theme.panel)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        self._icon_tile(top, self.SETTINGS, "Налаштування", self._overlay.open_settings).grid(
            row=0, column=0, sticky="ns", padx=(0, gap))
        size = Rounded(top, ui, theme.tile, theme.panel, radius=8, padx=8, pady=6)
        size.grid(row=0, column=1, sticky="nsew", padx=(0, gap))
        self._width = self._size_field(size.body)
        tk.Label(size.body, text="×", font=ui.font(15), fg=theme.muted, bg=theme.tile).pack(side="left", padx=ui.px(5))
        self._height = self._size_field(size.body)
        full = Tile(size.body, ui, bg=theme.tile, fill=theme.tile, hover=theme.tile_hover,
                    command=self._overlay.select_full, width=32, height=32, radius=6)
        full.text(self.FULLSCREEN, ui.font(14, "icons"), theme.text)
        full.pack(side="left", padx=(ui.px(8), 0))
        Tooltip(full, "Весь монітор")
        self._icon_tile(top, self.CROP, "Повернути збережену область", self._overlay.restore_area).grid(
            row=0, column=2, sticky="ns", padx=(0, gap))
        self._icon_tile(top, self.SUN if theme.dark else self.MOON, "Світла тема" if theme.dark else "Темна тема",
                        self._overlay.toggle_theme).grid(row=0, column=3, sticky="ns")

        toggles = tk.Frame(body, bg=theme.panel)
        toggles.pack(fill="x", pady=(gap, 0))
        self._tiles = {}
        for column, (name, glyph, caption, tip) in enumerate(self.TOGGLES):
            toggles.columnconfigure(column, weight=1, uniform="toggle")
            tile = Tile(toggles, ui, bg=theme.panel, fill=theme.tile, width=64, height=58, radius=8,
                        command=None if name == "camera" else lambda name=name: self._flip(name))
            icon = tile.text(glyph, ui.font(18, "icons"), theme.text, dy=-8)
            label = tile.text(caption, ui.font(11), theme.muted, dy=14)
            tile.grid(row=0, column=column, sticky="nsew", padx=(0, gap if column < len(self.TOGGLES) - 1 else 0))
            Tooltip(tile, tip)
            self._tiles[name] = (tile, icon, label)
            self._paint_toggle(name)

        speed = Rounded(body, ui, theme.tile, theme.panel, radius=8, padx=12, pady=6)
        speed.pack(fill="x", pady=(gap, 0))
        title = tk.Label(speed.body, text="Швидкість", font=ui.font(13), fg=theme.muted, bg=theme.tile)
        title.pack(side="left")
        self._speed_value = tk.Label(speed.body, text=speed_text(self._speed), font=ui.font(13, "semibold"),
                                     fg=theme.text, bg=theme.tile, width=4, anchor="e")
        self._speed_value.pack(side="right")
        self._speed_slider = StepSlider(speed.body, ui, SPEEDS, self._speed, 1.0, self._pick_speed, bg=theme.tile,
                                        width=150)
        self._speed_slider.pack(side="left", fill="x", expand=True, padx=ui.px(8))
        Tooltip(speed, "Швидкість відео: вище 1× — швидше й коротше, нижче — повільніше й довше. "
                       "Подвійний клік — 1×", title, self._speed_value, self._speed_slider)

        record = Tile(body, ui, bg=theme.panel, fill=theme.danger, hover=theme.danger_hover,
                      command=self._overlay.record, width=200, height=48, radius=10)
        record.text(self.RECORD, ui.font(17, "icons"), "#ffffff", relx=0, dx=18, anchor="w")
        record.text("Записати відео", ui.font(15, "semibold"), "#ffffff", relx=0, dx=48, anchor="w")
        record.text(self.ENTER, ui.font(14, "icons"), mix("#ffffff", theme.danger, 0.3), relx=1, dx=-18, anchor="e")
        record.pack(fill="x", pady=(gap, 0))

        old, self._body = self._body, body
        if old:
            old.destroy()
        body.pack(padx=ui.px(8), pady=ui.px(8))
        self._fill_size()
        self.window.update_idletasks()
        winapi.round_corners(self.window.winfo_id(), border=theme.border)

    def _icon_tile(self, parent: tk.Widget, glyph: str, tip: str, action: Callable[[], None]) -> Tile:
        ui, theme = self._ui, self._ui.theme
        tile = Tile(parent, ui, bg=theme.panel, fill=theme.tile, hover=theme.tile_hover, command=action,
                    width=46, height=46, radius=8)
        tile.text(glyph, ui.font(16, "icons"), theme.text)
        Tooltip(tile, tip)
        return tile

    def _size_field(self, parent: tk.Widget) -> tk.Entry:
        ui, theme = self._ui, self._ui.theme
        box = Rounded(parent, ui, theme.field, theme.tile, radius=6, padx=6, pady=3)
        box.pack(side="left")
        field = tk.Entry(
            box.body, width=5, justify="center", font=ui.font(15, "semibold"), relief="flat", bd=0,
            bg=theme.field, fg=theme.text, insertbackground=theme.text, highlightthickness=0,
            validate="key", validatecommand=(parent.register(lambda text: text.isdigit() or text == ""), "%P"),
        )
        field.pack()
        field.bind("<Return>", self._enter_size)
        field.bind("<KP_Enter>", self._enter_size)
        field.bind("<FocusOut>", lambda _event: self._apply_size())
        field.bind("<space>", lambda _event: "break")  # typing in a size field, not picking a window
        return field

    def _enter_size(self, _event: tk.Event) -> str:
        self._apply_size()
        self.window.focus_set()
        return "break"  # Enter in a size field applies the size instead of starting to record

    def _apply_size(self) -> None:
        try:
            width, height = int(self._width.get()), int(self._height.get())
        except (ValueError, tk.TclError):  # empty field, or the panel is being destroyed
            return
        if (width, height) != self._shown_size:
            self._overlay.resize_to(width, height)

    def _flip(self, name: str) -> None:
        self.set_toggle(name, not self._toggles[name])
        self._overlay.set_toggle(name, self._toggles[name])

    def _pick_speed(self, speed: float) -> None:
        self._speed = speed
        self._speed_value.configure(text=speed_text(speed))
        self._overlay.set_speed(speed)

    def set_speed(self, speed: float) -> None:
        """Shows the speed, also when it was changed in the settings window."""
        self._speed = speed
        if self._body:
            self._speed_slider.set(speed)
            self._speed_value.configure(text=speed_text(speed))

    def set_toggle(self, name: str, enabled: bool) -> None:
        """Shows a toggle's state, also when it was changed in the settings window."""
        self._toggles[name] = enabled
        if name in self._tiles:
            self._paint_toggle(name)

    def _paint_toggle(self, name: str) -> None:
        """On: an accent tile, like Windows' quick settings. The camera is not there yet."""
        theme = self._ui.theme
        tile, icon, label = self._tiles[name]
        if name == "camera":
            tile.set_fill(theme.tile)
            colors = (theme.faint, theme.faint)
        elif self._toggles[name]:
            tile.set_fill(theme.accent, theme.accent_hover)
            colors = (theme.on_accent, theme.on_accent)
        else:
            tile.set_fill(theme.tile, theme.tile_hover)
            colors = (theme.text, theme.muted)
        tile.itemconfigure(icon, fill=colors[0])
        tile.itemconfigure(label, fill=colors[1])

    # --- Placement -------------------------------------------------------------------

    def set_size(self, width: int, height: int) -> None:
        self._shown_size = (width, height)
        if self._body:
            self._fill_size()

    def _fill_size(self) -> None:
        for field, value in zip((self._width, self._height), self._shown_size):
            field.delete(0, "end")
            field.insert(0, str(value))

    def show(self, monitor: Monitor, rect: Rect) -> None:
        """Centred in the selection; below or above it when it is too small to hold the panel."""
        if monitor.scale != self._scale:
            self._build(monitor.scale)
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x, y, w, h = rect
        gap = round(12 * monitor.scale)
        left = x + (w - width) // 2
        if w >= width + 2 * gap and h >= height + 2 * gap:
            top = y + (h - height) // 2
        elif y + h + gap + height <= monitor.height:
            top = y + h + gap
        elif y - gap - height >= 0:
            top = y - gap - height
        else:
            top = (monitor.height - height) // 2
        left = min(max(0, left), monitor.width - width)
        self.window.geometry(f"+{monitor.left + left}+{monitor.top + top}")
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def hide(self) -> None:
        self.window.withdraw()

    def close(self) -> None:
        self.window.destroy()
