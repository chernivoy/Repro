"""Mouse clicks and keystrokes drawn into the video, like CleanShot's "Highlight clicks" and
"Show keystrokes". Nothing is shown on screen, only in the video.

Keys come from a low-level keyboard hook: the Mac RDP clients send typed characters as Unicode
packets without a key code, which only a hook sees (Raw Input and key state polling miss
them). The hook only queues each key and returns at once, so typing never waits on the encoder;
a second thread turns the keys into what the video shows. The mouse buttons are polled."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import winapi
from .hotkeys import parse_hotkey
from .winapi import kernel32, user32

log = logging.getLogger(__name__)

POLL_SECONDS = 0.008  # mouse buttons; each poll costs ~12 µs per button in an RDP session
CLICK_SECONDS = 0.45  # how long the ring of a click spreads and fades
KEYS_HOLD = 1.6  # how long keystrokes stay after the last key
KEYS_FADE = 0.4
MAX_TOKENS = 12
MAX_TEXT = 60

KEYS_ALL, KEYS_SHORTCUTS = "all", "shortcuts"

VK_CAPITAL, VK_SPACE, VK_PACKET = 0x14, 0x20, 0xE7  # VK_PACKET: a Unicode character, no key behind it
SHIFT_KEYS, CTRL_KEYS, ALT_KEYS, WIN_KEYS = {0x10, 0xA0, 0xA1}, {0x11, 0xA2, 0xA3}, {0x12, 0xA4, 0xA5}, {0x5B, 0x5C}
BUTTONS = (0x01, 0x02, 0x04)  # physical left, right, middle
F_KEYS = {0x6F + n: f"F{n}" for n in range(1, 25)}
NAMED_KEYS = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space", 0x21: "PgUp", 0x22: "PgDn",
    0x23: "End", 0x24: "Home", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓", 0x2C: "PrtSc", 0x2D: "Insert",
    0x2E: "Delete", 0x5D: "Menu",
}
SHORTCUT_MODE_KEYS = {0x0D, 0x08}  # Enter and Backspace show in the shortcuts-only mode too
CONTROL_CHARACTERS = {"\r": 0x0D, "\n": 0x0D, "\t": 0x09, "\b": 0x08, "\x1b": 0x1B}  # sent as Unicode by some clients
# Shortcuts are named after the US keys: that is how Windows apps document them.
US_SYMBOLS = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
    0xDD: "]", 0xDE: "'", 0xE2: "\\",
}
NUMPAD = {**{0x60 + n: f"Num {n}" for n in range(10)}, 0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -", 0x6E: "Num .",
          0x6F: "Num /"}
TYPING_KEYS = [*range(0x30, 0x3A), *range(0x41, 0x5B), *NUMPAD, *US_SYMBOLS]
WATCHED_KEYS = {*NAMED_KEYS, *TYPING_KEYS, *F_KEYS}

# Click colours per button: amber left, blue right (both can be changed in the settings), green middle.
DEFAULT_LEFT_COLOR, DEFAULT_RIGHT_COLOR, MIDDLE_COLOR = "#ffbe00", "#0a84ff", "#30d158"


def bgr(color: str, default: str) -> tuple[int, int, int]:
    """'#rrggbb' as the BGR the frames use; `default` when it is not a valid colour."""
    text = color if len(color) == 7 and color[0] == "#" else default
    try:
        red, green, blue = (int(text[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return bgr(default, default)
    return blue, green, red


@dataclass(frozen=True)
class Click:
    button: int  # 0 left, 1 right, 2 middle
    x: int  # physical screen point
    y: int
    at: float  # time.perf_counter() of the press


@dataclass(frozen=True)
class Token:
    text: str
    key: bool  # a key or shortcut ("Ctrl + C") rather than typed text
    count: int = 1  # the same shortcut pressed again in a row


def key_name(vk: int) -> str:
    if vk in NAMED_KEYS:
        return NAMED_KEYS[vk]
    if vk in F_KEYS:
        return F_KEYS[vk]
    if vk in NUMPAD:
        return NUMPAD[vk]
    if vk in US_SYMBOLS:
        return US_SYMBOLS[vk]
    return chr(vk)  # digits and Latin letters share their codes with the virtual keys


def _shortcut(name: str, ctrl: bool, alt: bool, shift: bool, win: bool) -> Token:
    modifiers = [label for held, label in ((win, "Win"), (ctrl, "Ctrl"), (alt, "Alt"), (shift, "Shift")) if held]
    return Token(" + ".join([*modifiers, name]), key=True)


def describe(
    vk: int, ctrl: bool, alt: bool, shift: bool, win: bool, mode: str, typed: Callable[[bool], str]
) -> Token | None:
    """The token a key press shows, or None. `typed(altgr)` returns what the key types."""
    if ctrl and alt and not win:  # AltGr: on many layouts it types a character
        text = typed(True)
        if text.isprintable() and text.strip():
            return Token(text, key=False) if mode == KEYS_ALL else None
    if win or ctrl or alt or vk in F_KEYS or vk in SHORTCUT_MODE_KEYS:
        return _shortcut(key_name(vk), ctrl, alt, shift, win)
    if mode != KEYS_ALL:
        return None
    if vk == VK_SPACE:
        return Token(" ", key=False)
    if vk in NAMED_KEYS:
        return Token(("Shift + " if shift else "") + NAMED_KEYS[vk], key=True)
    text = typed(False)
    return Token(text, key=False) if text.isprintable() and text else None


def describe_character(char: str, ctrl: bool, alt: bool, shift: bool, win: bool, mode: str) -> Token | None:
    """The token for a character that arrived as a Unicode packet rather than a key."""
    if char in CONTROL_CHARACTERS:
        return describe(CONTROL_CHARACTERS[char], ctrl, alt, shift, win, mode, lambda _altgr: "")
    if not char.isprintable():
        return None
    if win or (ctrl != alt):  # Ctrl and Alt together are AltGr, which types characters
        return _shortcut("Space" if char == " " else char.upper(), ctrl, alt, shift, win)
    return Token(char, key=False) if mode == KEYS_ALL else None


def add_token(tokens: list[Token], token: Token) -> list[Token]:
    """Typed characters join into one run of text; Backspace takes the last one back."""
    last = tokens[-1] if tokens else None
    if token.key and token.text == "Backspace" and last and not last.key:
        text = last.text[:-1]
        return tokens[:-1] + ([Token(text, key=False)] if text else [])
    if not token.key and last and not last.key:
        return tokens[:-1] + [Token((last.text + token.text)[-MAX_TEXT:], key=False)]
    if token.key and last and last.key and last.text == token.text:
        return tokens[:-1] + [Token(last.text, key=True, count=last.count + 1)]
    return (tokens + [token])[-MAX_TOKENS:]


class InputTracker:
    """Follows the mouse buttons and the keyboard while recording."""

    def __init__(self, clicks: bool, keystrokes: str, hidden_hotkeys: tuple[str, ...] = ()) -> None:
        self.clicks_enabled = clicks
        self.keystrokes = keystrokes  # "", KEYS_ALL or KEYS_SHORTCUTS
        self._hidden = set()
        for hotkey in hidden_hotkeys:  # the app's own hotkeys stay out of the video
            try:
                self._hidden.add(parse_hotkey(hotkey))
            except ValueError:
                pass
        self._own = os.getpid()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._clicks: list[Click] = []
        self._held: dict[int, bool] = {}  # button -> held after a press outside the app's windows
        self._tokens: list[Token] = []
        self._last_key = -math.inf
        self._paused = False
        self._down: dict[int, float] = {}  # keys held -> when pressed, to tell presses from auto-repeat
        self._caps = False
        self._surrogate = ""  # the first half of a character outside the Basic Multilingual Plane
        self._counts = {"keys": 0, "characters": 0, "in Repro": 0, "while paused": 0}  # for the log
        self._buttons = threading.Thread(target=self._poll_buttons, name="mouse buttons", daemon=True)
        self._keyboard: KeyboardHook | None = None

    def start(self) -> None:
        if self.clicks_enabled:
            self._buttons.start()
        if self.keystrokes:
            self._caps = winapi.caps_lock_on()
            self._keyboard = KeyboardHook(self._on_hook_key, self._caught_up)
            if not self._keyboard.start():
                log.warning("The keyboard hook could not be installed: keystrokes will not show")

    def stop(self) -> None:
        self._stop.set()
        if self._buttons.is_alive():
            self._buttons.join(timeout=1)
        if self._keyboard:
            self._keyboard.stop()
            # Counts only, never which keys: enough to tell why keystrokes did not show.
            log.info("Keyboard: %s", ", ".join(f"{count} {name}" for name, count in self._counts.items()))

    def set_paused(self, paused: bool) -> None:
        """Input while paused never reaches the video, e.g. a password typed during a pause."""
        with self._lock:
            self._paused = paused
            self._clicks, self._held, self._tokens = [], {}, []

    def clicks(self, now: float) -> tuple[list[Click], list[int]]:
        """Recent clicks and the buttons held down right now."""
        with self._lock:
            self._clicks = [click for click in self._clicks if now - click.at < CLICK_SECONDS]
            return list(self._clicks), [button for button, held in self._held.items() if held]

    def keys(self, now: float) -> tuple[tuple[Token, ...], float]:
        """The keystrokes on show and their opacity."""
        with self._lock:
            age = now - self._last_key
            if age >= KEYS_HOLD + KEYS_FADE or not self._tokens:
                return (), 0.0
            return tuple(self._tokens), min(1.0, (KEYS_HOLD + KEYS_FADE - age) / KEYS_FADE)

    # --- Mouse ---------------------------------------------------------------------

    def _poll_buttons(self) -> None:
        down = {vk: winapi.key_state(vk)[0] for vk in BUTTONS}  # also clears "pressed since"
        while not self._stop.wait(POLL_SECONDS):
            now = time.perf_counter()
            for vk in BUTTONS:
                is_down, tapped = winapi.key_state(vk)
                if not down[vk] and (is_down or tapped):  # "tapped": pressed and let go between polls
                    self._on_button(vk, is_down, now)
                elif down[vk] and not is_down:
                    self._release(vk)
                down[vk] = is_down

    def _button_index(self, vk: int) -> int:
        index = BUTTONS.index(vk)
        if index < 2 and winapi.mouse_buttons_swapped():  # the physical buttons are polled
            index = 1 - index
        return index

    def _on_button(self, vk: int, is_down: bool, now: float) -> None:
        if self._paused:
            return
        x, y = winapi.cursor_position()
        if winapi.process_at(x, y) == self._own:  # e.g. the stop button, which the video leaves out
            return
        button = self._button_index(vk)
        with self._lock:
            self._clicks.append(Click(button, x, y, now))
            self._held[button] = is_down

    def _release(self, vk: int) -> None:
        with self._lock:
            self._held[self._button_index(vk)] = False

    # --- Keyboard --------------------------------------------------------------------

    def _on_hook_key(self, vk: int, scan: int, pressed: bool, at: float) -> None:
        """Every key event, in order, on the hook's worker thread."""
        if vk == VK_PACKET:
            if pressed:
                self._on_packet(scan, at)
            return
        if not pressed:
            self._down.pop(vk, None)
            return
        if vk in self._down:
            return  # auto-repeat while the key is held
        self._down[vk] = at
        if vk == VK_CAPITAL:
            self._caps = not self._caps
        elif vk in WATCHED_KEYS:
            self._counts["keys"] += 1
            self._on_key(vk, at)

    def _caught_up(self) -> None:
        """With no events waiting, a key still "held" a while after its press but up for Windows
        lost its release (e.g. Win+L locked the session): forget it, or it would stick."""
        now = time.perf_counter()
        for vk, at in list(self._down.items()):
            if now - at > 1 and not winapi.key_state(vk)[0]:
                del self._down[vk]

    def _modifiers(self) -> tuple[bool, bool, bool, bool]:
        held = self._down.keys()
        return (bool(CTRL_KEYS & held), bool(ALT_KEYS & held), bool(SHIFT_KEYS & held), bool(WIN_KEYS & held))

    def _on_packet(self, code: int, at: float) -> None:
        if 0xD800 <= code < 0xDC00:
            self._surrogate = chr(code)
            return
        char = (self._surrogate + chr(code)).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        self._surrogate = ""
        self._counts["characters"] += 1
        if self._skip():
            return
        self._show(describe_character(char, *self._modifiers(), self.keystrokes), at)

    def _on_key(self, vk: int, at: float) -> None:
        if self._skip():
            return
        ctrl, alt, shift, win = self._modifiers()
        flags = (winapi.MOD_CONTROL * ctrl) | (winapi.MOD_ALT * alt) | (winapi.MOD_SHIFT * shift) | (winapi.MOD_WIN * win)
        if (flags, vk) in self._hidden:
            return
        layout = winapi.foreground_layout()
        caps = self._caps
        self._show(describe(
            vk, ctrl, alt, shift, win, self.keystrokes,
            lambda altgr: winapi.key_text(vk, shift, caps, altgr, layout),
        ), at)

    def _skip(self) -> bool:
        if self._paused:
            self._counts["while paused"] += 1
            return True
        if winapi.foreground_process() == self._own:
            self._counts["in Repro"] += 1
            return True
        return False

    def _show(self, token: Token | None, at: float) -> None:
        if token is None:
            return
        with self._lock:
            if at - self._last_key >= KEYS_HOLD + KEYS_FADE:
                self._tokens = []  # the previous line has faded: start a new one
            self._tokens = add_token(self._tokens, token)
            self._last_key = at


class KeyboardHook:
    """Every key event in the session, whichever app has the focus. The hook callback only
    queues the event; `on_key(vk, scan, pressed, at)` runs on a worker thread, and `caught_up()`
    whenever the queue runs empty."""

    _active: list[KeyboardHook] = []

    def __init__(self, on_key: Callable[[int, int, bool, float], None], caught_up: Callable[[], None]) -> None:
        self._on_key = on_key
        self._caught_up = caught_up
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._ok = False
        self._thread_id = 0
        self._hook_thread = threading.Thread(target=self._run_hook, name="keyboard hook", daemon=True)
        self._worker = threading.Thread(target=self._work, name="keyboard", daemon=True)

    def start(self) -> bool:
        self._worker.start()
        self._hook_thread.start()
        self._ready.wait(2)
        return self._ok

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, winapi.WM_QUIT, 0, 0)
            self._hook_thread.join(timeout=1)
        self._done.set()
        self._worker.join(timeout=1)

    def _run_hook(self) -> None:
        # A low-level hook is called on the thread that set it, from its message loop.
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(winapi.WH_KEYBOARD_LL, _keyboard_proc, kernel32.GetModuleHandleW(None), 0)
        self._ok = bool(hook)
        if hook:
            KeyboardHook._active.append(self)
        self._ready.set()
        if not hook:
            return
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            KeyboardHook._active.remove(self)
            user32.UnhookWindowsHookEx(hook)

    def _work(self) -> None:
        while not self._done.is_set():
            try:
                event = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._on_key(*event)
                if self._events.empty():
                    self._caught_up()
            except Exception:
                log.exception("Handling a key failed")


@winapi.HOOKPROC
def _keyboard_proc(code: int, wparam: int, lparam: int) -> int:
    if code == winapi.HC_ACTION and KeyboardHook._active:
        info = ctypes.cast(lparam, ctypes.POINTER(winapi.KBDLLHOOKSTRUCT)).contents
        event = (info.vkCode, info.scanCode, wparam in (winapi.WM_KEYDOWN, winapi.WM_SYSKEYDOWN), time.perf_counter())
        for hook in KeyboardHook._active:
            hook._events.put(event)
    return user32.CallNextHookEx(None, code, wparam, lparam)


# --- Drawing -------------------------------------------------------------------------

Restore = Callable[[], None]


def _blend(frame: np.ndarray, left: int, top: int, color: np.ndarray, alpha: np.ndarray) -> Restore | None:
    """Composites premultiplied `color` with `alpha` (both float32) onto the BGRA frame at
    (left, top), clipped to the frame. Returns a callable that puts the pixels back."""
    height, width = alpha.shape[:2]
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + width, frame.shape[1]), min(top + height, frame.shape[0])
    if x0 >= x1 or y0 >= y1:
        return None
    cut = np.s_[y0 - top:y1 - top, x0 - left:x1 - left]
    area = frame[y0:y1, x0:x1, :3]
    saved = area.copy()
    area[:] = np.clip(saved * (1 - alpha[cut]) + color[cut] + 0.5, 0, 255).astype(np.uint8)

    def restore() -> None:
        area[:] = saved

    return restore


def _circle(frame: np.ndarray, cx: float, cy: float, radius: float, bgr: tuple[int, int, int], opacity: float,
            ring: float | None = None) -> Restore | None:
    """An antialiased disc, or a ring `ring` pixels wide."""
    reach = math.ceil(radius + (ring or 0) / 2 + 1)
    left, top = math.floor(cx) - reach, math.floor(cy) - reach
    ys, xs = np.ogrid[top:top + 2 * reach + 2, left:left + 2 * reach + 2]
    distance = np.sqrt((xs + 0.5 - cx) ** 2 + (ys + 0.5 - cy) ** 2, dtype=np.float32)
    if ring is None:
        cover = np.clip(radius - distance + 0.5, 0, 1)
    else:
        cover = np.clip(ring / 2 + 0.5 - np.abs(distance - radius), 0, 1)
    alpha = (cover * opacity)[..., np.newaxis].astype(np.float32)
    return _blend(frame, left, top, alpha * np.array(bgr, np.float32), alpha)


class InputPainter:
    """Draws what the tracker saw onto video frames: clicks under the pointer, keys on top."""

    def __init__(
        self, tracker: InputTracker, scale: float, video_size: tuple[int, int], zoom: float = 1.0,
        click_colors: tuple[str, str] = (DEFAULT_LEFT_COLOR, DEFAULT_RIGHT_COLOR),
    ) -> None:
        """`scale`: screen pixels per video pixel; `zoom`: video pixels per logical pixel, so
        clicks and keys keep their size next to the content at any resolution. `click_colors`:
        '#rrggbb' for the left and the right button."""
        self._tracker = tracker
        self._click_colors = (
            bgr(click_colors[0], DEFAULT_LEFT_COLOR), bgr(click_colors[1], DEFAULT_RIGHT_COLOR),
            bgr(MIDDLE_COLOR, MIDDLE_COLOR),
        )
        self._scale = scale
        self._zoom = zoom
        width, height = video_size
        self._width, self._height = width, height
        self._font_px = round(max(13, min(24, width / zoom * 0.0125)) * zoom)
        self._margin = max(8, round(height * 0.05))
        self._cache: tuple[tuple[Token, ...], np.ndarray, np.ndarray] | None = None

    def busy(self) -> bool:
        """Whether clicks or keystrokes are on show, so the video keeps getting frames for them."""
        now = time.perf_counter()
        clicks, held = self._tracker.clicks(now) if self._tracker.clicks_enabled else ([], [])
        return bool(clicks or held or (self._tracker.keystrokes and self._tracker.keys(now)[0]))

    def paint_clicks(self, frame: np.ndarray, origin_x: int, origin_y: int) -> list[Restore]:
        if not self._tracker.clicks_enabled:
            return []
        now = time.perf_counter()
        clicks, held = self._tracker.clicks(now)
        restores = []
        if held:  # a soft disc under the pointer while a button is down, following drags
            x, y = winapi.cursor_position()
            for button in held:
                restores.append(_circle(frame, (x - origin_x) / self._scale, (y - origin_y) / self._scale,
                                        14 * self._zoom, self._click_colors[button], 0.35))
        for click in clicks:  # a filled circle with a ring, spreading out from each press and fading
            progress = (now - click.at) / CLICK_SECONDS
            radius = (8 + 18 * (1 - (1 - progress) ** 3)) * self._zoom
            x, y = (click.x - origin_x) / self._scale, (click.y - origin_y) / self._scale
            color = self._click_colors[click.button]
            restores.append(_circle(frame, x, y, radius, color, 0.3 * (1 - progress)))
            restores.append(_circle(frame, x, y, radius, color, 0.9 * (1 - progress), ring=3 * self._zoom))
        return [restore for restore in restores if restore]

    def paint_keys(self, frame: np.ndarray) -> list[Restore]:
        if not self._tracker.keystrokes:
            return []
        tokens, opacity = self._tracker.keys(time.perf_counter())
        if not tokens:
            return []
        if self._cache is None or self._cache[0] != tokens:
            self._cache = (tokens, *self._render(tokens))
        _, color, alpha = self._cache
        height, width = alpha.shape[:2]
        left, top = (self._width - width) // 2, self._height - self._margin - height
        restore = _blend(frame, left, top, color * opacity, alpha * opacity)
        return [restore] if restore else []

    def _render(self, tokens: tuple[Token, ...]) -> tuple[np.ndarray, np.ndarray]:
        """A dark pill with typed text in white and keys in lighter key caps, as wide as fits."""
        factor = 2  # drawn large and scaled down for smooth edges
        size = self._font_px * factor
        text_font = ImageFont.truetype("segoeui.ttf", size)
        key_font = ImageFont.truetype("seguisb.ttf", round(size * 0.92))
        pad_x, pad_y, gap = round(size * 0.75), round(size * 0.45), round(size * 0.45)
        cap_x, cap_y = round(size * 0.4), round(size * 0.18)
        line = round(size * 1.45)

        def parts(token: Token) -> list[tuple[str, ImageFont.FreeTypeFont, bool]]:
            if not token.key:
                return [(token.text, text_font, False)]
            items = [(token.text, key_font, True)]
            if token.count > 1:
                items.append((f"×{token.count}", text_font, False))
            return items

        def width_of(text: str, font: ImageFont.FreeTypeFont, cap: bool) -> int:
            return round(font.getlength(text)) + (2 * cap_x if cap else 0)

        items = [part for token in tokens for part in parts(token)]
        limit = (self._width - 2 * self._margin) * factor - 2 * pad_x
        while True:
            total = sum(width_of(*item) for item in items) + gap * (len(items) - 1)
            if total <= limit or not items:
                break
            text, font, cap = items[0]
            if len(items) > 1:
                items.pop(0)
            elif not cap and len(text) > 1:
                items[0] = ("…" + text.lstrip("…")[1:], font, cap)  # keep the newest characters
            else:
                break

        width, height = total + 2 * pad_x, line + 2 * pad_y
        # ImageDraw replaces pixels instead of blending them, so each layer is drawn on its own
        # and composited: drawn straight onto the pill, the translucent key caps punched holes
        # in it and white text vanished over white pages.
        layers = [Image.new("RGBA", (width, height), (0, 0, 0, 0)) for _ in range(3)]
        pill, caps, texts = (ImageDraw.Draw(layer) for layer in layers)
        pill.rounded_rectangle((0, 0, width - 1, height - 1), radius=round(size * 0.55), fill=(24, 24, 27, 215))
        x = pad_x
        middle = height / 2
        for text, font, cap in items:
            item_width = width_of(text, font, cap)
            if cap:
                caps.rounded_rectangle(
                    (x, middle - line / 2 + cap_y, x + item_width, middle + line / 2 - cap_y),
                    radius=round(size * 0.25), fill=(255, 255, 255, 46), outline=(255, 255, 255, 70),
                    width=max(1, factor),
                )
                texts.text((x + cap_x, middle), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
            else:
                fill = (255, 255, 255, 255) if not text.startswith("×") else (190, 190, 198, 255)
                texts.text((x, middle), text, font=font, fill=fill, anchor="lm")
            x += item_width + gap
        image = Image.alpha_composite(Image.alpha_composite(layers[0], layers[1]), layers[2])
        image = image.resize((max(1, width // factor), max(1, height // factor)), Image.Resampling.LANCZOS)
        rgba = np.asarray(image, dtype=np.float32)
        alpha = rgba[..., 3:4] / 255
        return rgba[..., 2::-1] * alpha, alpha
