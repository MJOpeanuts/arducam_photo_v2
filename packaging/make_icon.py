"""Build the multi-resolution Windows icon from assets/icon_ref.png.

Usage: python packaging/make_icon.py  (requires Pillow)
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "icon_ref.png"
TARGET = ROOT / "src" / "arducam_photo_v2" / "resources" / "app.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def main() -> None:
    image = Image.open(SOURCE).convert("RGBA")
    if image.width != image.height:
        side = max(image.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        image = canvas
    image.save(TARGET, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"{TARGET} ({', '.join(map(str, SIZES))} px)")


if __name__ == "__main__":
    main()
