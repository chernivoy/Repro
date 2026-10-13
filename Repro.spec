# PyInstaller spec. Default: dist/Repro.exe, a single portable file that unpacks itself on every
# launch (~12 s, with a splash meanwhile). With "-- --folder": dist/Repro/, a portable folder
# that starts in a couple of seconds.
import argparse
import os
import sys

from PyInstaller.utils.win32.winmanifest import _DEFAULT_MANIFEST_XML

parser = argparse.ArgumentParser()
parser.add_argument("--folder", action="store_true")
options = parser.parse_args()

sys.path.insert(0, SPECPATH)
from repro.icons import save_ico
from repro.splash import save_splash

assets = os.path.join(SPECPATH, "build", "assets")
os.makedirs(assets, exist_ok=True)
icon = os.path.join(assets, "Repro.ico")
save_ico(icon)  # the same file the tray loads its icon from

# Per-monitor DPI-aware from the start, as the app makes itself anyway: otherwise Windows
# stretches the splash (shown before Python runs) on a 200% screen, and it comes out blurry.
DPI_AWARE = (
    '<dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>'
    '<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2, PerMonitor</dpiAwareness>'
)
manifest = _DEFAULT_MANIFEST_XML.decode("utf-8").replace("<windowsSettings>", "<windowsSettings>" + DPI_AWARE)
assert DPI_AWARE in manifest

a = Analysis([os.path.join(SPECPATH, "run.py")], pathex=[SPECPATH])
# OpenCV's own FFmpeg plugin (~25 MB) is unused: encoding goes through PyAV.
a.binaries = [entry for entry in a.binaries if "opencv_videoio_ffmpeg" not in entry[0].lower()]
pyz = PYZ(a.pure)

if options.folder:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Repro", console=False, icon=icon,
              manifest=manifest, upx=False)
    COLLECT(exe, a.binaries, a.datas, name="Repro", upx=False)
else:
    splash_image = os.path.join(assets, "splash.png")
    splash_script = save_splash(splash_image)

    class ReproSplash(Splash):
        """The splash at the screen's scale, with a sliding bar (see repro/splash.py)."""

        def generate_script(self):
            with open(self.script_name, "w", encoding="utf-8") as file:
                file.write(splash_script)
            return splash_script

    # The picture holds the splash at several scales, so it is larger than PyInstaller's default limit.
    splash = ReproSplash(splash_image, binaries=a.binaries, datas=a.datas, text_pos=None, always_on_top=True,
                         max_img_size=(10_000, 10_000))
    exe = EXE(
        pyz,
        a.scripts,
        splash,
        splash.binaries,
        a.binaries,
        a.datas,
        [],
        name="Repro",
        console=False,
        icon=icon,
        manifest=manifest,
        upx=False,
    )
