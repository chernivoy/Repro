"""Themed controls, drawn as antialiased images so they look alike in both themes and stay crisp
at any scale: rounded cards, tiles and buttons, segmented choices, switches, a step slider, and
tooltips. Sizes are in logical pixels; each control gets the window's Ui for scale and colours."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence

from . import winapi
from .theme import Paint, Ui, mix

ANIMATION_STEP_MS = 12


def _size(widget: tk.Canvas) -> tuple[int, int]:
    """The canvas's size once laid out, the size it asked for before that."""
    width, height = widget.winfo_width(), widget.winfo_height()
    if width <= 1 or height <= 1:
        return int(widget["width"]), int(widget["height"])
    return width, height


def _inside(widget: tk.Widget, event: tk.Event) -> bool:
    return 0 <= event.x < widget.winfo_width() and 0 <= event.y < widget.winfo_height()


class Tooltip:
    """A themed tip under a widget after a short hover. `exclude` keeps it out of screen capture,
    for controls shown while recording."""

    def __init__(self, widget: tk.Widget, text: str, *parts: tk.Widget, exclude: bool = False) -> None:
        self._widget, self.text = widget, text
        self._exclude = exclude
        self._window: tk.Toplevel | None = None
        self._job: str | None = None
        for target in (widget, *parts):
            target.bind("<Enter>", self._schedule, add="+")
            target.bind("<Leave>", self.hide, add="+")
            target.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: tk.Event) -> None:
        self.hide()
        self._job = self._widget.after(450, self._show)

    def _show(self) -> None:
        self._job = None
        widget = self._widget
        below = widget.winfo_rooty() + widget.winfo_height()
        (left, top, right, bottom), scale = winapi.monitor_at(widget.winfo_rootx(), below)
        ui = Ui(scale)
        theme = ui.theme
        window = self._window = tk.Toplevel(widget, bg=theme.surface)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        tk.Label(
            window, text=self.text, bg=theme.surface, fg=theme.text, font=ui.font(12), justify="left",
            wraplength=ui.px(300), padx=ui.px(10), pady=ui.px(6),
        ).pack()
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = min(max(left, widget.winfo_rootx() + (widget.winfo_width() - width) // 2), right - width)
        y = below + ui.px(6)
        if y + height > bottom:
            y = widget.winfo_rooty() - height - ui.px(6)
        window.geometry(f"+{x}+{y}")
        winapi.round_corners(window.winfo_id(), border=theme.border)
        if self._exclude:
            winapi.exclude_from_capture(window.winfo_id())
        window.deiconify()

    def hide(self, _event: tk.Event | None = None) -> None:
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._window:
            self._window.destroy()
            self._window = None


class Rounded(tk.Frame):
    """A frame with antialiased rounded corners, laid over its square ones, and an optional
    border. The content goes into `.body`, kept clear of the corners by the padding."""

    def __init__(
        self, parent: tk.Widget, ui: Ui, fill: str, outside: str, radius: float = 8, border: str | None = None,
        padx: float = 0, pady: float = 0,
    ) -> None:
        line = ui.px(1) if border else 0
        super().__init__(parent, bg=fill, bd=0, highlightthickness=line, highlightbackground=border or fill,
                         highlightcolor=border or fill)
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=ui.px(padx) if padx else 0, pady=ui.px(pady) if pady else 0)
        r = ui.px(radius)
        self._photos = []
        for anchor in ("nw", "ne", "sw", "se"):
            dx, dy = (0 if "w" in anchor else -r), (0 if "n" in anchor else -r)
            paint = Paint(r, r, outside)
            paint.rounded((dx, dy, dx + 2 * r, dy + 2 * r), r, fill=fill, outline=border, line=line)
            photo = paint.photo()
            self._photos.append(photo)
            tk.Label(self, image=photo, bd=0, highlightthickness=0, padx=0, pady=0, bg=outside).place(
                relx=1.0 if "e" in anchor else 0.0, rely=1.0 if "s" in anchor else 0.0, anchor=anchor,
                bordermode="ignore",
            )


class Tile(tk.Canvas):
    """A rounded tile, clickable when it has a command. Items are placed relative to its size:
    x = relx * width + dx, y = height / 2 + dy, with dx and dy in logical pixels; `decorate`
    draws extra shapes into its background."""

    def __init__(
        self, parent: tk.Widget, ui: Ui, *, bg: str, fill: str, hover: str | None = None,
        command: Callable[[], None] | None = None, width: float = 40, height: float = 40, radius: float = 8,
        outline: str | None = None, decorate: Callable[[Paint, int, int], None] | None = None,
    ) -> None:
        super().__init__(parent, width=ui.px(width), height=ui.px(height), bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2" if command else "arrow")
        self._ui = ui
        self._bg = bg
        self._fill, self._hover_fill = fill, hover or fill
        self._outline = outline
        self._radius = ui.px(radius)
        self.command = command
        self.decorate = decorate
        self._hovered = False
        self._painted: tuple | None = None
        self._photo = None
        self._image = self.create_image(0, 0, anchor="nw")
        self._places: dict[int, tuple[float, float, float]] = {}
        self.bind("<Configure>", lambda _event: self._layout())
        self.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self._paint()

    def text(
        self, text: str, font: tuple, color: str, relx: float = 0.5, dx: float = 0, dy: float = 0,
        anchor: str = "center",
    ) -> int:
        item = self.create_text(0, 0, text=text, font=font, fill=color, anchor=anchor)
        self._places[item] = (relx, dx, dy)
        self._layout()
        return item

    def set_fill(self, fill: str, hover: str | None = None) -> None:
        self._fill, self._hover_fill = fill, hover or fill
        self.repaint()

    def repaint(self) -> None:
        self._painted = None
        self._paint()

    def _release(self, event: tk.Event) -> None:
        if self.command and _inside(self, event):
            self.command()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _layout(self) -> None:
        width, height = _size(self)
        scale = self._ui.scale
        for item, (relx, dx, dy) in self._places.items():
            self.coords(item, relx * width + round(dx * scale), height / 2 + round(dy * scale))
        self._paint()

    def _paint(self) -> None:
        width, height = _size(self)
        color = self._hover_fill if self._hovered else self._fill
        key = (width, height, color)
        if key == self._painted:
            return
        paint = Paint(width, height, self._bg)
        paint.rounded((0, 0, width, height), self._radius, fill=color, outline=self._outline,
                      line=self._ui.px(1) if self._outline else 0)
        if self.decorate:
            self.decorate(paint, width, height)
        self._photo = paint.photo()
        self.itemconfigure(self._image, image=self._photo)
        self._painted = key


def button(
    parent: tk.Widget, ui: Ui, text: str, command: Callable[[], None], *, bg: str, kind: str = "secondary",
    icon: str = "", width: float | None = None, height: float = 32, size: float = 13, align: str = "center",
) -> Tile:
    """Primary (accent), secondary, danger, or subtle (shows only on hover) button."""
    theme = ui.theme
    outline = None
    if kind == "secondary" and not theme.dark:  # white with a hairline, as in Windows 11
        fill, hover, ink, outline = theme.surface, theme.control, theme.text, theme.border
    else:
        fill, hover, ink = {
            "primary": (theme.accent, theme.accent_hover, theme.on_accent),
            "secondary": (theme.control, theme.control_hover, theme.text),
            "danger": (theme.danger, theme.danger_hover, "#ffffff"),
            "subtle": (bg, theme.nav_hover, theme.text),
        }[kind]
    text_font = ui.font(size, "semibold" if kind in ("primary", "danger") else "regular")
    icon_font = ui.font(size + 2, "icons")
    gap = 8
    icon_width = ui.measure(icon, icon_font) / ui.scale + gap if icon else 0
    content = icon_width + ui.measure(text, text_font) / ui.scale
    padding = 14
    tile = Tile(parent, ui, bg=bg, fill=fill, hover=hover, command=command, width=width or content + 2 * padding,
                height=height, radius=6, outline=outline)
    relx, dx = (0.0, padding) if align == "left" else (0.5, -content / 2)
    if icon:
        tile.text(icon, icon_font, ink, relx=relx, dx=dx, anchor="w")
    tile.text(text, text_font, ink, relx=relx, dx=dx + icon_width, anchor="w")
    return tile


class Segmented(tk.Canvas):
    """One of a few options, like a row of tabs; the selection slides to the clicked one."""

    def __init__(
        self, parent: tk.Widget, ui: Ui, options: Sequence[str], index: int, on_change: Callable[[int], None], *,
        bg: str, width: float | None = None, icons: Sequence[str] | None = None, height: float = 32,
        size: float = 13,
    ) -> None:
        theme = ui.theme
        self._ui = ui
        self._choices = list(options)
        self._icons = list(icons) if icons else None
        self._index = index
        self._on_change = on_change
        self._enabled = True
        self._hover: int | None = None
        self._job: str | None = None
        self._fonts = (ui.font(size), ui.font(size, "semibold"))
        self._icon_font = ui.font(size + 1, "icons")
        self._gap = ui.px(6)
        self._inset = ui.px(3)
        count = len(self._choices)
        widest = max(self._content_width(i, self._fonts[1]) for i in range(count))
        if width:
            total = ui.px(width)
            self._segment = (total - 2 * self._inset) / count
        else:
            self._segment = widest + 2 * ui.px(12)
            total = round(self._segment * count + 2 * self._inset)
        tall = ui.px(height)
        super().__init__(parent, width=total, height=tall, bg=bg, highlightthickness=0, bd=0, cursor="hand2")

        track = Paint(total, tall, bg)
        track.rounded((0, 0, total, tall), ui.px(7), fill=theme.control)
        thumb_width, thumb_height = round(self._segment), tall - 2 * self._inset
        thumb = Paint(thumb_width, thumb_height, theme.control)
        thumb.rounded((0, 0, thumb_width, thumb_height), ui.px(5), fill=theme.thumb,
                      outline=None if theme.dark else theme.border, line=ui.px(1))
        self._photos = (track.photo(), thumb.photo())
        self.create_image(0, 0, anchor="nw", image=self._photos[0])
        self._thumb = self.create_image(self._thumb_x(index), self._inset, anchor="nw", image=self._photos[1])
        self._items = [
            (self.create_text(0, tall / 2, text=self._icons[i], font=self._icon_font, anchor="w") if self._icons else None,
             self.create_text(0, tall / 2, text=option, anchor="w"))
            for i, option in enumerate(self._choices)
        ]
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _event: self._set_hover(None))
        self.bind("<ButtonRelease-1>", self._on_release)
        self._style()

    @property
    def selected(self) -> int:
        return self._index

    def set(self, index: int) -> None:
        """Shows an option chosen elsewhere, without calling on_change."""
        if index != self._index:
            self._index = index
            self._cancel()
            self.coords(self._thumb, self._thumb_x(index), self._inset)
            self._style()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._style()

    def _content_width(self, index: int, text_font: tuple) -> float:
        width = self._ui.measure(self._choices[index], text_font)
        if self._icons:
            width += self._ui.measure(self._icons[index], self._icon_font) + self._gap
        return width

    def _thumb_x(self, index: float) -> float:
        return self._inset + self._segment * index

    def _at(self, x: int) -> int:
        return min(len(self._choices) - 1, max(0, int((x - self._inset) // self._segment)))

    def _on_motion(self, event: tk.Event) -> None:
        self._set_hover(self._at(event.x))

    def _set_hover(self, index: int | None) -> None:
        if index != self._hover:
            self._hover = index
            self._style()

    def _on_release(self, event: tk.Event) -> None:
        if not self._enabled or not _inside(self, event):
            return
        index = self._at(event.x)
        if index != self._index:
            start = self._thumb_x(self._index)
            self._index = index
            self._style()
            self._slide(start, self._thumb_x(index), 1)
            self._on_change(index)

    def _slide(self, start: float, end: float, step: int, steps: int = 9) -> None:
        progress = 1 - (1 - step / steps) ** 3  # ease out
        self.coords(self._thumb, round(start + (end - start) * progress), self._inset)
        self._job = self.after(ANIMATION_STEP_MS, self._slide, start, end, step + 1) if step < steps else None

    def _cancel(self) -> None:
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    def destroy(self) -> None:
        # Picking the theme rebuilds the window around this very control, mid-slide.
        self._cancel()
        super().destroy()

    def _style(self) -> None:
        theme = self._ui.theme
        middle = _size(self)[1] / 2
        for i, (icon, text) in enumerate(self._items):
            chosen = i == self._index
            text_font = self._fonts[1] if chosen else self._fonts[0]
            if not self._enabled:
                color = theme.faint
            else:
                color = theme.text if chosen or i == self._hover else theme.muted
            x = self._inset + self._segment * (i + 0.5) - self._content_width(i, text_font) / 2
            if icon:
                self.itemconfigure(icon, fill=theme.accent if chosen and self._enabled else color)
                self.coords(icon, x, middle)
                x += self._ui.measure(self._icons[i], self._icon_font) + self._gap
            self.itemconfigure(text, font=text_font, fill=color)
            self.coords(text, x, middle)


class Switch(tk.Canvas):
    """An on/off switch like Windows 11's; the knob slides."""

    def __init__(self, parent: tk.Widget, ui: Ui, value: bool, on_change: Callable[[bool], None], *, bg: str) -> None:
        self._width, self._height = ui.px(40), ui.px(20)
        super().__init__(parent, width=self._width, height=self._height, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self._ui, self._bg = ui, bg
        self._value = value
        self._position = 1.0 if value else 0.0
        self._hovered = False
        self._on_change = on_change
        self._job: str | None = None
        self._photo = None
        self._image = self.create_image(0, 0, anchor="nw")
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda event: self._toggle() if _inside(self, event) else None)
        self._draw()

    @property
    def value(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        """Shows a value chosen elsewhere, without calling on_change."""
        if value != self._value:
            self._value = value
            self._animate()

    def _toggle(self) -> None:
        self._value = not self._value
        self._animate()
        self._on_change(self._value)

    def destroy(self) -> None:
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        super().destroy()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _animate(self) -> None:
        if self._job:
            self.after_cancel(self._job)
        target = 1.0 if self._value else 0.0
        self._position += (target - self._position) * 0.4
        if abs(target - self._position) < 0.02:
            self._position = target
            self._job = None
        else:
            self._job = self.after(ANIMATION_STEP_MS, self._animate)
        self._draw()

    def _draw(self) -> None:
        theme, ui, on = self._ui.theme, self._ui, self._position
        width, height = self._width, self._height
        paint = Paint(width, height, self._bg)
        paint.rounded((0, 0, width, height), height / 2, fill=mix(self._bg, theme.accent, on),
                      outline=mix(theme.muted, theme.accent, on), line=ui.px(1))
        radius = height / 2 - ui.px(4) + (ui.scale if self._hovered else 0)
        paint.circle(height / 2 + on * (width - height), height / 2, radius, fill=mix(theme.muted, theme.on_accent, on))
        self._photo = paint.photo()
        self.itemconfigure(self._image, image=self._photo)


class StepSlider(tk.Canvas):
    """Click or drag to pick a stop; double-click returns to `default`. The stretch from the
    default stop to the knob is highlighted, so a value away from it stands out."""

    def __init__(
        self, parent: tk.Widget, ui: Ui, values: Sequence[float], value: float, default: float,
        on_change: Callable[[float], None], *, bg: str, width: float = 160,
    ) -> None:
        super().__init__(parent, width=ui.px(width), height=ui.px(28), bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self._ui, self._bg = ui, bg
        self._values = list(values)
        self._default = self._nearest_index(default)
        self._index = self._nearest_index(value)
        self._on_change = on_change
        self._hovered = self._pressed = False
        self._photo = None
        self._image = self.create_image(0, 0, anchor="nw")
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._pick)
        self.bind("<ButtonRelease-1>", lambda _event: self._set_state(pressed=False))
        self.bind("<Double-Button-1>", lambda _event: self._choose(self._default))
        self.bind("<Enter>", lambda _event: self._set_state(hovered=True))
        self.bind("<Leave>", lambda _event: self._set_state(hovered=False))
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    @property
    def value(self) -> float:
        return self._values[self._index]

    def set(self, value: float) -> None:
        """Shows a value chosen elsewhere, without calling on_change."""
        self._index = self._nearest_index(value)
        self._draw()

    def _nearest_index(self, value: float) -> int:
        return min(range(len(self._values)), key=lambda i: abs(self._values[i] - value))

    def _x(self, index: int, width: int) -> float:
        pad = self._ui.px(11)
        return pad + index * (width - 2 * pad) / (len(self._values) - 1)

    def _press(self, event: tk.Event) -> None:
        self._pressed = True
        self._pick(event)
        self._draw()

    def _pick(self, event: tk.Event) -> None:
        width = _size(self)[0]
        self._choose(min(range(len(self._values)), key=lambda i: abs(self._x(i, width) - event.x)))

    def _set_state(self, hovered: bool | None = None, pressed: bool | None = None) -> None:
        if hovered is not None:
            self._hovered = hovered
        if pressed is not None:
            self._pressed = pressed
        self._draw()

    def _choose(self, index: int) -> None:
        if index != self._index:
            self._index = index
            self._draw()
            self._on_change(self.value)

    def _draw(self) -> None:
        ui, theme = self._ui, self._ui.theme
        width, height = _size(self)
        paint = Paint(width, height, self._bg)
        middle, rail = height / 2, ui.px(4)
        first, last = self._x(0, width), self._x(len(self._values) - 1, width)
        paint.rounded((first - rail / 2, middle - rail / 2, last + rail / 2, middle + rail / 2), rail / 2,
                      fill=theme.track)
        here, default = self._x(self._index, width), self._x(self._default, width)
        if self._index != self._default:
            paint.rounded((min(here, default) - rail / 2, middle - rail / 2, max(here, default) + rail / 2,
                           middle + rail / 2), rail / 2, fill=theme.accent)
        low, high = sorted((self._index, self._default))
        for index in range(len(self._values)):
            active = low <= index <= high and self._index != self._default
            radius = (2.2 if index == self._default else 1.4) * ui.scale
            color = mix(theme.accent, theme.on_accent, 0.7) if active else theme.faint
            paint.circle(self._x(index, width), middle, radius, fill=color)
        paint.circle(here, middle, ui.px(10), fill=theme.knob, outline=theme.border, line=ui.px(1))
        inner = 4 if self._pressed else 6 if self._hovered else 5
        paint.circle(here, middle, inner * ui.scale, fill=theme.accent)
        self._photo = paint.photo()
        self.itemconfigure(self._image, image=self._photo)
