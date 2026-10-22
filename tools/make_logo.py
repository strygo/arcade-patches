"""Cut the site's logo assets from the AP emblem.

The emblem is a neon ring on black.  The header logo and the icons are all
cut from it here, so they cannot drift apart.  The ring is masked to a
circle so the logo sits cleanly on both the dark and the light theme.  Run
this only to change the art; the results are committed under site/.

    python3 tools/make_logo.py    # reads site/src/ap-emblem.png
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "site"
BLACK = (0, 0, 0)


def circle(src: Image.Image, size: int) -> Image.Image:
    """The emblem cut to its circle (the ring sits inside the black square)."""
    scale = 4  # supersample the mask for a smooth edge
    mask = Image.new("L", (src.width * scale,) * 2, 0)
    pad = src.width * 0.035 * scale
    ImageDraw.Draw(mask).ellipse([pad, pad, mask.width - pad, mask.height - pad], fill=255)
    out = src.convert("RGBA")
    out.putalpha(mask.resize(src.size, Image.LANCZOS))
    return out.resize((size, size), Image.LANCZOS)


def main(path: str) -> None:
    src = Image.open(path).convert("RGB")
    assert src.width == src.height, "the emblem is square"
    circle(src, 160).save(OUT / "logo.png", optimize=True)
    circle(src, 64).save(OUT / "favicon.png", optimize=True)
    circle(src, 64).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    # iOS rounds and fills the icon itself: give it the full black square.
    src.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png", optimize=True)
    print(f"wrote logo.png, favicon.png, favicon.ico and apple-touch-icon.png into {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(OUT / "src" / "ap-emblem.png"))
