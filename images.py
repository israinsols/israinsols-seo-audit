"""Create a clean, accessible 1200x630 featured image using Pillow."""

from __future__ import annotations

import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 630


@dataclass(frozen=True)
class FeaturedImage:
    path: Path
    alt_text: str


def _color(name: str, default: str) -> tuple[int, int, int]:
    value = os.getenv(name, default).strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"{name} must be a six-digit hex color (for example 123ABC).")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
        if bold else ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_featured_image(
    title: str,
    output_path: Path | str,
    *,
    brand_name: str = "IsrainSols",
) -> FeaturedImage:
    """Render a branded title card and return the image path and descriptive alt text."""
    clean_title = " ".join(title.split())
    if not clean_title:
        raise ValueError("Image title cannot be empty.")
    primary = _color("BRAND_PRIMARY", "14213D")
    accent = _color("BRAND_ACCENT", "FCA311")
    background = _color("BRAND_BACKGROUND", "F7F9FC")
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((56, 56, WIDTH - 56, HEIGHT - 56), radius=28, fill=primary)
    draw.rounded_rectangle((96, 110, 108, HEIGHT - 110), radius=6, fill=accent)

    font_size = 66
    font = _font(font_size, bold=True)
    lines = textwrap.wrap(clean_title, width=29) or [clean_title]
    while len(lines) > 4 and font_size > 42:
        font_size -= 4
        font = _font(font_size, bold=True)
        lines = textwrap.wrap(clean_title, width=max(25, int(29 * 66 / font_size)))
    line_height = font_size + 20
    y = (HEIGHT - line_height * len(lines)) // 2 - 12
    for line in lines:
        draw.text((150, y), line, font=font, fill="white")
        y += line_height
    draw.text((150, HEIGHT - 142), brand_name, font=_font(28, bold=True), fill=accent)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return FeaturedImage(path=path, alt_text=f"Featured image: {clean_title}")

