"""Link-preview images (Open Graph / X cards) for the site's pages.

Screenshots are native arcade resolution (384x224), which link previews would
blur. Each card is the page's own image scaled to fill a 1200x630 canvas:
whole-pixel nearest-neighbour first, so the pixel art stays crisp, then one
smooth step to the exact size, on the site's background colour. A page with
no image of its own gets a 2x2 mosaic of other pages' images.

Cards are named by a hash of their sources, so an unchanged page keeps its
card (and preview caches stay valid) while a changed screenshot gets a new
URL. Needs Pillow; build.py only imports this module when a card is missing.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

WIDTH, HEIGHT = 1200, 630
BACKGROUND = (0x10, 0x10, 0x14)  # --bg in style.css


def card_name(key: str, sources: list[Path]) -> str:
    digest = hashlib.sha256()
    for src in sources:
        digest.update(src.read_bytes())
    return f"{key}-{digest.hexdigest()[:10]}.png"


def _fit(image, width, height):
    from PIL import Image

    scale = min(width / image.width, height / image.height)
    whole = max(1, math.ceil(scale))
    big = image.resize((image.width * whole, image.height * whole), Image.NEAREST)
    size = (round(image.width * scale), round(image.height * scale))
    return big if big.size == size else big.resize(size, Image.LANCZOS)


def make_card(sources: list[Path], dest: Path) -> None:
    from PIL import Image

    images = [Image.open(s).convert("RGB") for s in sources]
    if len(images) == 1:
        art = images[0]
    else:  # mosaic: first four, each cell the size of the first
        w, h = images[0].size
        art = Image.new("RGB", (w * 2, h * 2), BACKGROUND)
        for i, im in enumerate(images[:4]):
            art.paste(im.resize((w, h), Image.NEAREST), ((i % 2) * w, (i // 2) * h))
    fitted = _fit(art, WIDTH, HEIGHT)
    card = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    card.paste(fitted, ((WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2))
    dest.parent.mkdir(parents=True, exist_ok=True)
    card.save(dest, optimize=True)
