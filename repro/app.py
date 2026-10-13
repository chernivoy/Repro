"""Repro app. The capture overlay picks what to record; the tray icon and the hotkeys drive
it; the settings window (the ⚙ on the panel) holds the video, mouse and keyboard options, the
hotkeys, the output folder, the theme and the status."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

from PIL import ImageTk

from . import theme, winapi
from .capture import Rect
from .hotkeys import HotkeyListener, format_hotkey, same_hotkey
from .icons import make_icon
from .inputs import DEFAULT_LEFT_COLOR, DEFAULT_RIGHT_COLOR, KEYS_ALL, bgr
from .overlay import CaptureOverlay, OverlayActions
from .recorder import (
    DEFAULT_QUALITY, DEFAULT_RESOLUTION, QUALITIES, RESOLUTIONS, Recorder, RecordingOptions, snap_sharpness,
    snap_speed, speed_text, video_divisor,
)
from .recording_bar import BarActions, ConfirmDelete, RecordingBar, format_timer
from .region import MIN_DRAG, RecordingFrame, recording_layout
from .settings import APP_DIR, Settings
from .settings_window import KEY_MODES, SettingsWindow
from .tray import Tray
from .winapi import Monitor

log = logging.getLogger(__name__)

HIDE_DELAY_MS = 350  # lets the overlay and windows disappear before the first frame is taken
WINDOW_ICON_SIZES = (256, 64, 48, 32, 24, 16)
INSTANCE_MUTEX = "Local\\Repro"
SHOW_EVENT = "Local\\Repro.Show"  # a second launch fires it to bring up the capture overlay
TOGGLES = {"cursor": "draw_cursor", "clicks": "highlight_clicks", "keys": "show_keys"}  # -> Settings field


def monitor_key(monitors: list[Monitor], monitor: Monitor) -> str:
    if monitor.primary:
        return "primary"
    return f"secondary:{[m for m in monitors if not m.primary].index(monitor) + 1}"


def find_monitor(monitors: list[Monitor], key: str) -> Monitor | None:
    if key == "primary":
        return next((m for m in monitors if m.primary), monitors[0] if monitors else None)
    secondary = [m for m in monitors if not m.primary]
    _, _, number = key.partition(":")
    if number.isdigit() and 1 <= int(number) <= len(secondary):
        return secondary[int(number) - 1]
    return None


def valid_area(monitor: Monitor, area: list[int] | None) -> Rect | None:
    """A saved area, if it still fits the monitor (RDP may reconnect at another resolution)."""
    if not area or len(area) != 4:
        return None
    x, y, width, height = area
    if x < 0 or y < 0 or width < MIN_DRAG or height < MIN_DRAG:
        return None
    if x + width > monitor.width or y + height > monitor.height:
        return None
    return x, y, width, height


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def format_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} МБ" if size >= 1024 * 1024 else f"{size / 1024:.0f} КБ"


class App:
    def __init__(self) -> None:
        self.settings = Settings.load()
        theme.use(self.settings.theme)
        self.monitors = winapi.list_monitors()
        self.recorder: Recorder | None = None
        self.overlay: CaptureOverlay | None = None
        self.recording_frame: RecordingFrame | None = None
        self.recording_bar: RecordingBar | None = None
        self.confirm: ConfirmDelete | None = None
        self.last_file: Path | None = None
        self._recording_target: tuple[Monitor, Rect] | None = None
        self._after_stop: str | None = None  # "restart" or "delete" once the file is closed
        self._bar_position: tuple[int, int] | None = None  # where the bar was dragged this take
        self._keep_bar_position = False  # a restart keeps the bar where it was dragged
        self.events: queue.Queue = queue.Queue()
        self._starting = False
        self._stopping = False
        self._quitting = False
        self._told_about_tray = False

        # The root window is the settings window, hidden until the ⚙ or the tray asks for it.
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Repro — налаштування")
        self.root.resizable(False, False)
        self.root.report_callback_exception = self._report_exception
        self.root.protocol("WM_DELETE_WINDOW", self.hide_settings)
        self.root.bind("<Unmap>", self._on_unmap)
        # Every size, so Windows need not scale one for the title bar, taskbar and Alt+Tab.
        self._window_icons = [ImageTk.PhotoImage(make_icon(False, size)) for size in WINDOW_ICON_SIZES]
        self.root.iconphoto(True, *self._window_icons)
        self.window = SettingsWindow(self)

        self.tray = Tray(
            lambda: self.recorder is not None,
            on_record=lambda: self.events.put("record"),
            on_settings=lambda: self.events.put("settings"),
            on_open_folder=lambda: self.events.put("folder"),
            on_quit=lambda: self.events.put("quit"),
        )
        self.hotkeys: HotkeyListener | None = None
        failed = self._start_hotkeys()
        self._show_hotkey_hint()
        if failed:
            message = (
                f"Не вдалося зайняти {', '.join(map(format_hotkey, failed))}: її вже використовує інша програма. "
                "Виберіть іншу в налаштуваннях."
            )
            self._set_status(message, "error")
            self.tray.notify(message)
        show_event = winapi.create_event(SHOW_EVENT)
        threading.Thread(target=self._watch_relaunch, args=(show_event,), name="relaunch", daemon=True).start()
        self.root.after(100, self._poll)
        self.root.after(0, self.open_overlay)  # like CleanShot: start ready to pick what to record

    def run(self) -> None:
        self.root.mainloop()

    # --- Settings ------------------------------------------------------------------

    def quality_key(self) -> str:
        return self.settings.quality if self.settings.quality in QUALITIES else DEFAULT_QUALITY

    def resolution_key(self) -> str:
        return self.settings.resolution if self.settings.resolution in RESOLUTIONS else DEFAULT_RESOLUTION

    def sharpness(self) -> float:
        return snap_sharpness(self.settings.sharpness)

    def speed(self) -> float:
        return snap_speed(self.settings.speed)

    def zoom(self) -> float:
        return RESOLUTIONS[self.resolution_key()][1]

    def keys_mode(self) -> str:
        return self.settings.keys_mode if self.settings.keys_mode in KEY_MODES else KEYS_ALL

    def option(self, name: str) -> bool:
        return getattr(self.settings, TOGGLES[name])

    def click_color(self, name: str) -> str:
        """The saved colour, or the default when settings.json holds something unusable."""
        default = DEFAULT_LEFT_COLOR if name == "click_color_left" else DEFAULT_RIGHT_COLOR
        blue, green, red = bgr(getattr(self.settings, name), default)
        return f"#{red:02x}{green:02x}{blue:02x}"

    def set_quality(self, key: str) -> None:
        self.settings.quality = key
        self._video_options_changed()

    def set_resolution(self, key: str) -> None:
        self.settings.resolution = key
        if self.overlay:
            self.overlay.set_zoom(self.zoom())
        self._video_options_changed()

    def set_sharpness(self, amount: float) -> None:
        self.settings.sharpness = amount
        self._video_options_changed()

    def set_fps(self, fps: int) -> None:
        self.settings.fps = fps
        self._video_options_changed()

    def set_speed(self, speed: float) -> None:
        """The video speed, from the settings window or the capture panel."""
        self.settings.speed = speed
        if self.overlay:
            self.overlay.panel.set_speed(speed)
        self._video_options_changed()

    def set_option(self, name: str, enabled: bool) -> None:
        """A mouse or keyboard toggle, from the settings window or the capture panel."""
        setattr(self.settings, TOGGLES[name], enabled)
        if self.overlay and name in ("cursor", "keys"):
            self.overlay.panel.set_toggle(name, enabled)
        self._video_options_changed()

    def set_keys_mode(self, mode: str) -> None:
        self.settings.keys_mode = mode
        self._video_options_changed()

    def choose_click_color(self, name: str, button: str) -> None:
        _rgb, color = colorchooser.askcolor(
            self.click_color(name), parent=self.root, title=f"Колір кліку: {button} кнопка"
        )
        if color:
            setattr(self.settings, name, color.lower())
            self._video_options_changed()

    def _video_options_changed(self) -> None:
        self.settings.save()
        self.window.refresh()
        if self.recorder:
            self._set_status("Нові налаштування відео діятимуть з наступного запису", "recording")

    def set_theme(self, name: str) -> None:
        """Light or dark, from the settings window or the capture panel; open windows follow."""
        if name == theme.current().name:
            return
        self.settings.theme = name
        self.settings.save()
        theme.use(name)
        self.window.build()
        if self.overlay:
            self.overlay.set_theme()
        if self.recording_bar:
            self.recording_bar.set_theme()
        if self.confirm:
            self.confirm.set_theme()

    def toggle_theme(self) -> None:
        self.set_theme("light" if theme.current().dark else "dark")

    # --- Hotkeys ---------------------------------------------------------------------

    def _hotkey_bindings(self) -> dict[str, str]:
        """Event name -> hotkey. One key for both makes a toggle, as RegisterHotKey takes a
        combination only once."""
        record, stop = self.settings.hotkey_record, self.settings.hotkey_stop
        if same_hotkey(record, stop):
            return {"record": record}
        return {"hotkey-start": record, "hotkey-stop": stop}

    def _start_hotkeys(self) -> list[str]:
        """Registers the hotkeys; returns those another program already holds."""
        self.hotkeys = HotkeyListener(self._hotkey_bindings(), self.events.put)
        return self.hotkeys.start()

    def _stop_hotkeys(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
            self.hotkeys = None

    def listen_for_hotkey(self, listening: bool) -> None:
        """While a field waits for a combination, the current hotkeys must reach it instead of
        opening the panel, so they are off until it is done."""
        if listening:
            self._stop_hotkeys()
            self.window.set_hotkey_hint("Натисніть нову комбінацію, наприклад Ctrl+Alt+R. Esc — залишити як було.")
        elif self.hotkeys is None:
            self._start_hotkeys()
            self._show_hotkey_hint()

    def change_hotkey(self, name: str, hotkey: str) -> str | None:
        previous = getattr(self.settings, name)
        setattr(self.settings, name, hotkey)
        self._stop_hotkeys()
        failed = self._start_hotkeys()
        if failed:  # taken by another program: back to what worked
            setattr(self.settings, name, previous)
            self._stop_hotkeys()
            self._start_hotkeys()
            return f"{format_hotkey(hotkey)} вже використовує інша програма. Залишено {format_hotkey(previous)}."
        self.settings.save()
        self._show_hotkey_hint()
        return None

    def _show_hotkey_hint(self) -> None:
        if same_hotkey(self.settings.hotkey_record, self.settings.hotkey_stop):
            text = ("Одна комбінація для всього: перше натискання відкриває панель, друге починає запис, "
                    "під час запису — зупиняє. Щоб змінити, клацніть поле й натисніть нову комбінацію.")
        else:
            text = ("Запис: перше натискання відкриває панель, друге починає запис. Зупинка: зупиняє запис "
                    "або закриває панель. Щоб змінити, клацніть поле й натисніть нову комбінацію.")
        self.window.set_hotkey_hint(text)

    def show_settings(self) -> None:
        self.root.deiconify()
        self.root.lift()
        # Above the overlay layers while they cover the screen.
        self.root.attributes("-topmost", self.overlay is not None)
        self.root.focus_force()
        self.window.style_title_bar()

    def hide_settings(self) -> None:
        self.root.withdraw()
        if self.overlay:
            self.overlay.panel.window.focus_force()

    def _on_unmap(self, event: tk.Event) -> None:
        # Minimizing hides to the tray, like closing: the tray icon is where the app lives.
        if event.widget is self.root and self.root.state() == "iconic":
            self.hide_settings()

    # --- Capture overlay -----------------------------------------------------------

    def open_overlay(self) -> None:
        if self.recorder or self.overlay or self._starting or self._stopping:
            return
        self.monitors = winapi.list_monitors()
        monitor = find_monitor(self.monitors, self.settings.source) or find_monitor(self.monitors, "primary")
        rect = valid_area(monitor, self.settings.region) or (0, 0, monitor.width, monitor.height)
        self.overlay = CaptureOverlay(
            self.root, self.monitors, monitor, rect,
            {"cursor": self.settings.draw_cursor, "keys": self.settings.show_keys}, self.speed(), self.zoom(),
            OverlayActions(
                on_change=self._selection_changed,
                on_record=self._record_selection,
                on_close=self.close_overlay,
                on_settings=self.show_settings,
                on_toggle=self.set_option,
                on_speed=self.set_speed,
                on_theme=self.toggle_theme,
                remembered_area=self._remembered_area,
            ),
        )

    def close_overlay(self) -> None:
        if self.overlay:
            self.overlay.close()
            self.overlay = None
        self.root.attributes("-topmost", False)

    def _selection_changed(self, monitor: Monitor, rect: Rect) -> None:
        key = monitor_key(self.monitors, monitor)
        self.settings.source = key
        if rect == (0, 0, monitor.width, monitor.height):
            self.settings.region = None
        else:
            self.settings.region = list(rect)
            self.settings.areas[key] = list(rect)
        self.settings.save()

    def _remembered_area(self, monitor: Monitor) -> Rect | None:
        return valid_area(monitor, self.settings.areas.get(monitor_key(self.monitors, monitor)))

    def _record_selection(self, monitor: Monitor, rect: Rect) -> None:
        self.close_overlay()
        self.root.withdraw()  # the settings window would end up in the video
        self._starting = True
        self.root.after(HIDE_DELAY_MS, lambda: self._begin(monitor, rect))

    # --- Recording -------------------------------------------------------------------

    def on_hotkey(self) -> None:
        """The tray icon, or one hotkey for both: open the overlay, then record; while recording, stop."""
        if self.recorder:
            self.stop_recording()
        else:
            self.on_start_hotkey()

    def on_start_hotkey(self) -> None:
        """The recording hotkey, when stopping has its own: open the overlay, then record."""
        if self.recorder:
            return
        if self.overlay:
            self.overlay.record()
        else:
            self.open_overlay()

    def on_stop_hotkey(self) -> None:
        if self.recorder:
            self.stop_recording()
        elif self.overlay:
            self.close_overlay()

    def _begin(self, monitor: Monitor, rect: Rect) -> None:
        self._starting = False
        folder = Path(self.settings.output_dir)
        path = folder / f"Repro_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
        quality = QUALITIES[self.quality_key()]
        options = RecordingOptions(
            fps=self.settings.fps, speed=self.speed(), crf=quality.crf, preset=quality.preset,
            zoom=self.zoom(), sharpness=self.sharpness(), draw_cursor=self.settings.draw_cursor,
            highlight_clicks=self.settings.highlight_clicks,
            click_colors=(self.click_color("click_color_left"), self.click_color("click_color_right")),
            keystrokes=self.keys_mode() if self.settings.show_keys else "",
            hidden_hotkeys=(self.settings.hotkey_record, self.settings.hotkey_stop),
        )
        try:
            folder.mkdir(parents=True, exist_ok=True)
            capture_rect, frame_width = recording_layout(monitor, rect, video_divisor(monitor, options.zoom))
            recorder = Recorder(
                monitor, capture_rect, path, options, lambda r, e: self.events.put(("finished", r, e))
            )
            recorder.start()
        except Exception as error:
            log.exception("Could not start recording")
            message = f"Не вдалося почати запис: {error}"
            self._set_status(message, "error")
            self.tray.notify(message)
            return

        self.recorder = recorder
        self._recording_target = (monitor, rect)
        self.recording_frame = RecordingFrame(self.root, monitor, capture_rect, frame_width)
        if not self._keep_bar_position:
            self._bar_position = None
        self._keep_bar_position = False
        self.recording_bar = RecordingBar(
            self.root, monitor, capture_rect, frame_width,
            BarActions(
                on_stop=self.stop_recording, on_pause=self.toggle_pause, on_restart=self.restart_recording,
                on_delete=self.ask_delete, on_moved=self._bar_moved,
            ),
            position=self._bar_position,
        )
        self._set_status(
            f"Йде запис {recorder.size[0]}×{recorder.size[1]}, {options.fps} кадр/с, "
            f"якість «{quality.name.lower()}»"
            + (f", швидкість {speed_text(options.speed)}" if options.speed != 1 else "")
            + f" ({recorder.source_name}) → {path.name}",
            "recording",
        )
        self.tray.set_recording(True)
        self._tick()

    def _bar_moved(self, x: int, y: int) -> None:
        self._bar_position = (x, y)

    def stop_recording(self) -> None:
        if not self.recorder or self._stopping:
            return
        self._stopping = True
        if self.confirm:
            self.confirm.close()
            self.confirm = None
        self.recorder.stop()

    def toggle_pause(self) -> None:
        if not self.recorder or self._stopping:
            return
        if self.recorder.paused:
            self.recorder.resume()
        else:
            self.recorder.pause()
        self._show_paused(self.recorder.paused)

    def _show_paused(self, paused: bool) -> None:
        self.recording_bar.set_paused(paused)
        self.recording_frame.set_paused(paused)
        self._tick_once()

    def restart_recording(self) -> None:
        """Throws the recording away and starts a new one of the same area."""
        if self.recorder and not self._stopping:
            self._after_stop = "restart"
            self.stop_recording()

    def ask_delete(self) -> None:
        if not self.recorder or self._stopping or self.confirm:
            return
        was_paused = self.recorder.paused
        self.recorder.pause()
        self._show_paused(True)

        def confirmed() -> None:
            self.confirm = None
            self._after_stop = "delete"
            self.stop_recording()

        def cancelled() -> None:
            self.confirm = None
            if not was_paused and self.recorder:
                # Resume once the dialog has left the screen, so it stays out of the video.
                self.root.after(150, self._resume_after_dialog)

        self.confirm = ConfirmDelete(self.root, self.recording_bar, confirmed, cancelled)

    def _resume_after_dialog(self) -> None:
        if self.recorder and not self._stopping and self.recorder.paused:
            self.recorder.resume()
            self._show_paused(False)

    def _on_finished(self, recorder: Recorder, error: BaseException | None) -> None:
        self.recorder = None
        self._stopping = False
        if self.recording_frame:
            self.recording_frame.close()
            self.recording_frame = None
        if self.recording_bar:
            self.recording_bar.close()
            self.recording_bar = None
        self.tray.set_recording(False)
        self.tray.set_tooltip("Repro")

        after_stop, self._after_stop = self._after_stop, None
        if after_stop and not self._quitting:
            recorder.path.unlink(missing_ok=True)
            if after_stop == "restart":
                self._keep_bar_position = True
                self._starting = True  # keeps the hotkey from opening the overlay in between
                self.root.after(100, lambda: self._begin(*self._recording_target))
            else:
                self._set_status("Запис видалено")
                self.tray.notify("Запис видалено")
            return
        if recorder.path.exists():
            self.last_file = recorder.path
            message = (
                f"Збережено {recorder.path.name} · {format_size(recorder.path.stat().st_size)} · "
                f"{format_duration(recorder.duration)}"
            )
            if error:
                message = f"Запис зупинено: {error}. {message}"
        else:
            message = f"Помилка запису: {error}"
        self._set_status(message, "error" if error else "idle")
        self.tray.notify(message)
        if self._quitting:
            self._shutdown()

    def _tick(self) -> None:
        if not self.recorder or self._stopping:
            return
        self._tick_once()
        if self.recording_frame:
            self.recording_frame.keep_on_top()
        if self.recording_bar:
            self.recording_bar.keep_on_top()
        self.root.after(500, self._tick)

    def _tick_once(self) -> None:
        elapsed = self.recorder.elapsed
        state = "пауза" if self.recorder.paused else "запис"
        self.tray.set_tooltip(f"Repro — {state} {format_timer(elapsed)}")
        if self.recording_bar:
            self.recording_bar.set_time(elapsed)

    # --- Folder, events, shutdown ------------------------------------------------------

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.settings.output_dir, mustexist=False)
        if folder:
            self.settings.output_dir = os.path.normpath(folder)
            self.settings.save()
            self.window.refresh()

    def open_folder(self) -> None:
        if self.last_file and self.last_file.exists():
            subprocess.Popen(f'explorer /select,"{self.last_file}"')
            return
        folder = Path(self.settings.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _set_status(self, text: str, tone: str = "idle") -> None:
        self.window.set_status(text, tone)

    def _watch_relaunch(self, event: int) -> None:
        while winapi.wait_event(event):
            self.events.put("show")

    def _poll(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "record":
                self.on_hotkey()
            elif event == "hotkey-start":
                self.on_start_hotkey()
            elif event == "hotkey-stop":
                self.on_stop_hotkey()
            elif event == "show":
                self.open_overlay()
            elif event == "settings":
                self.show_settings()
            elif event == "folder":
                self.open_folder()
            elif event == "quit":
                self.quit()
            elif event[0] == "finished":
                self._on_finished(*event[1:])
        self.root.after(100, self._poll)

    def quit(self) -> None:
        self.close_overlay()
        if self.recorder:
            self._quitting = True
            self.stop_recording()
            return
        self._shutdown()

    def _shutdown(self) -> None:
        self._stop_hotkeys()
        self.tray.stop()
        self.root.destroy()

    def _report_exception(self, exc_type, value, traceback) -> None:
        log.error("Unhandled error", exc_info=(exc_type, value, traceback))
        messagebox.showerror("Repro", f"Помилка: {value}", parent=self.root)


def main() -> None:
    winapi.enable_dpi_awareness()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(APP_DIR / "repro.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")],
    )
    if not winapi.claim_single_instance(INSTANCE_MUTEX):
        # Already running, perhaps hidden in the tray: bring up its overlay instead.
        close_splash()
        if not winapi.set_event(SHOW_EVENT):
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("Repro", "Repro вже запущено: шукайте іконку в треї біля годинника.")
            root.destroy()
        return
    app = App()
    close_splash()
    app.run()


def close_splash() -> None:
    """Closes the splash the one-file exe shows while unpacking; a no-op in other builds."""
    if "_PYI_SPLASH_IPC" not in os.environ:  # set by the bootloader only when a splash is up
        return
    import pyi_splash

    pyi_splash.close()
