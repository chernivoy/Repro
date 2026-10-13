"""Records a monitor, or a region of it, to H.264 MP4 at the logical (non-Retina) resolution."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np
from av.video.reformatter import ColorPrimaries, ColorRange, Colorspace, ColorTrc

from . import winapi
from .capture import CaptureError, Rect, open_source
from .cursor import CursorPainter
from .inputs import DEFAULT_LEFT_COLOR, DEFAULT_RIGHT_COLOR, InputPainter, InputTracker
from .winapi import Monitor

log = logging.getLogger(__name__)


# Video pixels per logical pixel. ½ is the size things look on the Mac; halving the Retina
# picture greys out strokes one or two pixels wide, so ¾ keeps text noticeably crisper, for
# files 3-4 times bigger and an encoder near its limit on a full monitor in motion.
RESOLUTIONS = {
    "half": ("½ — як на Mac", 1.0,
             "Як на екрані Mac. Найменші файли."),
    "three_quarters": ("¾ — чіткіше", 1.5,
                       "Текст чіткіший, але файли в 3–4 рази більші, а швидкий рух на весь екран "
                       "може йти менш плавно."),
}
DEFAULT_RESOLUTION = "half"
# How strongly edges are sharpened after downscaling, which halving leaves soft. 0.5 made text
# look harsh and jagged, especially at ½; 0.2 brings back crisp edges without halos.
SHARPNESS = {0.0: "вимк.", 0.1: "ледь", 0.2: "слабка", 0.3: "середня", 0.4: "сильна"}
DEFAULT_SHARPNESS = 0.2


def snap_sharpness(amount: float) -> float:
    return min(SHARPNESS, key=lambda option: abs(option - amount))


def sharpen_kernel(amount: float) -> np.ndarray:
    """A 3×3 sharpening filter: 2-4 ms a frame, ~10% more bytes at 0.2."""
    return np.array([[0, -amount, 0], [-amount, 1 + 4 * amount, -amount], [0, -amount, 0]], np.float32)


def video_divisor(monitor: Monitor, zoom: float) -> float:
    """Physical pixels per video pixel: 2 for a 200% session at ½, 4/3 at ¾. Never below 1:
    the video does not get bigger than the screen."""
    return max(1.0, monitor.scale / zoom)


def output_size(width: int, height: int, divisor: float) -> tuple[int, int]:
    """Video size for an area of the screen, rounded down to even: H.264 4:2:0 needs both
    sides even."""
    return max(2, int(width / divisor + 1e-6) // 2 * 2), max(2, int(height / divisor + 1e-6) // 2 * 2)


@dataclass(frozen=True)
class Quality:
    name: str  # shown in the settings
    hint: str
    crf: int  # x264: lower is sharper and bigger, +6 roughly halves the size
    preset: str  # slower presets squeeze more quality out of the same size, at more CPU


# Measured on scrolling text at the logical size of a 4K Retina monitor. Compact matches the
# size and look of the old veryfast/CRF 26; standard is ~+3 dB PSNR for 20–50% more bytes;
# every level holds 30 fps at about 40% of 8 cores.
QUALITIES = {
    "compact": Quality("Компактна", "Найменші файли. Текст при прокручуванні трохи розмитий.", 28, "faster"),
    "standard": Quality("Стандартна", "Чіткий текст і під час прокручування. Рекомендовано.", 23, "faster"),
    "high": Quality("Висока", "Ще чистіші дрібні деталі, файли трохи більші.", 20, "medium"),
    "max": Quality("Максимальна", "Майже без втрат, файли в півтора раза більші за стандартні.", 16, "medium"),
}
DEFAULT_QUALITY = "standard"
FRAME_RATES = (15, 25, 30)
TIME_BASE = Fraction(1, 1000)
SPEEDS = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3)  # how fast the video plays what happened on screen


def speed_text(speed: float) -> str:
    return f"{speed:g}×"


def minute_of_video(speed: float) -> str:
    """How long a minute of recording plays in the video: "1 хв 26 с", "50 с"."""
    seconds = round(60 / speed)
    return f"{seconds // 60} хв {seconds % 60} с" if seconds >= 60 else f"{seconds} с"


def speed_hint(speed: float) -> str:
    if speed == 1.0:
        return "Як на екрані: хвилина запису — хвилина відео."
    return f"{'Швидше' if speed > 1 else 'Повільніше'}: хвилина запису стане відео на {minute_of_video(speed)}."


def snap_speed(speed: float) -> float:
    return min(SPEEDS, key=lambda option: abs(option - speed))


@dataclass(frozen=True)
class RecordingOptions:
    fps: int = 30  # at most this many frames per second of recording
    speed: float = 1.0  # 1.2: the video plays 20% faster, so a minute of recording lasts 50 s
    crf: int = QUALITIES[DEFAULT_QUALITY].crf
    preset: str = QUALITIES[DEFAULT_QUALITY].preset
    zoom: float = 1.0  # video pixels per logical pixel, see RESOLUTIONS
    sharpness: float = DEFAULT_SHARPNESS  # 0 leaves the downscaled picture as it is
    keyframe_seconds: int = 5  # lets players seek quickly
    draw_cursor: bool = True
    highlight_clicks: bool = True
    click_colors: tuple[str, str] = (DEFAULT_LEFT_COLOR, DEFAULT_RIGHT_COLOR)  # left and right button
    keystrokes: str = ""  # "", "all" or "shortcuts"
    hidden_hotkeys: tuple[str, ...] = ()  # the app's own, kept out of the keystrokes


class Recorder:
    """Capture thread keeps the latest downscaled frame; the encoder thread writes each new one.

    The video has a variable frame rate: every frame the screen produces goes in once, stamped
    with the moment it was captured, so it lasts exactly as long as it was on screen. Sampling
    at a fixed rate instead holds some frames for one tick and others for two (RDP updates the
    screen 20-30 times a second, not in step with the sampling), and that judder became plain
    to see in slow motion. While the screen stands still, the pointer, clicks and keystrokes
    still add frames, up to fps a second."""

    def __init__(
        self,
        monitor: Monitor,
        rect: Rect,
        path: Path,
        options: RecordingOptions,
        on_finished: Callable[[Recorder, BaseException | None], None],
    ) -> None:
        self.monitor = monitor
        self.rect = rect
        self.path = path
        self.options = options
        self.divisor = video_divisor(monitor, options.zoom)
        self.size = output_size(rect[2], rect[3], self.divisor)
        # At ¾ a full monitor has 2.25 times the pixels; "medium" then manages only ~24 fps
        # while the whole screen scrolls, "faster" keeps up for a slightly bigger file.
        self.preset = "faster" if options.zoom > 1 and options.preset == "medium" else options.preset
        self.source_name = ""
        self.duration = 0.0
        self.frames = 0
        self._on_finished = on_finished
        self._latest: tuple[np.ndarray, int, float] | None = None  # frame, number, perf_counter when captured
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._source_ready = threading.Event()
        self._started = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._inputs: InputTracker | None = None

    def start(self) -> None:
        self._open_output()
        self._capture_thread = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self._capture_thread.start()
        if not self._source_ready.wait(5):
            self._error = CaptureError("Захоплення екрана не запустилося")
        if self._error:
            self._stop.set()
            try:
                self._container.close()
            except Exception:
                log.exception("Closing the empty file failed")
            self.path.unlink(missing_ok=True)
            if isinstance(self._error, Exception):
                raise self._error
            raise CaptureError(str(self._error))
        log.info(
            "Recording %s via %s: %s -> %sx%s%s, up to %s fps, speed %s, crf %s %s", self.path,
            self.source_name, self.rect, *self.size, f", sharpness {self.options.sharpness:g}",
            self.options.fps, speed_text(self.options.speed), self.options.crf, self.preset,
        )
        if self.options.highlight_clicks or self.options.keystrokes:
            self._inputs = InputTracker(
                self.options.highlight_clicks, self.options.keystrokes, self.options.hidden_hotkeys
            )
            self._inputs.start()
        self._started = time.perf_counter()
        self._encode_thread = threading.Thread(target=self._encode_loop, name="encode", daemon=True)
        self._encode_thread.start()

    def stop(self) -> None:
        """Returns at once; on_finished fires from the encoder thread when the file is complete."""
        self._stop.set()

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.perf_counter()
            if self._inputs:
                self._inputs.set_paused(True)

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.perf_counter() - self._paused_at
            self._paused_at = None
            if self._inputs:
                self._inputs.set_paused(False)

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def elapsed(self) -> float:
        """Recorded time so far; pauses do not count, so the video continues seamlessly."""
        paused_at = self._paused_at
        now = paused_at if paused_at is not None else time.perf_counter()
        return now - self._started - self._paused_total

    def _open_output(self) -> None:
        fps = self.options.fps
        self._container = av.open(str(self.path), "w", options={"movflags": "+faststart"})
        self._stream = self._container.add_stream(
            "libx264", rate=fps, options={"preset": self.preset, "crf": str(self.options.crf)}
        )
        self._stream.width, self._stream.height = self.size
        self._stream.pix_fmt = "yuv420p"
        codec = self._stream.codec_context
        codec.time_base = TIME_BASE  # timestamps in milliseconds of the video: the frame rate varies
        codec.gop_size = fps * self.options.keyframe_seconds
        codec.colorspace = Colorspace.ITU709
        codec.color_primaries = ColorPrimaries.BT709
        codec.color_trc = ColorTrc.BT709
        codec.color_range = ColorRange.MPEG

    def _capture_loop(self) -> None:
        # windows-capture's DXGI objects must be created, used and dropped on one thread.
        # Its Rust panics arrive as BaseException, hence the broad excepts below.
        try:
            source = open_source(self.monitor, self.rect)
        except BaseException as error:
            self._error = error
            self._source_ready.set()
            return
        self.source_name = source.name
        self._source_ready.set()

        # Up to twice the frame rate: the encoder evens out the rest.
        min_interval = 0.5 / self.options.fps
        kernel = sharpen_kernel(self.options.sharpness) if self.options.sharpness > 0 else None
        number = 0
        try:
            while not self._stop.is_set():
                if self.paused:
                    self._stop.wait(0.05)
                    continue
                began = time.perf_counter()
                image = source.grab()
                if image is None:
                    source.release()
                    self._stop.wait(0.004)  # screen unchanged: look again shortly
                    continue
                number += 1
                small = cv2.resize(image, self.size, interpolation=cv2.INTER_AREA)
                if kernel is not None:
                    small = cv2.filter2D(small, -1, kernel)
                self._latest = (small, number, began)
                del image  # the source may reuse or unmap the buffer after release()
                source.release()
                self._stop.wait(min_interval - (time.perf_counter() - began))
        except BaseException as error:
            log.exception("Capture failed")
            self._error = error
            self._stop.set()
        finally:
            source.close()

    def _encode_loop(self) -> None:
        fps, speed = self.options.fps, self.options.speed
        min_gap = 0.9 / fps  # a screen faster than fps is thinned out evenly, give or take jitter
        painter = CursorPainter(self.divisor) if self.options.draw_cursor else None
        inputs = (
            InputPainter(self._inputs, self.divisor, self.size, self.options.zoom, self.options.click_colors)
            if self._inputs else None
        )
        origin = (self.monitor.left + self.rect[0], self.monitor.top + self.rect[1])
        last_pts = -1
        last_time = -math.inf  # recording time of the last frame written
        last_number = 0
        last_overlay: object = None  # what the pointer, clicks and keystrokes looked like then
        error = None
        try:
            while True:
                stopping = self._stop.wait(0.004)
                latest = self._latest
                if latest is not None and not self.paused:
                    image, number, captured = latest
                    now = self.elapsed
                    overlay = (winapi.cursor_state() if painter else None, inputs.busy() if inputs else False)
                    if number != last_number and now >= last_time + min_gap:
                        # A new screen frame, stamped when it was captured (pauses taken out).
                        at = max(captured - self._started - self._paused_total, last_time + min_gap, 0.0)
                    elif (overlay != last_overlay or overlay[1] or stopping) and now >= last_time + 1 / fps:
                        at = now  # the same screen, but the pointer moved, a click spreads, keys fade...
                    else:
                        at = None
                    if stopping and at is None and now > last_time:
                        at = now  # holds the last frame until the moment recording stopped
                    if at is not None:
                        pts = max(round(at / speed / TIME_BASE), last_pts + 1)
                        self._encode(image, pts, painter, inputs, origin)
                        last_pts, last_time, last_number, last_overlay = pts, at, number, overlay
                        self.frames += 1
                if stopping:
                    break
            for packet in self._stream.encode():
                self._container.mux(packet)
        except Exception as exc:
            log.exception("Encoding failed")
            error = exc
        finally:
            self._stop.set()
            self._capture_thread.join(timeout=2)
            if self._inputs:
                self._inputs.stop()
            try:
                self._container.close()
            except Exception as exc:
                log.exception("Closing the file failed")
                error = error or exc
        if last_pts < 0 and not self._error:
            self.path.unlink(missing_ok=True)
            error = error or CaptureError("Не вдалося отримати жодного кадру з екрана")
        self.duration = max(0.0, float(last_pts * TIME_BASE))  # of the video, after the speed change
        log.info("Recording finished: %.1f s, %d frames (%.1f fps on average)", self.duration, self.frames,
                 self.frames / max(self.duration, 1e-3))
        self._on_finished(self, self._error or error)

    def _encode(
        self, frame: np.ndarray, pts: int, painter: CursorPainter | None, inputs: InputPainter | None,
        origin: tuple[int, int],
    ) -> None:
        # The capture thread never touches a frame once published, so drawing on it is safe
        # as long as the pixels go back before the next tick. Clicks go under the pointer,
        # keystrokes over everything.
        restores = inputs.paint_clicks(frame, *origin) if inputs else []
        if painter:
            restores.append(painter.paint(frame, *origin))
        if inputs:
            restores += inputs.paint_keys(frame)
        try:
            video_frame = av.VideoFrame.from_numpy_buffer(frame, format="bgra").reformat(
                format="yuv420p",
                src_colorspace=Colorspace.ITU709,
                dst_colorspace=Colorspace.ITU709,
                src_color_range=ColorRange.JPEG,
                dst_color_range=ColorRange.MPEG,
            )
        finally:
            for restore in reversed(restores):
                if restore:
                    restore()
        video_frame.pts = pts
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)
