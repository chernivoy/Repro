"""Frame sources: DXGI desktop duplication, with GDI as a slower fallback."""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np
from windows_capture import DxgiDuplicationSession

from .winapi import Dib, Monitor

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]  # x, y, width, height in physical pixels, relative to the monitor


class CaptureError(RuntimeError):
    pass


class DxgiSource:
    """Desktop Duplication: ~30 fps for a full 4K Retina monitor, even inside an RDP session."""

    name = "DXGI"

    def __init__(self, monitor: Monitor, rect: Rect) -> None:
        self._monitor = monitor
        self._rect = rect
        self._frame = None
        self._session = DxgiDuplicationSession(monitor.index)
        # Duplication hands out the whole desktop first and then only changes, so keep this
        # frame: on a still screen it is the only one we would get.
        self._pending = self._session.acquire_frame(1000)
        if self._pending is None:
            raise CaptureError("DXGI не віддав перший кадр")
        self._check_size(self._pending)

    def grab(self) -> np.ndarray | None:
        """The region as a BGRA view, valid until release(); None while the screen is unchanged."""
        self.release()
        frame, self._pending = self._pending, None
        if frame is None:
            if self._session is None and not self._reopen():
                return None
            try:
                # Poll rather than wait: windows-capture holds the GIL while waiting, which
                # would starve the encoder thread whenever the screen is still.
                frame = self._session.acquire_frame(0)
            except BaseException as error:
                # Access is lost on a display mode change, lock screen or UAC prompt.
                log.warning("DXGI access lost: %s", error)
                self._session = None
                return None
            if frame is None:
                return None
            self._check_size(frame)
        self._frame = frame
        x, y, width, height = self._rect
        image = frame.to_numpy()[y:y + height, x:x + width]
        if frame.color_format == "rgba8":
            return cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
        if frame.color_format == "rgba16f":  # HDR desktop: clip to SDR
            return (np.clip(image[..., [2, 1, 0, 3]].astype(np.float32), 0, 1) * 255).astype(np.uint8)
        return image

    def release(self) -> None:
        self._frame = None

    def close(self) -> None:
        self._frame = self._pending = self._session = None

    def _reopen(self) -> bool:
        time.sleep(0.5)
        try:
            self._session = DxgiDuplicationSession(self._monitor.index)
        except BaseException as error:
            log.info("DXGI still unavailable: %s", error)
            return False
        return True

    def _check_size(self, frame) -> None:
        if (frame.width, frame.height) != (self._monitor.width, self._monitor.height):
            raise CaptureError(
                f"Розмір монітора змінився ({self._monitor.width}×{self._monitor.height} → "
                f"{frame.width}×{frame.height}), запис зупинено"
            )


class GdiSource:
    """BitBlt: works everywhere, but manages only ~12 fps for a full 4K monitor."""

    name = "GDI"

    def __init__(self, monitor: Monitor, rect: Rect) -> None:
        x, y, width, height = rect
        self._x, self._y = monitor.left + x, monitor.top + y
        self._dib = Dib(width, height)

    def grab(self) -> np.ndarray:
        self._dib.copy_from_screen(self._x, self._y)
        return self._dib.pixels

    def release(self) -> None:
        pass

    def close(self) -> None:
        self._dib.close()


def open_source(monitor: Monitor, rect: Rect) -> DxgiSource | GdiSource:
    """Call from the thread that will grab frames: DXGI sessions are bound to their thread."""
    try:
        return DxgiSource(monitor, rect)
    except BaseException as error:  # windows-capture raises Rust panics as BaseException
        log.warning("DXGI unavailable, falling back to GDI: %s", error)
        return GdiSource(monitor, rect)
