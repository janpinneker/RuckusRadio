"""Tuner banner rendering (pure Pillow): gradient dial, FM scale, needle, wordmark."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from soundboard.theme import (
    ACCENT_INK,
    DISPLAY_FILES,
    GRAD_FROM,
    GRAD_TO,
    MONO_FILES,
    SIZE_WORDMARK,
    hex_to_rgb,
)

FREQ_MIN = 88.0
FREQ_MAX = 108.0
TICKS = 51  # 0.4 MHz steps -> every 5th tick lands on an even MHz
SIDE_PAD = 28
WORDMARK = "RUCKUS RADIO"
INK_ALPHA = 178  # ~70 %


@lru_cache(maxsize=None)
def _font(files: tuple[str, ...], size: int, variation: str | None = None):
    for name in files:
        try:
            font = ImageFont.truetype(name, size=size)
        except OSError:
            continue
        if variation:
            try:
                font.set_variation_by_name(variation)
            except (OSError, ValueError):
                pass
        return font
    return ImageFont.load_default()


def needle_x(width: int, needle: float) -> int:
    """Pixel column of the needle; the dial spans the width minus side padding."""
    needle = min(max(float(needle), 0.0), 1.0)
    pad = SIDE_PAD if width > SIDE_PAD * 4 else 0
    return round(pad + needle * (width - 1 - 2 * pad))


def _gradient(width: int, height: int) -> Image.Image:
    a, b = hex_to_rgb(GRAD_FROM), hex_to_rgb(GRAD_TO)
    strip = Image.new("RGB", (256, 1))
    strip.putdata([tuple(round(a[c] + (b[c] - a[c]) * i / 255) for c in range(3)) for i in range(256)])
    img = strip.resize((width, height), Image.BILINEAR)
    shade = Image.linear_gradient("L").resize((width, height), Image.BILINEAR)
    shade = shade.point(lambda v: max(0, v - 110) * 2 // 5)  # darken lower part, up to ~23 %
    img.paste((0, 0, 0), (0, 0), shade)
    return img


def _draw_scale(layer: Image.Image, width: int, height: int) -> None:
    draw = ImageDraw.Draw(layer)
    ink = hex_to_rgb(ACCENT_INK) + (INK_ALPHA,)
    base = height - 22
    font = _font(MONO_FILES, 11)
    major_gap = (width - 2 * SIDE_PAD) / (TICKS - 1) * 5
    label_every = 1 if major_gap >= 34 else 2
    for i in range(TICKS):
        x = needle_x(width, i / (TICKS - 1))
        major = i % 5 == 0
        draw.line([(x, base - (14 if major else 7)), (x, base)], fill=ink, width=1)
        if major and (i // 5) % label_every == 0:
            label = f"{FREQ_MIN + (FREQ_MAX - FREQ_MIN) * i / (TICKS - 1):.0f}"
            draw.text((x, base + 4), label, font=font, fill=ink, anchor="mt")
    draw.line([(needle_x(width, 0), base), (needle_x(width, 1), base)], fill=ink, width=1)


def _draw_wordmark(layer: Image.Image) -> None:
    draw = ImageDraw.Draw(layer)
    font = _font(DISPLAY_FILES, SIZE_WORDMARK, "Bold")
    ink = hex_to_rgb(ACCENT_INK) + (255,)
    x = SIDE_PAD
    for ch in WORDMARK:
        draw.text((x, 16), ch, font=font, fill=ink)
        x += draw.textlength(ch, font=font) + 2


def _draw_needle(img: Image.Image, x: int, height: int) -> None:
    top, bottom = (4, height - 5) if height > 10 else (0, height - 1)
    glow_w = 32
    glow = Image.new("L", (glow_w, height), 0)
    ImageDraw.Draw(glow).rectangle([glow_w // 2 - 3, top, glow_w // 2 + 2, bottom], fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    img.paste((255, 255, 255), (x - glow_w // 2, 0), glow)
    ImageDraw.Draw(img).rectangle([max(0, x - 1), top, x, bottom], fill=(255, 255, 255))


def render_tuner(width: int, height: int, needle: float, wordmark: bool = True,
                 scale: bool = True) -> Image.Image:
    """wordmark=False gives the compact strip (scale + needle, fits from 44px high)."""
    width, height = max(1, int(width)), max(1, int(height))
    img = _gradient(width, height)
    if width < 120 or height < (60 if wordmark else 44) or not (scale or wordmark):
        _draw_needle(img, needle_x(width, needle), height)
        return img
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if scale:
        _draw_scale(overlay, width, height)
    if wordmark:
        _draw_wordmark(overlay)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    _draw_needle(img, needle_x(width, needle), height)
    return img
