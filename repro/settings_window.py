"""The settings window, laid out like Windows 11 Settings: navigation on the left, pages of setting
cards on the right and a status line under them. It is built for the current theme and for the
scale of the monitor it is on, and rebuilt when either changes."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import ImageTk

from . import winapi
from .hotkey_field import HotkeyField, Keycaps
from .icons import make_icon
from .inputs import KEYS_ALL, KEYS_SHORTCUTS
from .recorder import DEFAULT_SHARPNESS, FRAME_RATES, QUALITIES, RESOLUTIONS, SHARPNESS, SPEEDS, speed_text
from .theme import Paint, Ui, mix
from .widgets import Rounded, Segmented, StepSlider, Switch, Tile, button

if TYPE_CHECKING:
    from .app import App

KEY_MODES = {
    KEYS_ALL: ("Усі клавіші", "Видно все, що ви набираєте, зокрема паролі."),
    KEYS_SHORTCUTS: ("Лише комбінації", "Ctrl, Alt, Win + клавіша, F1–F12, Enter, Backspace; без тексту."),
}
SWITCHES = (  # name in App.set_option, title, hint
    ("cursor", "Курсор миші", "Вказівник миші у відео."),
    ("clicks", "Підсвічувати кліки", "Кольорове коло там, де клацнули."),
    ("keys", "Натискання клавіш", "Клавіші, які ви натискаєте, внизу відео."),
)
PAGES = (  # key, title, Segoe Fluent Icons glyph
    ("video", "Відео", ""),
    ("input", "Миша і клавіатура", ""),
    ("hotkeys", "Гарячі клавіші", ""),
    ("general", "Загальні", ""),
)
SUN, MOON, POWER = "", "", ""
THEME_NAMES = ("light", "dark")

# Logical pixels.
NAV_WIDTH = 212
CARD_WIDTH = 476
CARD_PAD = 18
INNER = CARD_WIDTH - 2 * CARD_PAD - 2  # inside a card's padding and border


def speed_hint(speed: float) -> str:
    if speed == 1.0:
        return "Як на екрані: хвилина запису — хвилина відео."
    seconds = round(60 / speed)
    length = f"{seconds // 60} хв {seconds % 60} с" if seconds >= 60 else f"{seconds} с"
    return f"{'Швидше' if speed > 1 else 'Повільніше'}: хвилина запису стане відео на {length}."


class SettingsWindow:
    def __init__(self, app: App) -> None:
        self.app = app
        self.root = app.root
        self.page = "video"
        self._status = ("Готово до запису", "idle")
        self._hotkey_hint = ""
        self._frame: tk.Frame | None = None
        self._scale = 0.0
        self._scale_job: str | None = None
        self.hotkey_fields: dict[str, HotkeyField] = {}
        self.root.bind("<Configure>", self._on_configure, add="+")
        self.build()

    # --- Public ------------------------------------------------------------------

    def build(self) -> None:
        """(Re)builds the window for the current theme and the scale of its monitor."""
        for field in self.hotkey_fields.values():
            field.cancel()
        self._scale = winapi.window_scale(self.root.winfo_id())
        ui = self.ui = Ui(self._scale)
        self.root.configure(bg=ui.theme.bg)
        frame = tk.Frame(self.root, bg=ui.theme.bg)
        self._build_sidebar(frame)
        self._build_pages(frame)
        old, self._frame = self._frame, frame
        if old:
            old.destroy()
        frame.pack(fill="both", expand=True)
        self.refresh()
        self.show_page(self.page)
        self.style_title_bar()

    def style_title_bar(self) -> None:
        theme = self.ui.theme
        winapi.style_title_bar(self.root.winfo_id(), theme.dark, caption=theme.bg, text=theme.text, border=theme.border)

    def refresh(self) -> None:
        """Shows the current settings."""
        app, settings = self.app, self.app.settings
        quality = app.quality_key()
        self.quality.set(list(QUALITIES).index(quality))
        self.quality_hint.configure(text=QUALITIES[quality].hint)
        resolution = app.resolution_key()
        self.resolution.set(list(RESOLUTIONS).index(resolution))
        self.resolution_hint.configure(text=RESOLUTIONS[resolution][2])
        sharpness = app.sharpness()
        self.sharpness.set(sharpness)
        self.sharpness_value.configure(text=SHARPNESS[sharpness])
        self.fps.set(min(range(len(FRAME_RATES)), key=lambda i: abs(FRAME_RATES[i] - settings.fps)))
        speed = app.speed()
        self.speed.set(speed)
        self.speed_value.configure(text=speed_text(speed))
        self.speed_hint.configure(text=speed_hint(speed))

        for name, switch in self.switches.items():
            switch.set(app.option(name))
        for name, chip in self.color_chips.items():
            chip.color = app.click_color(name)
            chip.repaint()
        mode = app.keys_mode()
        self.keys_mode.set(list(KEY_MODES).index(mode))
        self.keys_mode.set_enabled(settings.show_keys)
        self.keys_hint.configure(text=KEY_MODES[mode][1], fg=self.ui.theme.muted if settings.show_keys else self.ui.theme.faint)

        for name, field in self.hotkey_fields.items():
            field.set(getattr(settings, name))
        self.folder.configure(text=settings.output_dir)

    def set_status(self, text: str, tone: str = "idle") -> None:
        """`tone`: idle, recording or error; colours the dot before the text."""
        self._status = (text, tone)
        self._show_status()

    def set_hotkey_hint(self, text: str) -> None:
        self._hotkey_hint = text
        self.hotkey_hint.configure(text=text)

    def show_page(self, key: str) -> None:
        self.page = key
        self.pages[key].tkraise()
        theme = self.ui.theme
        for name, item in self.nav.items():
            chosen = name == key
            item.decorate = self._indicator if chosen else None
            item.set_fill(theme.surface if chosen else theme.bg, theme.surface if chosen else theme.nav_hover)

    # --- Sidebar -------------------------------------------------------------------

    def _build_sidebar(self, parent: tk.Widget) -> None:
        ui, theme = self.ui, self.ui.theme
        side = tk.Frame(parent, bg=theme.bg)
        side.pack(side="left", fill="y", padx=(ui.px(12), 0), pady=ui.px(16))

        header = tk.Frame(side, bg=theme.bg)
        header.pack(fill="x", padx=ui.px(8), pady=(ui.px(4), ui.px(20)))
        self._logo = ImageTk.PhotoImage(make_icon(False, ui.px(36)))
        tk.Label(header, image=self._logo, bg=theme.bg, bd=0).pack(side="left")
        names = tk.Frame(header, bg=theme.bg)
        names.pack(side="left", padx=(ui.px(10), 0))
        tk.Label(names, text="Repro", font=ui.font(15, "semibold"), fg=theme.text, bg=theme.bg).pack(anchor="w")
        tk.Label(names, text="Налаштування", font=ui.font(12), fg=theme.muted, bg=theme.bg).pack(anchor="w")

        self.nav: dict[str, Tile] = {}
        for key, title, glyph in PAGES:
            item = Tile(side, ui, bg=theme.bg, fill=theme.bg, hover=theme.nav_hover, width=NAV_WIDTH, height=38,
                        radius=6, command=lambda key=key: self.show_page(key))
            item.text(glyph, ui.font(16, "icons"), theme.text, relx=0, dx=16, anchor="w")
            item.text(title, ui.font(14), theme.text, relx=0, dx=46, anchor="w")
            item.pack(pady=(0, ui.px(3)))
            self.nav[key] = item

        button(side, ui, "Вийти з Repro", self.app.quit, bg=theme.bg, kind="subtle", icon=POWER,
               width=NAV_WIDTH, height=36, size=13, align="left").pack(side="bottom")
        self.theme_choice = Segmented(
            side, ui, ["Світла", "Темна"], THEME_NAMES.index(theme.name), self._pick_theme, bg=theme.bg,
            width=NAV_WIDTH, icons=[SUN, MOON],
        )
        self.theme_choice.pack(side="bottom", pady=(ui.px(6), ui.px(14)))
        tk.Label(side, text="Тема", font=ui.font(12), fg=theme.muted, bg=theme.bg).pack(side="bottom", anchor="w",
                                                                                      padx=ui.px(6))

    def _indicator(self, paint: Paint, _width: int, height: int) -> None:
        """The accent mark of the selected page."""
        ui = self.ui
        paint.rounded((ui.px(5), height / 2 - ui.px(8), ui.px(5) + ui.px(3), height / 2 + ui.px(8)), ui.px(1.5),
                      fill=ui.theme.accent)

    def _pick_theme(self, index: int) -> None:
        self.app.set_theme(THEME_NAMES[index])

    # --- Pages ---------------------------------------------------------------------

    def _build_pages(self, parent: tk.Widget) -> None:
        ui, theme = self.ui, self.ui.theme
        content = tk.Frame(parent, bg=theme.bg)
        content.pack(side="left", fill="both", expand=True, padx=(ui.px(20), ui.px(24)), pady=(ui.px(18), ui.px(14)))
        stack = tk.Frame(content, bg=theme.bg)
        stack.pack(fill="both", expand=True)
        stack.columnconfigure(0, minsize=ui.px(CARD_WIDTH))  # every page as wide as the cards
        self.pages: dict[str, tk.Frame] = {}
        for key, title, _glyph in PAGES:
            page = tk.Frame(stack, bg=theme.bg)
            page.grid(row=0, column=0, sticky="nsew")  # all in one cell: the window fits the tallest
            tk.Label(page, text=title, font=ui.font(26, "display"), fg=theme.text, bg=theme.bg).pack(
                anchor="w", pady=(0, ui.px(14)))
            getattr(self, f"_page_{key}")(page)
            self.pages[key] = page

        status = tk.Frame(content, bg=theme.bg)
        status.pack(fill="x", pady=(ui.px(10), 0))
        self._status_dot = tk.Label(status, bg=theme.bg, bd=0)
        self._status_dot.pack(side="left", padx=(ui.px(4), ui.px(8)))
        self._status_label = tk.Label(status, font=ui.font(12), fg=theme.muted, bg=theme.bg, justify="left",
                                      anchor="w", wraplength=ui.px(CARD_WIDTH - 24), height=2)
        self._status_label.pack(side="left", fill="x")
        self._show_status()

    def _page_video(self, page: tk.Frame) -> None:
        ui, theme = self.ui, self.ui.theme
        card = self._card(page)
        self.quality_hint, row = self._row(card, "Якість", first=True)
        self.quality = Segmented(row, ui, [quality.name for quality in QUALITIES.values()], 0,
                                 lambda i: self.app.set_quality(list(QUALITIES)[i]), bg=theme.surface, width=INNER)
        self._below(row, self.quality)

        self.resolution_hint, row = self._row(card, "Роздільність", hint_lines=2)
        self.resolution = Segmented(row, ui, [label for label, _zoom, _hint in RESOLUTIONS.values()], 0,
                                    lambda i: self.app.set_resolution(list(RESOLUTIONS)[i]), bg=theme.surface,
                                    width=INNER)
        self._below(row, self.resolution)

        _hint, row = self._row(card, "Різкість тексту", hint_lines=0)
        self.sharpness, self.sharpness_value = self._slider(
            row, list(SHARPNESS), DEFAULT_SHARPNESS, self.app.set_sharpness)

        _hint, row = self._row(card, "Кадрів за секунду", hint_lines=0)
        self.fps = Segmented(row, ui, [str(rate) for rate in FRAME_RATES], 0,
                             lambda i: self.app.set_fps(FRAME_RATES[i]), bg=theme.surface)
        self._right(row, self.fps)

        self.speed_hint, row = self._row(card, "Швидкість відео")
        self.speed, self.speed_value = self._slider(row, SPEEDS, 1.0, self.app.set_speed)

    def _page_input(self, page: tk.Frame) -> None:
        ui, theme = self.ui, self.ui.theme
        card = self._card(page)
        self.switches: dict[str, Switch] = {}
        self.color_chips: dict[str, Tile] = {}
        for index, (name, title, hint) in enumerate(SWITCHES):
            hint_label, row = self._row(card, title, first=index == 0)
            hint_label.configure(text=hint)
            switch = Switch(row, ui, False, lambda enabled, name=name: self.app.set_option(name, enabled),
                            bg=theme.surface)
            self._right(row, switch)
            self.switches[name] = switch
            if name == "clicks":
                chips = tk.Frame(row, bg=theme.surface)
                self._below(row, chips)
                for side, (setting, text, button_name) in enumerate((
                    ("click_color_left", "Лівий", "ліва"), ("click_color_right", "Правий", "права"),
                )):
                    chip = self._color_chip(chips, text, lambda setting=setting, button_name=button_name:
                                            self.app.choose_click_color(setting, button_name))
                    chip.pack(side="left", padx=(ui.px(8) if side else 0, 0))
                    self.color_chips[setting] = chip
            elif name == "keys":
                self.keys_mode = Segmented(row, ui, [label for label, _hint in KEY_MODES.values()], 0,
                                           lambda i: self.app.set_keys_mode(list(KEY_MODES)[i]), bg=theme.surface,
                                           width=INNER)
                self._below(row, self.keys_mode)
                self.keys_hint = tk.Label(row, font=ui.font(12), fg=theme.muted, bg=theme.surface, justify="left",
                                          anchor="w", wraplength=ui.px(INNER))
                self.keys_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(ui.px(6), 0))

    def _page_hotkeys(self, page: tk.Frame) -> None:
        ui, theme = self.ui, self.ui.theme
        card = self._card(page)
        for index, (name, title, hint) in enumerate((
            ("hotkey_record", "Запис", "Відкриває панель запису, повторне натискання починає запис."),
            ("hotkey_stop", "Зупинка", "Зупиняє запис або закриває панель."),
        )):
            hint_label, row = self._row(card, title, first=index == 0)
            hint_label.configure(text=hint)
            field = HotkeyField(
                row, ui, getattr(self.app.settings, name), on_listen=self.app.listen_for_hotkey,
                on_change=lambda hotkey, name=name: self.app.change_hotkey(name, hotkey),
                on_message=self.set_hotkey_hint, bg=theme.surface,
            )
            self._right(row, field)
            self.hotkey_fields[name] = field
        self.hotkey_hint = tk.Label(page, text=self._hotkey_hint, font=ui.font(12), fg=theme.muted, bg=theme.bg,
                                    justify="left", anchor="nw", wraplength=ui.px(CARD_WIDTH - 8), height=3)
        self.hotkey_hint.pack(fill="x", padx=ui.px(4), pady=(0, ui.px(10)))

        card = self._card(page)
        for index, (title, keys) in enumerate((
            ("Почати запис", ["Enter"]), ("Вибрати вікно", ["Пробіл"]), ("Закрити панель", ["Esc"]),
        )):
            _hint, row = self._row(card, title, hint_lines=0, first=index == 0, compact=True)
            self._right(row, Keycaps(row, ui, keys, bg=theme.surface))
        tk.Label(page, text="Ці клавіші діють, поки відкрита панель запису.", font=ui.font(12), fg=theme.muted,
                 bg=theme.bg).pack(anchor="w", padx=ui.px(4))

    def _page_general(self, page: tk.Frame) -> None:
        ui, theme = self.ui, self.ui.theme
        card = self._card(page)
        self.folder, row = self._row(card, "Папка для відео", first=True)
        buttons = tk.Frame(row, bg=theme.surface)
        self._below(row, buttons)
        button(buttons, ui, "Змінити…", self.app.choose_folder, bg=theme.surface, kind="primary").pack(side="left")
        button(buttons, ui, "Відкрити папку", self.app.open_folder, bg=theme.surface, icon="").pack(
            side="left", padx=(ui.px(8), 0))

        card = self._card(page)
        hint, _row = self._row(card, "Про Repro", first=True)
        hint.configure(text="Записує екран у відео MP4 (H.264). Програма живе в треї біля годинника: "
                            "клацніть іконку, щоб почати запис.")

    # --- Building blocks ---------------------------------------------------------------

    def _card(self, page: tk.Frame) -> tk.Frame:
        ui, theme = self.ui, self.ui.theme
        card = Rounded(page, ui, theme.surface, theme.bg, radius=8, border=theme.border, padx=CARD_PAD)
        card.pack(fill="x", pady=(0, ui.px(10)))
        return card.body

    def _row(
        self, card: tk.Frame, title: str, hint_lines: int = 1, first: bool = False, compact: bool = False,
    ) -> tuple[tk.Label | None, tk.Frame]:
        """A setting: its title, a hint under it across the card, the control to the right of the
        title (_right) or under the hint (_below). Returns the hint label and the row."""
        ui, theme = self.ui, self.ui.theme
        if not first:
            tk.Frame(card, bg=theme.divider, height=ui.px(1)).pack(fill="x")
        row = tk.Frame(card, bg=theme.surface)
        row.pack(fill="x", pady=ui.px(10 if compact else 14))
        row.columnconfigure(0, weight=1)
        tk.Label(row, text=title, font=ui.font(14), fg=theme.text, bg=theme.surface).grid(row=0, column=0, sticky="w")
        hint = None
        if hint_lines:
            hint = tk.Label(row, font=ui.font(12), fg=theme.muted, bg=theme.surface, justify="left", anchor="nw",
                            wraplength=ui.px(INNER))
            if hint_lines > 1:
                hint.configure(height=hint_lines)
            hint.grid(row=1, column=0, columnspan=2, sticky="we", pady=(ui.px(2), 0))
        return hint, row

    def _right(self, row: tk.Frame, control: tk.Widget) -> None:
        control.grid(row=0, column=1, sticky="e", padx=(self.ui.px(16), 0))

    def _below(self, row: tk.Frame, control: tk.Widget) -> None:
        control.grid(row=2, column=0, columnspan=2, sticky="w", pady=(self.ui.px(10), 0))

    def _slider(
        self, row: tk.Frame, values: list[float], default: float, on_change: Callable[[float], None],
    ) -> tuple[StepSlider, tk.Label]:
        ui, theme = self.ui, self.ui.theme
        box = tk.Frame(row, bg=theme.surface)
        slider = StepSlider(box, ui, values, default, default, on_change, bg=theme.surface, width=176)
        slider.pack(side="left")
        value = tk.Label(box, font=ui.font(13, "semibold"), fg=theme.text, bg=theme.surface, width=7, anchor="e")
        value.pack(side="left")
        self._right(row, box)
        return slider, value

    def _color_chip(self, parent: tk.Widget, text: str, command: Callable[[], None]) -> Tile:
        """A button with a dot of the click colour; the colour is kept in its `color`."""
        ui, theme = self.ui, self.ui.theme
        text_font = ui.font(13)

        def dot(paint: Paint, _width: int, height: int) -> None:
            paint.circle(ui.px(19), height / 2, ui.px(7), fill=chip.color, outline=mix(chip.color, "#000000", 0.2),
                         line=ui.px(1))

        chip = Tile(parent, ui, bg=theme.surface, fill=theme.control, hover=theme.control_hover, command=command,
                    width=ui.measure(text, text_font) / ui.scale + 48, height=32, radius=6)
        chip.color = "#000000"
        chip.decorate = dot
        chip.text(text, text_font, theme.text, relx=0, dx=34, anchor="w")
        return chip

    def _show_status(self) -> None:
        ui, theme = self.ui, self.ui.theme
        text, tone = self._status
        color = {"recording": theme.danger, "error": theme.warning}.get(tone, theme.success)
        size = ui.px(8)
        paint = Paint(size, size, theme.bg)
        paint.circle(size / 2, size / 2, size / 2, fill=color)
        self._dot_photo = paint.photo()
        self._status_dot.configure(image=self._dot_photo)
        self._status_label.configure(text=text, fg=theme.text if tone != "idle" else theme.muted)

    # --- Moving between monitors -----------------------------------------------------------

    def _on_configure(self, event: tk.Event) -> None:
        # Every widget's events pass through the toplevel's bindings: only the window itself counts.
        if event.widget is not self.root:
            return
        if self._scale_job:
            self.root.after_cancel(self._scale_job)
        self._scale_job = self.root.after(250, self._check_scale)

    def _check_scale(self) -> None:
        """Dragged onto a monitor with another scale: rebuild at its size."""
        self._scale_job = None
        if winapi.window_scale(self.root.winfo_id()) != self._scale:
            self.build()
