"""CleanShot-style controls while recording: stop, timer, pause, restart and delete, and a grip
to drag the bar elsewhere. The bar starts on the recorded monitor and is excluded from screen
capture, as are its tooltips and the delete confirmation, so none of them shows up in the video.
It is drawn at the scale of the monitor it is on, and follows the theme and the interface size."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

from . import winapi
from .capture import Rect
from .theme import Paint, Ui, mix
from .widgets import Tile, Tooltip, button
from .winapi import Monitor

STOP, PAUSE, PLAY, RESTART, DELETE = "", "", "", "", ""
GAP = 10  # logical pixels between the recording frame and the bar
BLINK_MS = 700


def format_timer(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def place_bar(monitor: Monitor, rect: Rect, outer: int, width: int, height: int, margin: int) -> tuple[int, int]:
    """Screen position for the bar, always on the recorded monitor and clear of its taskbar.

    `rect` is the recorded area on `monitor`; `outer` is how far the frame plus a gap reach
    beyond it. Beside the area is preferred (below, above, right, left) so the bar does not
    cover what is being recorded; a whole monitor leaves no room, so it goes inside the area,
    near the bottom. Either way the bar is excluded from capture."""
    x, y, w, h = rect
    # The monitor's work area (clear of the taskbar), relative to the monitor.
    work_left, work_top, work_right, work_bottom = _work_area(monitor)
    work_left, work_right = work_left - monitor.left, work_right - monitor.left
    work_top, work_bottom = work_top - monitor.top, work_bottom - monitor.top
    left = min(max(work_left, x + (w - width) // 2), work_right - width)
    top = min(max(work_top, y + (h - height) // 2), work_bottom - height)
    if y + h + outer + height <= work_bottom:
        return monitor.left + left, monitor.top + y + h + outer
    if y - outer - height >= work_top:
        return monitor.left + left, monitor.top + y - outer - height
    if x + w + outer + width <= work_right:
        return monitor.left + x + w + outer, monitor.top + top
    if x - outer - width >= work_left:
        return monitor.left + x - outer - width, monitor.top + top
    return monitor.left + left, monitor.top + min(y + h, work_bottom) - height - 2 * margin


def _monitor_at(monitors: list[Monitor], x: int, y: int) -> Monitor | None:
    return next(
        (m for m in monitors if m.left <= x < m.left + m.width and m.top <= y < m.top + m.height), None
    )


def _work_area(monitor: Monitor) -> tuple[int, int, int, int]:
    return monitor.work or (monitor.left, monitor.top, monitor.left + monitor.width, monitor.top + monitor.height)


@dataclass
class BarActions:
    on_stop: Callable[[], None]
    on_pause: Callable[[], None]
    on_restart: Callable[[], None]
    on_delete: Callable[[], None]
    on_moved: Callable[[int, int], None]  # dragged to this screen position


class RecordingBar:
    def __init__(
        self, root: tk.Tk, monitor: Monitor, rect: Rect, frame_width: int, actions: BarActions,
        position: tuple[int, int] | None = None,
    ) -> None:
        """`position`: where the bar was dragged during the previous take, when restarting."""
        self._actions = actions
        self._drag: tuple[int, int, list[Monitor]] | None = None
        self._paused = False
        self._seconds = 0.0
        self._lit = True
        self._body: tk.Frame | None = None
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.screen = monitor  # where the bar is; dragging may take it to another monitor
        self._build(monitor.scale)

        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        outer = frame_width + round(GAP * monitor.scale)
        x, y = position or place_bar(monitor, rect, outer, width, height, round(GAP * monitor.scale))
        self.window.geometry(f"+{x}+{y}")
        winapi.make_no_activate(self.window.winfo_id())
        winapi.exclude_from_capture(self.window.winfo_id())
        self.window.deiconify()
        self._blink_job = self.window.after(BLINK_MS, self._blink)

    @property
    def scale(self) -> float:
        """The scale of the monitor the bar is on."""
        return self._scale

    def _build(self, scale: float) -> None:
        self._scale = scale
        ui = self._ui = Ui(scale)
        theme = ui.theme
        pad = ui.px(6)
        self.window.configure(bg=theme.panel)
        body = tk.Frame(self.window, bg=theme.panel)

        stop = self._button(body, STOP, "Зупинити й зберегти", self._actions.on_stop, fill=theme.danger,
                            hover=theme.danger_hover, ink="#ffffff", size=12)
        stop.pack(side="left", padx=(pad, 0), pady=pad)
        self._dot = tk.Label(body, bg=theme.panel, bd=0)
        self._dot.pack(side="left", padx=(ui.px(12), ui.px(7)))
        self._timer = tk.Label(body, bg=theme.panel, font=ui.font(15, "semibold"), width=5, anchor="w")
        self._timer.pack(side="left", padx=(0, ui.px(4)))
        self._separator(body)
        self._pause = self._button(body, PAUSE, "", self._actions.on_pause)
        self._pause.pack(side="left", padx=(pad, 0))
        self._pause_tip = Tooltip(self._pause, "", exclude=True)
        self._button(body, RESTART, "Почати запис спочатку", self._actions.on_restart).pack(
            side="left", padx=(ui.px(2), 0))
        self._button(body, DELETE, "Видалити запис", self._actions.on_delete).pack(side="left", padx=(ui.px(2), pad))
        self._separator(body)
        self._make_grip(body).pack(side="left", padx=(ui.px(2), pad), pady=pad)

        old, self._body = self._body, body
        if old:
            old.destroy()
        body.pack()
        self._show_state()
        self.window.update_idletasks()
        winapi.round_corners(self.window.winfo_id(), border=theme.border)

    def _button(
        self, parent: tk.Widget, glyph: str, tip: str, action: Callable[[], None], fill: str | None = None,
        hover: str | None = None, ink: str | None = None, size: float = 14,
    ) -> Tile:
        theme = self._ui.theme
        tile = Tile(parent, self._ui, bg=theme.panel, fill=fill or theme.panel, hover=hover or theme.tile_hover,
                    command=action, width=36, height=36, radius=8)
        tile.glyph = tile.text(glyph, self._ui.font(size, "icons"), ink or theme.text)
        if tip:
            Tooltip(tile, tip, exclude=True)
        return tile

    def _separator(self, parent: tk.Widget) -> None:
        tk.Frame(parent, bg=self._ui.theme.border, width=self._ui.px(1)).pack(
            side="left", fill="y", pady=self._ui.px(13), padx=(self._ui.px(6), 0))

    def _make_grip(self, parent: tk.Widget) -> Tile:
        """Six dots to take hold of and drag the bar by."""
        ui, theme = self._ui, self._ui.theme

        def dots(paint: Paint, width: int, height: int) -> None:
            color = theme.text if grip._hovered else theme.faint
            for column in (-0.5, 0.5):
                for row in (-1, 0, 1):
                    paint.circle(width / 2 + column * 5 * ui.scale, height / 2 + row * 5 * ui.scale, 1.5 * ui.scale,
                                 fill=color)

        grip = Tile(parent, ui, bg=theme.panel, fill=theme.panel, hover=theme.tile_hover, width=22, height=36, radius=6)
        grip.decorate = dots
        grip.repaint()
        grip.configure(cursor="fleur")
        grip.bind("<ButtonPress-1>", self._start_drag)
        grip.bind("<B1-Motion>", self._drag_to)
        grip.bind("<ButtonRelease-1>", self._end_drag, add="+")
        # <ButtonPress-1> above outranks the tip's own <ButtonPress>, so the drag hides it.
        self._grip_tip = Tooltip(grip, "Перетягніть, щоб перемістити панель", exclude=True)
        return grip

    def _start_drag(self, event: tk.Event) -> None:
        self._grip_tip.hide()
        offset_x = event.x_root - self.window.winfo_rootx()
        offset_y = event.y_root - self.window.winfo_rooty()
        self._drag = (offset_x, offset_y, winapi.list_monitors())

    def _drag_to(self, event: tk.Event) -> None:
        if not self._drag:
            return
        offset_x, offset_y, monitors = self._drag
        # The whole bar stays on the monitor under the pointer.
        monitor = _monitor_at(monitors, event.x_root, event.y_root) or self.screen
        self._move(monitor, event.x_root - offset_x, event.y_root - offset_y)

    def _move(self, monitor: Monitor, x: int, y: int) -> None:
        width, height = self.window.winfo_width(), self.window.winfo_height()
        x = min(max(monitor.left, x), monitor.left + monitor.width - width)
        y = min(max(monitor.top, y), monitor.top + monitor.height - height)
        self.window.geometry(f"+{x}+{y}")
        self.screen = monitor

    def _end_drag(self, _event: tk.Event) -> None:
        if not self._drag:
            return
        self._drag = None
        if self.screen.scale != self._scale:  # dropped on a monitor with another scale: redraw at its size
            self._build(self.screen.scale)
            self._move(self.screen, self.window.winfo_rootx(), self.window.winfo_rooty())
        self._actions.on_moved(self.window.winfo_rootx(), self.window.winfo_rooty())

    def restyle(self) -> None:
        """The theme or the interface size changed."""
        self._build(self._scale)
        self._move(self.screen, self.window.winfo_rootx(), self.window.winfo_rooty())  # still all on screen

    def set_time(self, seconds: float) -> None:
        self._seconds = seconds
        self._timer.configure(text=format_timer(seconds))

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._show_state()

    def _show_state(self) -> None:
        theme = self._ui.theme
        self._pause.itemconfigure(self._pause.glyph, text=PLAY if self._paused else PAUSE)
        self._pause_tip.text = "Продовжити" if self._paused else "Пауза"
        self._timer.configure(text=format_timer(self._seconds), fg=theme.warning if self._paused else theme.text)
        self._paint_dot()

    def _paint_dot(self) -> None:
        """Red and blinking while recording, amber while paused."""
        theme, size = self._ui.theme, self._ui.px(8)
        if self._paused:
            color = theme.warning
        else:
            color = theme.danger if self._lit else mix(theme.danger, theme.panel, 0.65)
        paint = Paint(size, size, theme.panel)
        paint.circle(size / 2, size / 2, size / 2, fill=color)
        self._dot_photo = paint.photo()
        self._dot.configure(image=self._dot_photo)

    def _blink(self) -> None:
        self._lit = not self._lit
        self._paint_dot()
        self._blink_job = self.window.after(BLINK_MS, self._blink)

    def keep_on_top(self) -> None:
        winapi.bring_to_top(self.window.winfo_id())

    def close(self) -> None:
        self.window.after_cancel(self._blink_job)
        self.window.destroy()


class ConfirmDelete:
    """Asks before throwing the recording away. The recording is paused meanwhile, so this
    dialog does not end up in the video either."""

    def __init__(self, root: tk.Tk, bar: RecordingBar, on_confirm: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        self._on_confirm, self._on_cancel = on_confirm, on_cancel
        self._bar = bar
        self._body: tk.Frame | None = None
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self._build()

        # Next to the bar: above it when there is room, otherwise below.
        gap = round(8 * bar.scale)
        bar_window = bar.window
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = bar_window.winfo_rootx() + (bar_window.winfo_width() - width) // 2
        y = bar_window.winfo_rooty() - height - gap
        if y < bar.screen.top:
            y = bar_window.winfo_rooty() + bar_window.winfo_height() + gap
        self.window.geometry(f"+{x}+{y}")
        winapi.exclude_from_capture(self.window.winfo_id())
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def _build(self) -> None:
        ui = Ui(self._bar.scale)
        theme = ui.theme
        self.window.configure(bg=theme.panel)
        body = tk.Frame(self.window, bg=theme.panel)
        tk.Label(body, text="Видалити цей запис?", font=ui.font(15, "semibold"), fg=theme.text, bg=theme.panel).pack(
            anchor="w")
        tk.Label(body, text="Відео буде видалено, відновити його не вийде.", font=ui.font(13), fg=theme.muted,
                 bg=theme.panel).pack(anchor="w", pady=(ui.px(4), ui.px(16)))
        buttons = tk.Frame(body, bg=theme.panel)
        buttons.pack(anchor="e")
        button(buttons, ui, "Скасувати", self.cancel, bg=theme.panel).pack(side="left")
        button(buttons, ui, "Видалити", self.confirm, bg=theme.panel, kind="danger").pack(side="left",
                                                                                        padx=(ui.px(8), 0))
        old, self._body = self._body, body
        if old:
            old.destroy()
        body.pack(padx=ui.px(20), pady=ui.px(18))
        self.window.update_idletasks()
        winapi.round_corners(self.window.winfo_id(), border=theme.border)

    def restyle(self) -> None:
        self._build()

    def confirm(self) -> None:
        self.close()
        self._on_confirm()

    def cancel(self) -> None:
        self.close()
        self._on_cancel()

    def close(self) -> None:
        if self.window.winfo_exists():
            self.window.destroy()
