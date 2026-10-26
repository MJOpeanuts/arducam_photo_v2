from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from uuid import uuid4

FOCUS_MIN = 0
FOCUS_MAX = 1023
BACKENDS = ("MSMF", "V4L2")
DEFAULT_BACKEND = "MSMF" if sys.platform == "win32" else "V4L2"


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    width: int
    height: int
    raw: bool = False


MODES = {
    "color_720p": Mode("color_720p", "Couleur — 1280 × 720", 1280, 720),
    "color_4k": Mode("color_4k", "Couleur — 3840 × 2160", 3840, 2160),
    "color_12mp": Mode("color_12mp", "Couleur — 4000 × 3000", 4000, 3000),
    "raw_108mp": Mode("raw_108mp", "RAW Bayer — 12000 × 9000", 12000, 9000, True),
}


@dataclass(frozen=True)
class CameraDevice:
    """A camera as enumerated by the OS.

    ``index`` is only the OpenCV position at enumeration time; the hardware
    identity is ``path`` (device symbolic link) and, failing that, ``name``.
    """

    backend: str
    index: int
    name: str | None = None
    path: str | None = None

    def validate(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError("Backend caméra non pris en charge.")
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Index caméra invalide.")
        for value in (self.name, self.path):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("Identité caméra invalide.")

    @property
    def identity(self) -> tuple[str, str | None, str | None]:
        return self.backend, self.path, self.name

    @property
    def label(self) -> str:
        name = self.name or "Périphérique sans nom"
        return f"{name}  ·  n° {self.index}"


@dataclass(frozen=True)
class Bank:
    id: str
    name: str
    mode: str
    focus: int | None
    camera: CameraDevice | None = None
    revision: int = 1

    @classmethod
    def draft(cls, name: str, mode: str, focus: int | None,
              camera: CameraDevice | None = None) -> "Bank":
        bank = cls(str(uuid4()), name.strip(), mode, focus, camera)
        bank.validate()
        return bank

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Identifiant de bank manquant.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Le nom de la bank ne peut pas être vide.")
        if self.mode not in MODES:
            raise ValueError(f"Mode d’acquisition inconnu : {self.mode}.")
        if self.focus is not None and (type(self.focus) is not int or not FOCUS_MIN <= self.focus <= FOCUS_MAX):
            raise ValueError("Le focus doit être un entier compris entre 0 et 1023.")
        if self.camera is not None:
            if not isinstance(self.camera, CameraDevice):
                raise ValueError("Contexte caméra invalide.")
            self.camera.validate()
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Révision de bank invalide.")

    def snapshot(self) -> dict:
        self.validate()
        return asdict(self)
