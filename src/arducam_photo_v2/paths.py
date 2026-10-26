from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "ArducamPhotoV2"


def data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / APP_DIR_NAME
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def default_photo_dir() -> Path:
    pictures = os.environ.get("USERPROFILE")
    root = Path(pictures) / "Pictures" if pictures else Path.home() / "Pictures"
    return root / APP_DIR_NAME
