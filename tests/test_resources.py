import os
import subprocess
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication

from arducam_photo_v2 import resources

ROOT = Path(__file__).resolve().parents[1]


def test_all_resources_are_packaged_and_readable():
    for name in resources.ALL:
        assert resources.read_bytes(name)
    assert b"<svg" in resources.read_bytes(resources.SQUIRREL)
    assert b"ISC" in resources.read_bytes("LICENSE-lucide.txt")


def test_packaged_logos_are_the_repository_assets():
    for name in (resources.LOGO, resources.POWERED_BY, resources.WINDOW_ICON):
        assert resources.read_bytes(name) == (ROOT / "assets" / name).read_bytes()


def test_pixmaps_keep_proportions_and_transparency():
    QApplication.instance() or QApplication([])
    logo = resources.pixmap(resources.LOGO, 40)
    assert logo.height() == 40 and abs(logo.width() / 40 - 1056 / 476) < 0.05
    assert logo.hasAlphaChannel()
    hi = resources.pixmap(resources.POWERED_BY, 18, 1.5)
    assert hi.height() == 27 and hi.deviceIndependentSize().height() == 18
    squirrel = resources.squirrel(64, "#FF6F4C").toImage().convertToFormat(QImage.Format.Format_ARGB32)
    alphas = {squirrel.pixelColor(x, y).alpha() for x in range(0, 64, 2) for y in range(0, 64, 2)}
    assert 0 in alphas and 255 in alphas
    assert not resources.window_icon().isNull()


def test_windows_ico_is_multi_resolution_and_square():
    with Image.open(Path(resources.__file__).parent / "resources" / resources.WINDOWS_ICON) as ico:
        sizes = ico.info["sizes"]
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes
    assert all(w == h for w, h in sizes)


def test_capture_icon_has_explicit_legible_high_dpi_states():
    QApplication.instance() or QApplication([])
    icon = resources.squirrel_icon()
    for mode, expected in ((QIcon.Mode.Normal, "#ffffff"), (QIcon.Mode.Disabled, "#59636d")):
        for ratio in (1.0, 1.25, 1.5, 2.0):
            image = icon.pixmap(QSize(26, 26), ratio, mode).toImage()
            assert image.width() >= round(26 * ratio)
            colors = {image.pixelColor(x, y).name()
                      for x in range(image.width()) for y in range(image.height())
                      if image.pixelColor(x, y).alpha() > 200}
            assert expected in colors


def test_resources_load_from_another_working_directory(tmp_path):
    code = ("from arducam_photo_v2 import resources;"
            "print(len(resources.read_bytes(resources.LOGO)))")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True)
    assert int(out.stdout) == (ROOT / "assets" / resources.LOGO).stat().st_size
