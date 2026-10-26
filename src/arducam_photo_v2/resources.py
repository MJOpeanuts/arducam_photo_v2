"""Packaged visual resources, read through importlib.resources.

Works from the sources, from any working directory and from the PyInstaller
bundle; nothing is downloaded at run time.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

LOGO = "logo_ref.png"
POWERED_BY = "poweredby.png"
WINDOW_ICON = "icon_ref.png"
WINDOWS_ICON = "app.ico"
SQUIRREL = "lucide-squirrel.svg"
CHEVRON = "lucide-chevron-down.svg"
ALL = (LOGO, POWERED_BY, WINDOW_ICON, WINDOWS_ICON, SQUIRREL, CHEVRON, "LICENSE-lucide.txt")


@lru_cache(maxsize=None)
def read_bytes(name: str) -> bytes:
    if name not in ALL:
        raise KeyError(f"Ressource inconnue : {name}")
    return resources.files(__package__).joinpath("resources", name).read_bytes()


def file_path(name: str) -> str:
    """Filesystem path for Qt style sheets (package and PyInstaller bundle are unpacked on disk)."""
    if name not in ALL:
        raise KeyError(f"Ressource inconnue : {name}")
    return str(resources.files(__package__).joinpath("resources", name)).replace("\\", "/")


def pixmap(name: str, height: int, ratio: float = 1.0):
    """Scale to a logical height, keeping proportions and transparency."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    source = QPixmap()
    if not source.loadFromData(read_bytes(name)):
        raise ValueError(f"Ressource illisible : {name}")
    scaled = source.scaledToHeight(max(1, round(height * ratio)), Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    return scaled


def squirrel(size: int, color: str, ratio: float = 1.0):
    """Render the Lucide squirrel icon; currentColor is set as its stroke colour."""
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    svg = read_bytes(SQUIRREL).replace(b"currentColor", color.encode("ascii"))
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise ValueError("Icône squirrel illisible.")
    physical = max(1, round(size * ratio))
    image = QPixmap(physical, physical)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


def window_icon():
    from PySide6.QtGui import QIcon, QPixmap

    icon = QIcon()
    source = QPixmap()
    if not source.loadFromData(read_bytes(WINDOW_ICON)):
        raise ValueError("Icône de fenêtre illisible.")
    icon.addPixmap(source)
    return icon


def squirrel_icon():
    """Explicit state pixmaps keep the local SVG legible, including when disabled."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
        color = "#59636D" if mode == QIcon.Mode.Disabled else "#FFFFFF"
        for ratio in (1.0, 1.25, 1.5, 2.0, 3.0):
            icon.addPixmap(squirrel(26, color, ratio), mode)
    return icon
