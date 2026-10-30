"""Link-preview images (Open Graph / X cards) for the site's pages.

Screenshots use a 4:3 display aspect regardless of their native capture size.
Each card fits the complete image into a 1200x630 canvas, with side padding:
whole-pixel nearest-neighbour first, then one smooth step to the corrected
size, on the site's background colour. A page with no image of its own gets
a 2x2 mosaic whose cells also display at 4:3.

Cards are named by a hash of their sources and rendering recipe, so an
unchanged page keeps its card (and preview caches stay valid) while a changed
screenshot or recipe gets a new URL. Needs Pillow; build.py only imports this module when a card is missing.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

WIDTH, HEIGHT = 1200, 630
BACKGROUND = (0x10, 0x10, 0x14)  # --bg in style.css


def card_name(key: str, sources: list[Path]) -> str:
    digest = hashlib.sha256(b"social-card-4x3-v1")
    for src in sources:
        digest.update(src.read_bytes())
    return f"{key}-{digest.hexdigest()[:10]}.png"


def _fit(image, width, height):
    from PIL import Image

    display_ratio = 4 / 3
    fitted_height = min(height, width / display_ratio)
    size = (round(fitted_height * display_ratio), round(fitted_height))
    scale = max(size[0] / image.width, size[1] / image.height)
    whole = max(1, math.ceil(scale))
    big = image.resize((image.width * whole, image.height * whole), Image.NEAREST)
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
