"""User settings, stored as JSON in %APPDATA%\\Repro next to the log."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .winapi import videos_folder

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "Repro"
SETTINGS_FILE = APP_DIR / "settings.json"
OLD_SETTINGS_FILE = APP_DIR.with_name("MakeScreen") / "settings.json"  # the app was called MakeScreen before

log = logging.getLogger(__name__)


@dataclass
class Settings:
    output_dir: str = ""
    # Monitors are stored as "primary" or "secondary:<n>": RDP renumbers displays on every reconnect.
    source: str = "primary"  # the monitor to record
    region: list[int] | None = None  # x, y, width, height on it in physical pixels; None: all of it
    areas: dict[str, list[int]] = field(default_factory=dict)  # last custom area per monitor (crop button)
    hotkey_record: str = "ctrl+alt+r"  # opens the capture panel, pressed again starts recording
    hotkey_stop: str = "ctrl+alt+r"  # stops recording; the same as hotkey_record makes one toggle
    fps: int = 30
    speed: float = 1.0  # video speed, one of recorder.SPEEDS
    quality: str = "standard"  # a key of recorder.QUALITIES
    resolution: str = "half"  # a key of recorder.RESOLUTIONS
    sharpness: float = 0.2  # one of recorder.SHARPNESS; 0 is off
    draw_cursor: bool = True
    highlight_clicks: bool = True
    click_color_left: str = "#ffbe00"  # '#rrggbb'
    click_color_right: str = "#0a84ff"
    show_keys: bool = True
    keys_mode: str = "all"  # "all" or "shortcuts" (with Ctrl, Alt or Win; F-keys, Enter, Backspace)
    theme: str = ""  # "light" or "dark"; empty: as Windows' app mode, until picked

    @classmethod
    def load(cls) -> Settings:
        settings = cls()
        renamed = False
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = _settings_before_rename()
            renamed = bool(data)
        except (OSError, ValueError) as error:
            log.warning("Ignoring unreadable settings: %s", error)
            data = {}
        if "region_monitor" in data:  # written before the capture overlay, when a region was its own source
            if data.get("region"):
                data.setdefault("areas", {data["region_monitor"]: data["region"]})
            if data.get("source") == "region":
                data["source"] = data["region_monitor"]
            else:
                data["region"] = None
        for item in fields(cls):
            if item.name not in data:
                continue
            default, value = getattr(settings, item.name), data[item.name]
            if default is None or type(value) is type(default):
                setattr(settings, item.name, value)
        if not settings.output_dir:
            settings.output_dir = str(Path(videos_folder()) / "Repro")
        if renamed:
            settings.save()
            log.info("Settings carried over from %s", OLD_SETTINGS_FILE.parent)
        return settings

    def save(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            log.warning("Could not save settings: %s", error)


def _settings_before_rename() -> dict:
    """The settings saved under the old name, if any. Videos went to Videos\\MakeScreen by default
    then; that default now becomes Videos\\Repro, while a folder picked by hand is kept."""
    try:
        data = json.loads(OLD_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    old_default = os.path.normcase(os.path.normpath(str(Path(videos_folder()) / "MakeScreen")))
    if os.path.normcase(os.path.normpath(str(data.get("output_dir") or ""))) == old_default:
        data["output_dir"] = ""
    return data
