"""Draw the site icon: an arcade joystick, from one pixel grid.

The PNG, ICO and SVG are all emitted from the grid below, so the scalable
icon and the bitmap ones cannot drift apart.  Run this only to change the
art; the results are committed under site/.
"""
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "site"

# 16x16, one character per pixel.  '#' is the icon's own background, so the
# dark plate still reads on a light browser tab; the four corners are cut
# to transparent for a slightly rounded tile.
GRID = [
    ".##############.",
    "################",
    "######BBBB######",
    "#####BBBBBB#####",
    "#####BBBBBB#####",
    "#####BBBBBB#####",
    "######BBBB######",
    "#######SS#######",
    "#######SS#######",
    "#######SS#######",
    "#######SS#######",
    "###PPPPPPPPPP###",
    "##PPPPPPPPPPPP##",
    "##DDDDDDDDDDDD##",
    "################",
    ".##############.",
]
COLORS = {
    "#": (20, 20, 26, 255),     # tile
    "B": (224, 86, 79, 255),    # ball
    "S": (154, 160, 173, 255),  # shaft
    "P": (74, 74, 85, 255),     # plate
    "D": (35, 35, 43, 255),     # plate shadow
    ".": (0, 0, 0, 0),
}


def base() -> Image.Image:
    im = Image.new("RGBA", (16, 16))
    im.putdata([COLORS[c] for row in GRID for c in row])
    return im


def svg() -> str:
    rects = []
    for y, row in enumerate(GRID):
        x = 0
        while x < len(row):
            c = row[x]
            run = 1
            while x + run < len(row) and row[x + run] == c:
                run += 1
            if c != ".":
                r, g, b, _ = COLORS[c]
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" '
                             f'fill="#{r:02x}{g:02x}{b:02x}"/>')
            x += run
    body = "\n  ".join(rects)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
            'shape-rendering="crispEdges">\n  ' + body + "\n</svg>\n")


def main() -> None:
    im = base()
    (OUT / "favicon.svg").write_text(svg())
    im.resize((180, 180), Image.NEAREST).save(OUT / "apple-touch-icon.png")
    im.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"wrote favicon.svg, favicon.ico and apple-touch-icon.png into {OUT}")


if __name__ == "__main__":
    main()
