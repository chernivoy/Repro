"""App icon drawn in code: an indigo tile with viewfinder corners around a record dot, and a red
tile with a stop square while recording. Each size is drawn on its own, so small ones stay clean."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

INDIGO = ((104, 117, 245), (124, 77, 230))  # top-left, bottom-right
RED = ((255, 106, 92), (220, 38, 54))
WHITE = (255, 255, 255, 255)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 4


@lru_cache(maxsize=32)
def make_icon(recording: bool, size: int = 64) -> Image.Image:
    big = size * SUPERSAMPLE
    image = _tile(big, RED if recording else INDIGO)
    pen = ImageDraw.Draw(image)
    middle = big / 2
    if size >= 32:  # the viewfinder corners would only blur a small icon
        _corners(pen, big)
    if recording:
        half = big * (0.14 if size >= 32 else 0.2)
        pen.rounded_rectangle((middle - half, middle - half, middle + half, middle + half), radius=big * 0.05, fill=WHITE)
    else:
        image = _record_dot(image, big, big * (0.17 if size >= 32 else 0.25))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _squircle(size: int, inset: float = 0.0) -> Image.Image:
    """Mask of the superellipse |x|^5 + |y|^5 <= 1: a rounded square with smooth, continuous corners."""
    centre, radius = (size - 1) / 2, size / 2 - inset
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    inside = (np.abs((xs - centre) / radius) ** 5 + np.abs((ys - centre) / radius) ** 5) <= 1
    return Image.fromarray(inside.astype(np.uint8) * 255, "L")


def _gradient(size: int, colors: tuple[tuple[int, int, int], tuple[int, int, int]]) -> np.ndarray:
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32) / max(1, size - 1)
    amount = np.clip(ys * 0.65 + xs * 0.35, 0, 1)[..., None]
    start, end = (np.array(color, np.float32) for color in colors)
    return start * (1 - amount) + end * amount


def _tile(size: int, colors) -> Image.Image:
    rgb = _gradient(size, colors)
    # A soft sheen fading out towards the middle, for a little depth.
    sheen = np.clip(1 - np.mgrid[0:size, 0:1][0].astype(np.float32) / (size * 0.6), 0, 1)[..., None] * 0.16
    rgb = rgb + (255 - rgb) * sheen
    mask = _squircle(size)
    tile = Image.fromarray(np.round(rgb).astype(np.uint8), "RGB").convert("RGBA")
    tile.putalpha(mask)
    # A faint light rim along the edge.
    rim = np.asarray(mask, np.float32) - np.asarray(_squircle(size, inset=size * 0.012), np.float32)
    edge = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    edge.putalpha(Image.fromarray(np.round(np.clip(rim, 0, 255) * 0.25).astype(np.uint8), "L"))
    return Image.alpha_composite(tile, edge)


def _corners(pen: ImageDraw.ImageDraw, size: int) -> None:
    inset, arm, thickness = size * 0.2, size * 0.15, size * 0.055
    for x in (inset, size - inset):
        for y in (inset, size - inset):
            dx, dy = (1 if x < size / 2 else -1), (1 if y < size / 2 else -1)
            for width, height in ((arm, thickness), (thickness, arm)):
                x1, y1 = x + dx * width, y + dy * height
                pen.rounded_rectangle((min(x, x1), min(y, y1), max(x, x1), max(y, y1)), radius=thickness / 2, fill=WHITE)


def _record_dot(image: Image.Image, size: int, radius: float) -> Image.Image:
    middle = size / 2
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    reach = radius * 1.3
    ImageDraw.Draw(glow).ellipse((middle - reach, middle - reach, middle + reach, middle + reach), fill=(255, 60, 60, 110))
    image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(size * 0.05)))
    ring = radius + size * (0.045 if radius < size * 0.2 else 0.055)
    ImageDraw.Draw(image).ellipse((middle - ring, middle - ring, middle + ring, middle + ring), fill=WHITE)
    # The dot lit from the top left.
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    amount = np.clip(np.hypot(xs - (middle - radius * 0.35), ys - (middle - radius * 0.35)) / (radius * 1.6), 0, 1)[..., None]
    start, end = np.array((255, 122, 110), np.float32), np.array((226, 36, 48), np.float32)
    dot = Image.fromarray(np.round(start * (1 - amount) + end * amount).astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((middle - radius, middle - radius, middle + radius, middle + radius), fill=255)
    dot.putalpha(mask)
    return Image.alpha_composite(image, dot)


def save_ico(path: str, recording: bool = False) -> None:
    """The exe's icon, and the tray's: Windows picks the size it shows from the same file."""
    images = [make_icon(recording, size) for size in ICO_SIZES]
    images[-1].save(path, sizes=[(size, size) for size in ICO_SIZES], append_images=images[:-1])
