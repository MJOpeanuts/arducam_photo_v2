from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

SCHEMA_VERSION = 1
FOCUS_MIN = 0
FOCUS_MAX = 1023


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
class Bank:
    id: str
    name: str
    mode: str
    focus: int | None
    schema_version: int = SCHEMA_VERSION
    revision: int = 1
    camera_index: int = 0
    backend: str = "MSMF"

    @classmethod
    def draft(cls, name: str, mode: str, focus: int | None, camera_index: int = 0,
              backend: str = "MSMF") -> "Bank":
        bank = cls(str(uuid4()), name.strip(), mode, focus, camera_index=camera_index, backend=backend)
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
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Version de schéma de bank non prise en charge.")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Révision de bank invalide.")
        if type(self.camera_index) is not int or self.camera_index < 0:
            raise ValueError("Index caméra invalide.")
        if self.backend not in ("MSMF", "DSHOW", "ANY"):
            raise ValueError("Backend caméra non pris en charge.")

    def snapshot(self) -> dict:
        self.validate()
        return asdict(self)
