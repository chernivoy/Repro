"""The splash the one-file exe shows while it unpacks itself (several seconds), used at build time.

PyInstaller's bootloader shows it with Tcl/Tk before Python starts, so it is a picture plus a Tcl
script. The exe is marked DPI-aware (see Repro.spec), so the picture is drawn at several
scales, stacked in one image; the script picks the one for the screen's DPI, so it is sharp at
100% as well as on a 200% Retina session, and slides a small bar along a track meanwhile."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from .icons import make_icon
from .theme import pil_font

SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
WIDTH, HEIGHT, RADIUS = 380, 236, 18  # logical pixels
TRACK_WIDTH, PILL_WIDTH, BAR_HEIGHT, TRACK_TOP = 120, 40, 4, 206
TITLE, SUBTITLE = "Repro", "Запис екрана"


def _rounded_distance(width: int, height: int, radius: float) -> np.ndarray:
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    qx = np.abs(xs - width / 2) - (width / 2 - radius)
    qy = np.abs(ys - height / 2) - (height / 2 - radius)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius


def _bar(width: int, height: int, colors: tuple[tuple[int, int, int], tuple[int, int, int]]) -> Image.Image:
    """A rounded bar, a horizontal gradient, antialiased edges in its alpha."""
    amount = np.linspace(0, 1, width, dtype=np.float32)[None, :, None]
    start, end = (np.array(color, np.float32) for color in colors)
    rgb = np.broadcast_to(start * (1 - amount) + end * amount, (height, width, 3))
    alpha = np.clip(0.5 - _rounded_distance(width, height, height / 2), 0, 1) * 255
    return Image.fromarray(np.dstack([rgb, alpha]).round().astype(np.uint8), "RGBA")


def render(scale: float) -> tuple[Image.Image, Image.Image, dict[str, int]]:
    """The splash and the sliding bar at one scale, and where the track is."""
    width, height = round(WIDTH * scale), round(HEIGHT * scale)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    # Graphite, lighter at the top, with an indigo glow from above.
    amount = (ys / height)[..., None]
    rgb = np.array((52, 53, 61), np.float32) * (1 - amount) + np.array((36, 37, 43), np.float32) * amount
    glow = np.clip(1 - np.hypot(xs - width / 2, (ys + height * 0.25) * 1.4) / (width * 0.7), 0, 1)[..., None] ** 2
    rgb = rgb + (np.array((104, 117, 245), np.float32) - rgb) * glow * 0.35
    # Rounded corners. The bootloader keys out one colour for transparency, so a pixel is either
    # in or out; at the drawn scale the steps are a physical pixel each.
    distance = _rounded_distance(width, height, RADIUS * scale)
    rim = np.clip(0.5 - np.abs(distance + 0.5 * scale) + 0.5 * scale, 0, 1)[..., None]
    rgb = rgb + (255 - rgb) * rim * 0.1
    alpha = np.where(distance <= 0, 255, 0)
    image = Image.fromarray(np.dstack([rgb, alpha]).round().astype(np.uint8), "RGBA")

    icon_size = round(80 * scale)
    icon = make_icon(False, icon_size)
    image.alpha_composite(icon, ((width - icon_size) // 2, round(34 * scale)))
    pen = ImageDraw.Draw(image)
    pen.text((width / 2, 146 * scale), TITLE, font=pil_font(round(28 * scale), "display"), fill=(243, 244, 248),
             anchor="ms")
    pen.text((width / 2, 174 * scale), SUBTITLE, font=pil_font(round(13 * scale)), fill=(166, 169, 180), anchor="ms")

    track_width, bar_height = round(TRACK_WIDTH * scale), max(2, round(BAR_HEIGHT * scale))
    track = {"x": (width - track_width) // 2, "y": round(TRACK_TOP * scale), "width": track_width}
    image.alpha_composite(_bar(track_width, bar_height, ((63, 65, 73), (63, 65, 73))), (track["x"], track["y"]))
    pill = _bar(round(PILL_WIDTH * scale), bar_height, ((122, 133, 255), (167, 123, 255)))
    return image, pill, track


def save_splash(path: str) -> str:
    """Saves the stacked picture and returns the Tcl script that shows it."""
    parts, layouts, top = [], [], 0
    for scale in SCALES:
        image, pill, track = render(scale)
        layouts.append(
            f"{{{scale} {top} {image.width} {image.height} {track['x']} {track['y']} {track['width']} "
            f"{top + image.height} {pill.width} {pill.height}}}"
        )
        parts += [(image, top), (pill, top + image.height)]
        top += image.height + pill.height
    strip = Image.new("RGBA", (max(image.width for image, _ in parts), top), (0, 0, 0, 0))
    for image, y in parts:
        strip.paste(image, (0, y))
    strip.save(path, optimize=True)
    return SCRIPT.replace("@LAYOUTS@", " ".join(layouts))


# Based on PyInstaller's splash_templates.py: the same IPC server (the app closes the splash
# through it), image data and centring, plus picking the scale and the sliding bar.
SCRIPT = r"""
package require Tk

proc _ipc_server {channel clientaddr clientport} {
    set client_name [format <%s:%d> $clientaddr $clientport]
    chan configure $channel -buffering none -encoding utf-8 -eofchar \x04 -translation cr
    chan event $channel readable [list _ipc_caller $channel $client_name]
}

proc _ipc_caller {channel client_name} {
    chan gets $channel cmd
    if {[chan eof $channel]} {
        chan close $channel
        exit
    }
}

set server_socket [socket -server _ipc_server -myaddr localhost 0]
set server_port [fconfigure $server_socket -sockname]
set env(_PYI_SPLASH_IPC) [lindex $server_port 2]

image create photo splash_strip
splash_strip put $_image_data
unset _image_data

# Each layout: scale, top in the strip, width, height, track x, y and width, then the bar's
# top in the strip, width and height.
set layouts {@LAYOUTS@}
set dpi_scale [expr {[winfo fpixels . 1i] / 96.0}]
set best_distance 1e9
foreach layout $layouts {
    set distance [expr {abs([lindex $layout 0] - $dpi_scale)}]
    if {$distance < $best_distance} {
        set best $layout
        set best_distance $distance
    }
}
lassign $best scale top image_width image_height track_x track_y track_width bar_top bar_width bar_height

image create photo splash_image -width $image_width -height $image_height
splash_image copy splash_strip -from 0 $top $image_width [expr {$top + $image_height}]
image create photo splash_bar -width $bar_width -height $bar_height
splash_bar copy splash_strip -from 0 $bar_top $bar_width [expr {$bar_top + $bar_height}]
image delete splash_strip

if {[info exists _pyi_screen_geometry]} {
    set display_x $_pyi_screen_geometry(x)
    set display_y $_pyi_screen_geometry(y)
    set display_width $_pyi_screen_geometry(width)
    set display_height $_pyi_screen_geometry(height)
} else {
    set display_x 0
    set display_y 0
    set display_width [winfo screenwidth .]
    set display_height [winfo screenheight .]
}
set x_position [expr {int($display_x + 0.5 * ($display_width - $image_width))}]
set y_position [expr {int($display_y + 0.5 * ($display_height - $image_height))}]

frame .root
canvas .root.canvas -width $image_width -height $image_height -borderwidth 0 -highlightthickness 0
.root.canvas create image 0 0 -anchor nw -image splash_image
.root.canvas create image $track_x $track_y -anchor nw -image splash_bar -tags bar

wm attributes . -transparentcolor magenta
.root.canvas configure -background magenta
pack .root
grid .root.canvas -column 0 -row 0

wm overrideredirect . 1
wm geometry . +${x_position}+${y_position}
wm attributes . -topmost 1
raise .

# The bar glides to the end of the track and back while the app unpacks.
set started [clock milliseconds]
proc slide {} {
    global started track_x track_y track_width bar_width
    set phase [expr {0.5 - 0.5 * cos(([clock milliseconds] - $started) / 1100.0 * 3.14159265)}]
    .root.canvas coords bar [expr {$track_x + round(($track_width - $bar_width) * $phase)}] $track_y
    after 16 slide
}
slide
"""
