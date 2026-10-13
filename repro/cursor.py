"""Draws the mouse pointer into frames: desktop duplication leaves it out."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from . import winapi


@dataclass(frozen=True)
class _Sprite:
    color: np.ndarray  # premultiplied BGR, float32 (h, w, 3)
    alpha: np.ndarray  # float32 (h, w, 1)
    invert: np.ndarray  # bool (h, w): XOR pixels, e.g. the text I-beam
    hot_x: float
    hot_y: float


class CursorPainter:
    def __init__(self, scale: float) -> None:
        self._scale = scale
        self._sprites: dict[int, _Sprite | None] = {}

    def paint(self, frame: np.ndarray, origin_x: int, origin_y: int) -> Callable[[], None] | None:
        """Draws the pointer onto a BGRA frame whose top-left corner sits at the given physical
        screen point. Returns a callable that puts the covered pixels back."""
        state = winapi.cursor_state()
        if state is None:
            return None
        handle, x, y = state
        if handle not in self._sprites:
            self._sprites[handle] = self._render(handle)
        sprite = self._sprites[handle]
        if sprite is None:
            return None

        sprite_h, sprite_w = sprite.alpha.shape[:2]
        left = round((x - origin_x) / self._scale - sprite.hot_x)
        top = round((y - origin_y) / self._scale - sprite.hot_y)
        x0, y0 = max(left, 0), max(top, 0)
        x1, y1 = min(left + sprite_w, frame.shape[1]), min(top + sprite_h, frame.shape[0])
        if x0 >= x1 or y0 >= y1:
            return None

        area = frame[y0:y1, x0:x1, :3]
        saved = area.copy()
        cut = np.s_[y0 - top:y1 - top, x0 - left:x1 - left]
        blended = saved * (1 - sprite.alpha[cut]) + sprite.color[cut]
        invert = sprite.invert[cut]
        blended[invert] = 255 - saved[invert]
        area[:] = np.clip(blended + 0.5, 0, 255).astype(np.uint8)

        def restore() -> None:
            area[:] = saved

        return restore

    def _render(self, handle: int) -> _Sprite | None:
        drawn = winapi.draw_cursor(handle)
        if drawn is None:
            return None
        on_black, on_white, hot_x, hot_y = drawn
        black = on_black[..., :3].astype(np.float32)
        white = on_white[..., :3].astype(np.float32)
        # Over white minus over black: 255 where transparent, 0 where opaque, -255 where inverting.
        diff = (white - black).mean(axis=2, keepdims=True)
        invert = diff[..., 0] < -127
        alpha = np.clip(1 - diff / 255, 0, 1)
        alpha[invert] = 0
        color = black
        color[invert] = 0

        if self._scale != 1:
            height, width = invert.shape
            size = (max(1, round(width / self._scale)), max(1, round(height / self._scale)))
            color = cv2.resize(color, size, interpolation=cv2.INTER_AREA)
            alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)[..., np.newaxis]
            invert = cv2.resize(invert.astype(np.float32), size, interpolation=cv2.INTER_AREA) > 0.5
        return _Sprite(color, alpha, invert, hot_x / self._scale, hot_y / self._scale)
