"""Light and dark themes, fonts, and crisp shapes for the app's windows.

Sizes are given in logical pixels (as at 100%) and turned into physical ones with the scale of
the monitor a window is on, so text and controls keep their size on every monitor. Fonts are
sized in pixels for the same reason: Tk's point sizes follow one DPI for the whole process."""

from __future__ import annotations

import tkinter.font as tkfont
import winreg
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFont, ImageTk


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    bg: str  # window background
    surface: str  # cards
    control: str  # segmented tracks, secondary buttons, fields
    control_hover: str
    thumb: str  # the selected segment
    knob: str  # slider thumb
    nav_hover: str
    border: str
    divider: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_hover: str
    on_accent: str
    danger: str
    danger_hover: str
    warning: str
    success: str
    track: str  # slider rail
    panel: str  # floating panels: the capture HUD, the recording bar
    tile: str
    tile_hover: str
    field: str


LIGHT = Theme(
    name="light", dark=False,
    bg="#f3f4f7", surface="#ffffff", control="#eceef3", control_hover="#e2e5ec", thumb="#ffffff",
    knob="#ffffff", nav_hover="#e8eaf0", border="#e0e2e9", divider="#eceef2",
    text="#1b1c22", muted="#5f6371", faint="#9a9eab",
    accent="#5458e0", accent_hover="#4448cf", on_accent="#ffffff",
    danger="#e5484d", danger_hover="#d23a40", warning="#e08a00", success="#2fb26a",
    track="#cdd0d9", panel="#f3f4f7", tile="#ffffff", tile_hover="#e8eaf0", field="#eceef3",
)
# Graphite rather than black: easier on the eyes, and cards still stand out from the window.
DARK = Theme(
    name="dark", dark=True,
    bg="#2b2c32", surface="#35363d", control="#3f4149", control_hover="#494b54", thumb="#5b5d68",
    knob="#4a4c56", nav_hover="#313238", border="#42444c", divider="#3e4047",
    text="#eeeff3", muted="#a6a9b4", faint="#7c7f8a",
    accent="#7378f5", accent_hover="#8287ff", on_accent="#ffffff",
    danger="#ef5157", danger_hover="#ff6469", warning="#ffb340", success="#3ccf7e",
    track="#5b5e69", panel="#2b2c32", tile="#3a3b43", tile_hover="#45474f", field="#2f3036",
)
THEMES = {"light": LIGHT, "dark": DARK}

_current = DARK


def current() -> Theme:
    return _current


def use(name: str) -> Theme:
    global _current
    _current = THEMES.get(name) or THEMES[system_theme()]
    return _current


def system_theme() -> str:
    """The app mode chosen in Windows settings (Personalization > Colors)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


# --- Fonts -----------------------------------------------------------------------

# Windows 11's Segoe UI Variable, falling back to Segoe UI on Windows 10.
_FAMILIES = {
    "regular": ("Segoe UI Variable Text", "Segoe UI"),
    "semibold": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "display": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
    "icons": ("Segoe Fluent Icons", "Segoe MDL2 Assets"),
}
_families: dict[str, str] = {}


def font(size: float, scale: float, weight: str = "regular") -> tuple[str, int]:
    """`size` in logical pixels; `weight`: regular, semibold, display or icons."""
    if not _families:  # needs Tk running, so it is looked up on first use
        available = set(tkfont.families())
        for name, candidates in _FAMILIES.items():
            _families[name] = next((family for family in candidates if family in available), "Segoe UI")
    return _families[weight], -max(1, round(size * scale))


_PIL_FONTS = {  # for text drawn into images: file, named instance of the variable font
    "regular": (("SegUIVar.ttf", "Regular"), ("segoeui.ttf", None)),
    "semibold": (("SegUIVar.ttf", "Semibold Text"), ("seguisb.ttf", None)),
    "display": (("SegUIVar.ttf", "Semibold Display"), ("seguisb.ttf", None)),
}


def pil_font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    for file, instance in _PIL_FONTS[weight]:
        try:
            loaded = ImageFont.truetype(file, size)
            if instance:
                loaded.set_variation_by_name(instance)
            return loaded
        except (OSError, ValueError):
            continue
    return ImageFont.load_default(size)


class Ui:
    """Theme, scale and fonts for one window."""

    def __init__(self, scale: float, theme: Theme | None = None) -> None:
        self.scale = scale
        self.theme = theme or _current
        self._measures: dict[tuple, tkfont.Font] = {}

    def px(self, value: float) -> int:
        return max(1, round(value * self.scale))

    def font(self, size: float, weight: str = "regular") -> tuple[str, int]:
        return font(size, self.scale, weight)

    def measure(self, text: str, spec: tuple[str, int]) -> int:
        if spec not in self._measures:
            self._measures[spec] = tkfont.Font(font=spec)
        return self._measures[spec].measure(text)

    def line_height(self, spec: tuple[str, int]) -> int:
        self.measure("", spec)
        return self._measures[spec].metrics("linespace")


# --- Shapes ------------------------------------------------------------------------


def rgb(color: str) -> np.ndarray:
    return np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], np.float32)


def mix(first: str, second: str, amount: float) -> str:
    """`first` moved `amount` of the way to `second`."""
    a, b = rgb(first), rgb(second)
    red, green, blue = np.round(a + (b - a) * amount).astype(int)
    return f"#{red:02x}{green:02x}{blue:02x}"


class Paint:
    """Antialiased rounded rectangles and circles on a solid background, drawn from signed
    distances: exact coverage at any size, and fast enough to redraw controls as they change."""

    def __init__(self, width: int, height: int, background: str) -> None:
        self.width, self.height = max(1, width), max(1, height)
        self.pixels = np.empty((self.height, self.width, 3), np.float32)
        self.pixels[:] = rgb(background)

    def rounded(
        self, box: tuple[float, float, float, float], radius: float, fill: str | None = None,
        outline: str | None = None, line: float = 0.0, alpha: float = 1.0,
    ) -> None:
        left, top, right, bottom = box
        radius = max(0.0, min(radius, (right - left) / 2, (bottom - top) / 2))
        area = self._area(left, top, right, bottom)
        if area is None:
            return
        xs, ys, view = area
        qx = np.abs(xs - (left + right) / 2) - ((right - left) / 2 - radius)
        qy = np.abs(ys - (top + bottom) / 2) - ((bottom - top) / 2 - radius)
        distance = (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius)
        self._shape(view, distance, fill, outline, line, alpha)

    def circle(
        self, x: float, y: float, radius: float, fill: str | None = None, outline: str | None = None,
        line: float = 0.0, alpha: float = 1.0,
    ) -> None:
        area = self._area(x - radius, y - radius, x + radius, y + radius)
        if area is None:
            return
        xs, ys, view = area
        self._shape(view, np.hypot(xs - x, ys - y) - radius, fill, outline, line, alpha)

    def _area(self, left: float, top: float, right: float, bottom: float):
        """Pixel centres around a shape, and the matching view of the pixels."""
        x0, y0 = max(0, int(left) - 1), max(0, int(top) - 1)
        x1, y1 = min(self.width, int(right) + 2), min(self.height, int(bottom) + 2)
        if x0 >= x1 or y0 >= y1:
            return None
        ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32) + 0.5
        return xs, ys, self.pixels[y0:y1, x0:x1]

    @staticmethod
    def _shape(view: np.ndarray, distance: np.ndarray, fill, outline, line: float, alpha: float) -> None:
        total = np.clip(0.5 - distance, 0, 1)[..., None] * alpha
        if outline and line > 0:
            inner = np.clip(0.5 - (distance + line), 0, 1)[..., None] * alpha
            inside = rgb(fill) * inner if fill else view * inner
            view[:] = view * (1 - total) + rgb(outline) * (total - inner) + inside
        elif fill:
            view[:] = view * (1 - total) + rgb(fill) * total

    def image(self) -> Image.Image:
        return Image.fromarray(np.round(self.pixels).astype(np.uint8), "RGB")

    def photo(self) -> ImageTk.PhotoImage:
        return ImageTk.PhotoImage(self.image())
