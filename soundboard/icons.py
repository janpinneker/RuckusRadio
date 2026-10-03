"""Load arbitrary images and crop them into circular PNG tile icons."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from soundboard.theme import BG

ICON_SIZE = 128
UNREADABLE_IMAGE_MESSAGE = "Die Datei ist kein lesbares Bild."


class IconError(ValueError):
    """The chosen icon file is missing or not a readable image (German message)."""

    def __init__(self, message: str = UNREADABLE_IMAGE_MESSAGE):
        super().__init__(message)


def load_circle(path: str | Path, size: int = ICON_SIZE) -> Image.Image:
    """Open an image file and crop it to a circle; IconError if it can't be read."""
    try:
        with Image.open(path) as img:
            return crop_to_circle(img, size)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise IconError() from exc

def save_cover(source_path: str | Path, dest_path: str | Path, size: int = 512) -> Path:
    """Schritt C: a square collection cover (centre crop, RGB PNG). IconError before
    touching dest_path if the source isn't a readable image."""
    try:
        with Image.open(source_path) as img:
            square = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), (size, size), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise IconError() from exc
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    square.save(dest_path, format="PNG")
    return dest_path


_PLACEHOLDER_COLORS = [
    "#22D3EE", "#7C3AED", "#F97316", "#F43F5E", "#10B981", "#EAB308",
]


def crop_to_circle(image: Image.Image, size: int = ICON_SIZE) -> Image.Image:
    """Center-crop to a square, resize, and mask to a circle with alpha."""
    image = image.convert("RGBA")
    w, h = image.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.LANCZOS
    )

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(image, (0, 0), mask=mask)
    return out


def save_icon(source_path: str | Path, dest_path: str | Path, size: int = ICON_SIZE) -> Path:
    """Load an arbitrary image file, crop it to a circle, and save as PNG.
    Raises IconError (before touching dest_path) if the source isn't a readable image."""
    circular = load_circle(source_path, size)
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    circular.save(dest_path, format="PNG")
    return dest_path


def placeholder_icon(name: str, size: int = ICON_SIZE) -> Image.Image:
    """Generate a neutral circular placeholder with the sound's initials."""
    initials = "".join(w[0] for w in name.split()[:2]).upper() or "?"
    color_index = sum(ord(c) for c in name) % len(_PLACEHOLDER_COLORS)
    bg_color = _PLACEHOLDER_COLORS[color_index]

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=bg_color)

    try:
        font = ImageFont.truetype("segoeui.ttf", size=size // 2)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
        initials,
        font=font,
        fill=BG,
    )
    return img
