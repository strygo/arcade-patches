#!/usr/bin/env python3
"""Vertical-pan converter: one TALL panorama + scroll2 Y, not 400 cels.

The opening's Mad Gear lineup already ships this way -- convert.py
stitches a wide panorama, paints each column into the map as it enters
view, and sweeps `sx` (512 -> 1323 over ~540 events).  That path is
horizontal only: it measures dx, builds a 224 x (Wc*16) canvas, and never
touches sy.

CD-6 (the Cody/Jessica farewell) is the vertical mirror -- the camera
tilts 280 px down from their faces to their feet.  Converted as cels it
cost 2.5 MB of tiles, because a 1 px/frame pan makes every tile new every
frame.  Converted as a panorama it is one 320 x ~504 image plus a sy ramp.

Deliberately NOT inherited from the horizontal path: its lineup-specific
rules (truncate-at-last-bright, CD deceleration-tail handling, per-run
brightness ramps).  Those are right for the gang panel and wrong here --
this scene is uniformly lit and must not be trimmed.

Output is the standard conv directory (tiles/palblocks/deltas/script +
manifest), so rom.py consumes it exactly like any other.

Usage:
    convert_pan.py <snapdir> <outdir> --window A-B --base-code 0x80cf
                     [--pan A2-B2]   # frames that actually move (default: auto)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert import quant12  # noqa: E402

CELL = 16
COLS, ROWS = 20, 14          # visible window, in cells
ROW0, COL0 = 0x11, 0x26      # where the view sits in the 64x64 map
SX0, SY0 = 0x200, 0x100
ART_TOP, ART_BOT = 32, 160   # the CD's letterbox window in a source frame


def cell_off(col: int, row: int) -> int:
    col &= 0x3F
    return ((row & 0x0F) + (col << 4) + ((row & 0x30) << 6)) * 4


def reduce15w(counts: dict) -> set[int]:
    """Pick <=15 colours by PIXEL COUNT, not by merging nearest pairs.

    Pairwise nearest-merge invents averaged colours that match no real
    pixel, so a gradient crossing a cell loses the shade it actually needs
    and adjacent pixels snap to two different neighbours -- rendering as a
    checkerboard (seen on Jessica's leg where it meets the heel).  A CPS1
    16x16 cell carries ONE palette while the Mega CD source tiles are
    8x8, so cells over 15 colours are unavoidable here; keeping the most
    common shades and mapping the rest to their nearest is the honest
    approximation.
    """
    if len(counts) <= 15:
        return set(counts)
    return {c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:15]}


def reduce15(cols: set[int]) -> set[int]:
    """Merge nearest colours until <=15 (transparent pen takes the 16th)."""
    if len(cols) <= 15:
        return cols
    cl = sorted(cols)
    while len(cl) > 15:
        best = None
        for x in range(len(cl)):
            for y in range(x + 1, len(cl)):
                a, b = cl[x], cl[y]
                d = (((a >> 8) - (b >> 8)) ** 2
                     + ((((a >> 4) & 15) - ((b >> 4) & 15)) ** 2)
                     + ((a & 15) - (b & 15)) ** 2)
                if best is None or d < best[0]:
                    best = (d, x, y)
        _, x, y = best
        a, b = cl[x], cl[y]
        m = ((((a >> 8) + (b >> 8)) // 2) << 8
             | ((((a >> 4) & 15) + ((b >> 4) & 15)) // 2) << 4
             | (((a & 15) + (b & 15)) // 2))
        cl.pop(y)
        cl[x] = m
        cl = sorted(set(cl))
    return set(cl)


def shift_y(a: np.ndarray, b: np.ndarray, span: int = 24) -> int:
    """Vertical offset (px) taking image a to image b."""
    ga = a.astype(np.float32).mean(axis=2)
    gb = b.astype(np.float32).mean(axis=2)
    best = (1e18, 0)
    for dy in range(0, span + 1):
        x = ga[dy:] if dy else ga
        y = gb[:len(gb) - dy] if dy else gb
        n = min(len(x), len(y))
        if n < 80:
            continue
        d = float(np.abs(x[:n] - y[:n]).mean())
        if d < best[0]:
            best = (d, dy)
    return best[1]


OPT = {"no_pan_patches": False, "predict": None,
       "trace_cell": None, "trace_runs": "0-60"}


def main() -> int:
    ap = argparse.ArgumentParser()
    # These four were FFCD_* environment variables.  As flags they cannot be
    # set by accident -- --no-pan-patches in particular drops part of the
    # shipped encoding, which is not something a shell variable should do.
    ap.add_argument("--no-pan-patches", action="store_true",
                    help="drop the OBJ patch layer (A/B measurement only; it "
                         "is part of the shipped encoding)")
    ap.add_argument("--predict", type=Path, default=None,
                    help="write the predicted composite to this PNG")
    ap.add_argument("--trace-cell", default=None,
                    help="trace one cell, as 'event,col'")
    ap.add_argument("--trace-runs", default="0-60",
                    help="run range for --trace-cell (default 0-60)")
    ap.add_argument("snapdir", type=Path)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--window", required=True)
    ap.add_argument("--base-code", default="0x4000")
    ap.add_argument("--crop", default="320x224")
    ap.add_argument("--anchors", default="",
                    help="lip-sync warp: 'src:eng,src:eng,...' mapping source "
                         "scene frames to engine frames.  The source animation "
                         "follows the JAPANESE dialogue, so each measured mouth "
                         "block is pinned onto its US voice span and the script "
                         "is re-timed piecewise-linearly between anchors.")
    ap.add_argument("--pano-from", type=Path, default=None,
                    help="build the PANORAMA from this frame set "
                         "(the scene's background layer, plane A blanked and "
                         "sprites parked) while every patch decision is made "
                         "against `snapdir`'s full frames.  That is the "
                         "layered build review asked for: the background is a "
                         "rigid pan the panorama reproduces exactly (208 "
                         "tiles, ONE palette), and the characters and shadow "
                         "-- which differ from it -- fall out as sprite "
                         "patches with their own OBJ palettes, off the "
                         "plane's 32.  Flattened, they instead ate the plane "
                         "budget and the shadow rendered in a skin palette.")
    ap.add_argument("--obj-from", type=Path, default=None,
                    help="take the OBJ cells from these ISOLATED "
                         "character frames (RGBA, alpha 0 where the character "
                         "is absent) instead of from the composited frame.  "
                         "Review: 'why aren't you using Jessica as standalone "
                         "sprites rather than including scenery around her'.  "
                         "A composited cell forces ONE 14-colour palette to "
                         "hold skin, satin, grass and sand together, which is "
                         "the white block at her mouth and the wrong palette "
                         "either side of her heel.  Isolated, the whole "
                         "character layer is 46 COLOURS -- about four "
                         "palettes -- and the background shows through pen 0 "
                         "from the panorama beneath, untouched.")
    ap.add_argument("--camera-from", type=Path, default=None,
                    help="take the pan's scroll ramp from this "
                         "CD-SOURCED table (the VM's own plane-B vscroll, "
                         "unwrapped) instead of correlating frames.  The "
                         "correlated ramp is OUR metadata and it is a pixel "
                         "off during motion, which is the whole of the "
                         "residual: the difference image is edges, not "
                         "areas.  The camera is something the disc TELLS us, "
                         "so it should be read, not inferred.")
    ap.add_argument("--pano-cap", type=int, default=27,
                    help="how many of the 32 palettes the PANORAMA may take "
                         "before the animation gets any")
    ap.add_argument("--anim-objects", action="store_true",
                    help="carry ANIMATING cells as gated OBJ "
                         "patches instead of plane refreshes.  The figures "
                         "stay in the panorama -- plane A scrolls rigidly "
                         "with plane B (vscrollA == vscrollB all scene), so "
                         "only the ~167 cells that actually change need to "
                         "leave.  On OBJ they carry their own palette from a "
                         "SEPARATE bank, which is what removes the plane "
                         "contention that miscoloured the ground under "
                         "Jessica's heel -- rather than merely making it "
                         "rarer.  Requires the end-gated patch record.")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip the animation-refresh pass entirely.  Refresh "
                         "exists for content that ANIMATES "
                         "while the camera holds.  Fed a LAYERED source -- "
                         "background only, characters carried as cels and "
                         "the shadow as sprites -- the pan is rigid and the "
                         "panorama plus the sy ramp is already exact, so "
                         "every refresh is correcting stitch error instead. "
                         "Measured on jp background-only frames: 2299 "
                         "refresh cells and 1694 tiles with it, against a "
                         "208-tile panorama without.")
    ap.add_argument("--hold-start", type=int, default=0,
                    help="extra frames to hold the opening cel before the "
                         "pan begins (the CD holds ~103; a longer hold keeps "
                         "Cody and Jessica framed together while she speaks)")
    args = ap.parse_args()
    OPT.update(no_pan_patches=args.no_pan_patches, predict=args.predict,
               trace_cell=args.trace_cell, trace_runs=args.trace_runs)

    BASE = int(args.base_code, 0)
    f0, f1 = (int(v) for v in args.window.split("-"))
    W, H = (int(v) for v in args.crop.split("x"))

    # ---- cel runs: consecutive identical frames collapse
    runs = []            # [hash, f_start, f_end]
    imgs: dict[str, np.ndarray] = {}
    prev = None
    for fr in range(f0, f1 + 1):
        p = args.snapdir / f"f{fr:06d}.png"
        if not p.exists():
            continue
        img = np.asarray(Image.open(p).convert("RGB"))[:H, :W]
        h = hashlib.md5(img.tobytes()).hexdigest()[:12]
        if h != prev:
            runs.append([h, fr, fr])
            imgs.setdefault(h, img)
            prev = h
        else:
            runs[-1][2] = fr
    print(f"{len(runs)} cel runs over {f1 - f0 + 1} frames")

    # ---- WHERE THE DISC'S CLOSING FADE STARTS.
    # The scene ends on a ~60-frame fade to black, and the slot has only
    # 10-20 frames of it: jp fades 8320-8380 against a window ending 8330,
    # us 6790-6850 against 6810.  Encoding those first two or four dim
    # steps is the worst of both -- not a fade, just the picture going
    # murky and then cutting -- and on the plane it is worse still,
    # because a dim step is a new palette fit, so the tail cells are
    # REQUANTISED: review saw the JP scene "change palettes at the end and
    # these transitions look bad", and asked for a straight cut to black.
    # So the fade is not encoded at all.  Runs from here on emit no
    # deltas and take no palette block: the picture holds at full
    # brightness for its slot time and `fin` cuts to black.
    # Detected, not configured: a fade is the maximal STRICTLY DECREASING
    # suffix of the runs' art-band means.  Keying on the frame MAX instead
    # (255 -> 206 -> 172) looked simpler and missed the CD's first dim
    # step, which lowers the mean while the max stays 255 -- two frames of
    # murk survived into the build.  Measured, both regions plateau at
    # 158.766 and then run the identical ramp (157.749, 157.298, 156.627,
    # 155.500, ...), so this finds jp run 357 / us run 353.
    means = [float(imgs[h][ART_TOP:ART_BOT].mean()) for h, _f, _e in runs]
    k_fade = len(runs)
    while k_fade > 1 and len(runs) - k_fade < 40 \
            and means[k_fade - 1] < means[k_fade - 2]:
        k_fade -= 1
    # a real fade, not a scene that merely drifts darker
    if k_fade >= len(runs) or means[k_fade - 1] - means[-1] < 1.0:
        k_fade = len(runs)
    if k_fade < len(runs):
        print(f"  closing fade starts at run {k_fade} "
              f"(source frame {runs[k_fade][1]}): {len(runs) - k_fade} run(s) "
              f"held at full brightness, then a cut to black")
    else:
        k_fade = None

    # ---- the PANORAMA source (see --pano-from).  Keyed by a run's START
    # FRAME, not by the full frames' hash: with characters animating over a
    # still background the two sets do not share run boundaries.
    pano_src: dict[int, np.ndarray] | None = None
    if args.pano_from:
        pano_src = {}
        for h, fs, fe in runs:
            p = args.pano_from / f"f{fs:06d}.png"
            if not p.exists():
                raise SystemExit(f"--pano-from is missing {p.name}")
            pano_src[fs] = np.asarray(Image.open(p).convert("RGB"))[:H, :W]
        print(f"panorama from {args.pano_from.name} "
              f"({len(pano_src)} run heads)")

    obj_src: dict[int, np.ndarray] | None = None
    if args.obj_from:
        obj_src = {}
        for h, fs, fe in runs:
            q = args.obj_from / f"f{fs:06d}.png"
            if not q.exists():
                raise SystemExit(f"--obj-from is missing {q.name}")
            obj_src[fs] = np.asarray(Image.open(q).convert("RGBA"))[:H, :W]
        print(f"OBJ cells from {args.obj_from.name} "
              f"({len(obj_src)} run heads)")

    def pimg(k):
        """The panorama-source frame for run k (falls back to the full set)."""
        return pano_src[runs[k][1]] if pano_src else imgs[runs[k][0]]

    # ---- cumulative vertical offset per run.  Measured on the PANORAMA
    # source: the pan is a property of the background, and character
    # animation would otherwise perturb the correlation.
    cam = {}
    if args.camera_from:
        for line in args.camera_from.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            a_, b_ = line.split("\t")
            cam[int(a_)] = int(b_)
    if cam:
        base = cam[runs[0][1]]
        cum = [cam[fs] - base for _h, fs, _fe in runs]
        print(f"camera from {args.camera_from.name}: "
              f"{cum[0]}..{cum[-1]} px (CD-sourced, not correlated)")
    else:
        cum = [0]
        for k in range(len(runs) - 1):
            cum.append(cum[-1] + shift_y(pimg(k), pimg(k + 1)))
    travel = cum[-1]
    Hc = (H + travel + CELL - 1) // CELL
    print(f"vertical travel {travel} px -> panorama {W}x{Hc * CELL} "
          f"({COLS}x{Hc} cells)")
    if ROW0 + Hc > 64:
        raise SystemExit(f"panorama {Hc} rows overflows the 64-row map "
                         f"at ROW0={ROW0:#x}")

    # ---- stitch: each panorama ROW band comes from the frame where it sits
    # nearest screen centre (sourcing bands from independently-chosen frames
    # makes adjacent bands come from different instants, which reads as
    # seams).
    pano = np.zeros((Hc * CELL, W, 3), np.uint8)
    for i in range(Hc):
        target = i * CELL - (H // 2 - CELL // 2)
        best, bestd = None, None
        for k in range(len(runs)):
            y0 = i * CELL - cum[k]
            if y0 < 0 or y0 > H - CELL:
                continue
            d = abs(cum[k] - target)
            if bestd is None or d < bestd:
                bestd, best = d, (k, y0)
        if best is not None:
            k2, y2 = best
            pano[i * CELL:(i + 1) * CELL] = pimg(k2)[y2:y2 + CELL]

    # ---- re-anchor each run against the PANORAMA.
    # cum[] is measured frame-to-frame, so its error accumulates over 355
    # steps.  Refresh samples the live frame at `i*CELL - cum`, so drift
    # makes it sample shifted content and repaint cells with a smear --
    # seen as stray red pixels on a tree trunk that persisted through the
    # whole tail.  Matching each frame back to the finished panorama makes
    # every offset absolute.
    fixed = 0
    for k, (h, _fs, _fe) in enumerate(runs):
        # against the PANORAMA SOURCE: matching a frame that carries the
        # characters back to a background-only panorama would fit the
        # figures' rows as if they were terrain and pull the anchor off.
        f = pimg(k).astype(np.int16)
        best = (None, None)
        for cand in range(max(0, cum[k] - 4), cum[k] + 5):
            if cand + H > pano.shape[0]:
                continue
            # compare only the art window, where both are real content
            a = f[ART_TOP:ART_BOT]
            b = pano[cand + ART_TOP:cand + ART_BOT].astype(np.int16)
            if a.shape != b.shape:
                continue
            d = float(np.abs(a - b).mean())
            if best[0] is None or d < best[0]:
                best = (d, cand)
        if best[1] is not None and best[1] != cum[k]:
            fixed += 1
            cum[k] = best[1]
    print(f"re-anchored {fixed}/{len(runs)} runs against the panorama")
    # travel was measured on the pre-anchor offsets; the last run may have
    # moved, and the SETTLED scroll is what the tail patches key off.  Using
    # the stale value sampled the source a few rows out, so a patch was built
    # from misaligned art AND the residual it was compared against was
    # inflated -- the gate then accepted patches over cells the plane was
    # already rendering well, which is what read as a stripe down Cody's leg.
    travel = cum[-1]

    # ---- palettes over the panorama's cells
    q = quant12(pano)
    sets = []
    for cy in range(Hc):
        for cx in range(COLS):
            cell = q[cy * CELL:(cy + 1) * CELL, cx * CELL:(cx + 1) * CELL]
            cnt = {}
            for r, g, b in cell.reshape(-1, 3):
                k = (int(r) << 8) | (int(g) << 4) | int(b)
                cnt[k] = cnt.get(k, 0) + 1
            sets.append(reduce15w(cnt))
    order = sorted(range(len(sets)), key=lambda i: -len(sets[i]))
    pals: list[set[int]] = []
    for i in order:
        for pal in pals:
            if len(pal | sets[i]) <= 15:
                pal |= sets[i]
                break
        else:
            pals.append(set(sets[i]))

    def pal_dist(a, b):
        return min(((x >> 8) - (y >> 8)) ** 2
                   + (((x >> 4) & 15) - ((y >> 4) & 15)) ** 2
                   + ((x & 15) - (y & 15)) ** 2 for x in a for y in b)

    # Cap the PANORAMA's own palette use below the hardware's 32 so the
    # animated cells can still claim dedicated ones.  Letting the panorama
    # take all 32 (it wanted 27, and refresh claims filled the rest)
    # exhausted the block, and every later animated cell was stuck with a
    # mediocre fit -- Jessica's ankle rendered as a checkerboard.  The
    # static background tolerates a merged palette far better than moving
    # content does.
    # the panorama's share of the 32 palettes is a CHOICE, not
    # a constant.  At 27 it reserves almost everything before the animation
    # is considered, leaving 5 for the 4,611 cells that want one -- which is
    # every palette defect in this scene (the white block at Jessica's mouth,
    # the wrong palette either side of her heel).  The panorama is one large
    # static image and merges cheaply; the animating cells are small and
    # specific and cannot.  Sweep --pano-cap to see the trade instead of
    # asserting it.
    PANO_CAP = args.pano_cap
    while len(pals) > PANO_CAP:
        best = None
        for x in range(len(pals)):
            for y in range(x + 1, len(pals)):
                d = pal_dist(pals[x], pals[y])
                if best is None or d < best[0]:
                    best = (d, x, y)
        _, x, y = best
        merged = pals[x] | pals[y]
        if len(merged) > 15:
            merged = reduce15(merged)
        pals.pop(y)
        pals[x] = merged
    # assign each cell its palette by RENDERED ERROR, not by
    # colour-set overlap.  Overlap is an exact-match score, so a cell whose
    # colours are near-misses in every palette scores ~0 everywhere and takes
    # whichever came first -- for the cell holding Jessica's lip against
    # Cody's white shirt that was a palette with neither, and it rendered
    # FLAT WHITE from the panorama's first paint (the block reported in review
    # beside her mouth, which no downstream sprite pass could reach because
    # the cell was never a refresh candidate).  The refresh path already
    # learned this -- "that is how the shoe came out dark" -- and the lesson
    # simply was never applied to the panorama itself.
    def _fit(cy, cx, pi):
        cell = q[cy * CELL:(cy + 1) * CELL, cx * CELL:(cx + 1) * CELL]
        pl = sorted(pals[pi])
        if not pl:
            return 1e9
        arr = np.array([[(c >> 8) & 15, (c >> 4) & 15, c & 15] for c in pl],
                       dtype=np.int16)
        flat = cell.reshape(-1, 3).astype(np.int16)
        d = np.abs(flat[:, None, :] - arr[None, :, :]).sum(axis=2)
        return float(d.min(axis=1).mean())

    assign = []
    for n, cs in enumerate(sets):
        cy, cx = divmod(n, COLS)
        cands = sorted(range(len(pals)),
                       key=lambda pi: -(len(cs & pals[pi])
                                        - 0.01 * len(pals[pi])))[:6]
        assign.append(min(cands, key=lambda pi: _fit(cy, cx, pi)))
    pal_order = [sorted(p) for p in pals]
    lut = [{c: i for i, c in enumerate(pl)} for pl in pal_order]
    print(f"{len(pals)} palettes")

    # ---- tiles
    tiles: dict[bytes, int] = {b"\xff" * 128: 0}
    cells = [[None] * COLS for _ in range(Hc)]
    for cy in range(Hc):
        for cx in range(COLS):
            pi = assign[cy * COLS + cx]
            pal = pal_order[pi]
            cell = q[cy * CELL:(cy + 1) * CELL, cx * CELL:(cx + 1) * CELL]
            pens = np.zeros((CELL, CELL), dtype=np.uint8)
            for y in range(CELL):
                for x in range(CELL):
                    r, g, b = (int(v) for v in cell[y, x])
                    c = (r << 8) | (g << 4) | b
                    if c not in lut[pi]:
                        c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                                + (((pc >> 4) & 15) - g) ** 2
                                + ((pc & 15) - b) ** 2)
                    pens[y, x] = lut[pi][c]
            ch = bytearray(128)
            for y in range(CELL):
                for kx in range(8):
                    ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                                      | int(pens[y, 2 * kx + 1]))
            ch = bytes(ch)
            if ch not in tiles:
                tiles[ch] = len(tiles)
            cells[cy][cx] = (BASE + tiles[ch], pi)
    print(f"{len(tiles)} unique tiles ({len(tiles) * 128:,} B)")

    _pen_cache: dict = {}

    def _pen_table(pi: int):
        """(colour -> pen) for all 4096 12-bit colours, plus the pen -> RGB.

        `tile_of` was 42% of the whole two-region build (128.8 s, 68.1 M
        pixel iterations through a Python nearest-colour search).  It is a
        pure function of the palette, so the search collapses to one table
        built per distinct palette and reused.

        Equivalence with the loop it replaces is exact, not approximate:
          * colours the palette HOLDS are seeded straight from lut[pi], so
            they cannot drift even if a palette ever held a duplicate;
          * colours it does not hold take np.argmin over the same squared
            distance, and argmin returns the FIRST minimum exactly as
            min() did -- which matters, because ties are common (7.9% of
            cases) and picking a different tie changes output bytes.

        Keyed on the palette's CONTENTS, not its index: pal_order/lut are
        rebuilt between the two passes and appended to when a spare is
        claimed, so an index-keyed cache would serve a stale table.
        """
        pal = pal_order[pi]
        key = (pi, tuple(pal))
        hit = _pen_cache.get(key)
        if hit is not None:
            return hit
        arr = np.asarray(pal, dtype=np.int32)
        cs = np.arange(4096, dtype=np.int32)
        d = ((((cs >> 8) & 15)[:, None] - ((arr >> 8) & 15)[None, :]) ** 2
             + (((cs >> 4) & 15)[:, None] - ((arr >> 4) & 15)[None, :]) ** 2
             + ((cs & 15)[:, None] - (arr & 15)[None, :]) ** 2)
        table = np.argmin(d, axis=1).astype(np.uint8)
        for c, pen in lut[pi].items():
            table[c] = pen
        palrgb = np.stack([((arr >> 8) & 15) * 17, ((arr >> 4) & 15) * 17,
                           (arr & 15) * 17], axis=1).astype(np.uint8)
        _pen_cache[key] = (table, palrgb)
        return table, palrgb

    def tile_of(cell_rgb: np.ndarray, pi: int):
        """Quantised 16x16 -> (chunky tile, rendered RGB) using palette pi."""
        table, palrgb = _pen_table(pi)
        cq = quant12(cell_rgb).astype(np.int32)
        pens = table[(cq[..., 0] << 8) | (cq[..., 1] << 4) | cq[..., 2]]
        # chunky: two pens per byte, high nibble = even x, row-major
        ch = ((pens[:, 0::2] << 4) | pens[:, 1::2]).astype(np.uint8).tobytes()
        return ch, palrgb[pens]

    # ---- deltas + script.
    # Each panorama row is painted once as it enters view -- but content
    # that ANIMATES after that (Jessica's feet and their shadow shifting at
    # the end of this scene; the horizontal path sacrificed exactly this for
    # the lineup, "El Gado's arm gesture", ) must still update.  So
    # every run also REFRESHES the cells whose live frame has drifted from
    # what is currently on screen, above a perceptual threshold that keeps
    # quantisation noise from repainting the whole screen.
    # The spare palettes are a FIXED budget (32 minus the panorama's cap) and
    # were handed out first-come: the first refreshed cells to miss by more
    # than the threshold took them all, and every later cell -- however bad --
    # was stuck with the closest existing palette.  Jessica's heel settles at
    # the very end of the pan, so the cell holding her shoe AND the grey-green
    # ground arrived after the spares were gone and rendered the ground in
    # skin tones.  Rank first, then spend: pass 0 measures every candidate's
    # miss with panorama-only palettes, pass 1 gives the spares to the worst.
    _pal_base = ([set(p) for p in pals], [list(p) for p in pal_order],
                 [dict(l) for l in lut])
    _tiles_base = dict(tiles)
    SPARES = 32 - len(pals)
    rank_claims: list[tuple[float, int, int]] = []
    bad_cells: list = []      # (err, band, col, run, live)
    claim_allow: set[tuple[int, int]] | None = None
    for _pass in (0, 1):
        pals[:] = [set(p) for p in _pal_base[0]]
        pal_order[:] = [list(p) for p in _pal_base[1]]
        lut[:] = [dict(l) for l in _pal_base[2]]
        tiles.clear()
        tiles.update(_tiles_base)
        deltas = b""
        script = []
        painted: set[int] = set()
        anim: dict[tuple[int, int], list] = {}   # (band,col) -> [(run, rgb)]
        shown: dict[tuple[int, int], np.ndarray] = {}   # (band,col) -> rgb
        shown_pi: dict[tuple[int, int], int] = {}       # (band,col) -> palette
        tail_state: dict[int, tuple] = {}               # run -> shown snapshot
        refreshed = 0
        refreshed_cells = set()
        primed: set[int] = set()

        def _record_bad(i, k, frame, off, win_top, win_bot):
            """Log cells the PLANE gets wrong at a moment it will not repaint.

            The refresh loop returns early for a band on the event that
            paints it and for every event while the camera moves, so those
            moments produced no candidates at all.  Measuring costs a numpy
            diff against what is already on screen -- no requantisation --
            and the candidate list is the only route a sprite patch has to
            reach a mid-pan defect.
            """
            if _pass != 0:
                return
            y0 = i * CELL - off
            if y0 < 0 or y0 + CELL > frame.shape[0]:
                return
            if y0 < win_top or y0 + CELL > win_bot:
                return
            for cx in range(COLS):
                cur = shown.get((i, cx))
                if cur is None:
                    continue
                live = frame[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                if int(live.max()) < 12:
                    continue
                e = float(np.abs(cur.astype(int) - live.astype(int)).mean())
                if e >= 12.0:
                    bad_cells.append((e, i, cx, k, live.copy()))

        for k, (h, fs, fe) in enumerate(runs):
            sy = SY0 + cum[k]
            if k_fade is not None and k >= k_fade:
                # inside the disc's closing fade: hold the picture, encode
                # nothing.  See the k_fade note above.
                script.append((0, 0, len(deltas), 0x8000, fe - fs + 1,
                               SX0, sy))
                continue
            top = cum[k] // CELL                       # first row band in view
            bot = min(Hc - 1, (cum[k] + H - 1) // CELL)
            frame = imgs[h]
            # This frame's own art window.  The source is letterboxed and the
            # window CHANGES (32..159 during the pan, opening to 32..215 near
            # the end), so a fixed clamp is wrong both ways: too tight froze
            # the tail animation, too loose let a band STRADDLING the mask edge
            # be refreshed from half-black content -- its claimed palette was
            # then fitted on mostly black and the visible half rendered with
            # stray reds across Jessica's leg.
            lit = np.nonzero(frame.max(axis=(1, 2)) > 12)[0]
            win_top, win_bot = (int(lit[0]), int(lit[-1]) + 1) if len(lit) else (0, 0)
            chunk = b""
            painted_now: set[int] = set()
            for i in range(top, bot + 1):
                if i not in painted:
                    painted.add(i)
                    painted_now.add(i)
                    primed.discard(i)      # wants a live prime pass
                    # PAINT FROM THE LIVE FRAME WHEN THE CAMERA
                    # IS STILL.  The panorama band is stitched from the
                    # moment that band sits nearest screen centre -- a
                    # different moment -- so a band painted at the scene head
                    # starts wrong and is corrected later, whenever its prime
                    # look happens to land.  That correction is a re-fit, so
                    # it changes the cell's palette, and it lands at an
                    # arbitrary frame: measured in game, Jessica's mouth
                    # changed by 3.8 over two frames just as the pan began
                    # while THE DISC CHANGED BY 0.00 (the same window on a
                    # static background floors at 0.00, so that is real).
                    # If the camera is still, the live frame IS the right
                    # art for this band right now -- paint that, and there is
                    # nothing left to correct later.  While the camera moves
                    # the panorama still wins: a band entering mid-pan is
                    # stitch-matched to its neighbours, and correcting it
                    # belongs to the sprite passes.
                    _still = (k == 0 or cum[k] == cum[k - 1])
                    _y0 = i * CELL - cum[k]
                    _live_ok = (_still and _y0 >= 0
                                and _y0 + CELL <= frame.shape[0]
                                and _y0 >= win_top and _y0 + CELL <= win_bot)
                    for cx in range(COLS):
                        code, pi = cells[i][cx]
                        if _live_ok:
                            _lc = frame[_y0:_y0 + CELL,
                                        cx * CELL:(cx + 1) * CELL]
                            _ch, _rgb = tile_of(_lc, pi)
                            if _ch not in tiles:
                                tiles[_ch] = len(tiles)
                            code = BASE + tiles[_ch]
                        chunk += struct.pack(">3H", cell_off(COL0 + cx, ROW0 + i),
                                             code, pi)
                        # record what is ACTUALLY on screen -- the quantised
                        # tile, not the raw panorama.  Seeding this with the
                        # panorama made the refresh gate compare against a
                        # better-than-reality baseline and reject genuine
                        # improvements: where the shoe moved into a cell late,
                        # the update was refused and the shoe went missing.
                        shown[(i, cx)] = _rgb if _live_ok else tile_of(
                            pano[i * CELL:(i + 1) * CELL,
                                 cx * CELL:(cx + 1) * CELL], pi)[1]
                        shown_pi[(i, cx)] = pi
                    _record_bad(i, k, frame, cum[k], win_top, win_bot)
                    continue
                if k == 0 or cum[k] != cum[k - 1]:
                    # camera MOVING: trust the PLANE, do not refresh this
                    # band.  A refresh mid-pan re-fits the cell's palette (a
                    # colour change) from a sample taken at the new scroll (a
                    # 1 px shift), so one repaint delivers both symptoms at
                    # once -- "when the scene scrolls up, jessica's mouth
                    # shifts down by 1px and changes palettes".  In panorama
                    # coordinates (scroll removed) a mid-pan refresh moves the
                    # mouth region by 14.3, 8.8 and 10.1 over the scroll's
                    # first three frames while THE DISC CHANGES BY 0.00 -- the
                    # disc holds that mouth perfectly still.  A band's first
                    # live look belongs to a STILL camera.  Bands that enter
                    # view mid-pan are covered by the candidate recorder
                    # below: a sprite patch can correct them without touching
                    # the plane, which is exactly how the END card's reveal is
                    # carried.  Refreshing mid-pan repaints stitch mismatch
                    # rather than animation (cells flip states as the scroll
                    # advances, perceived as palettes changing) and costs 7422
                    # tiles instead of 380.
                    #
                    # but still MEASURE.  Leaving the plane rigid is right;
                    # leaving the cell UNRECORDED is not, and it hides a whole
                    # class of defect from every later pass.  A band that
                    # enters view during the pan is painted from the panorama,
                    # which is stitched from the moment that band sits nearest
                    # screen centre -- a LATER moment.  The END card is
                    # revealed a letter at a time, so the bands carrying it
                    # come in reading "END" while the disc still shows "E", two
                    # events early.  The sprite passes can fix that (an OBJ
                    # patch neither disturbs the plane nor spends a plane
                    # palette), so the cell must be recorded for them.
                    _record_bad(i, k, frame, cum[k], win_top, win_bot)
                    continue
                if args.no_refresh:
                    continue          # layered source: the pan is rigid
                y0 = i * CELL - cum[k]
                if y0 + CELL <= win_top or y0 >= win_bot:
                    continue          # wholly outside this frame's art window
                # a band can straddle the FRAME edge, not just the
                # art window -- `bot` is clamped to the panorama's last band,
                # which does not bound y0 against this frame's height.  The
                # slice then comes back SHORT (`live[yy]` -> IndexError, hit at
                # yy=12 on the jp VM window), and a negative y0 would silently
                # wrap in from the bottom.  Clamp, and composite only the rows
                # that really exist.
                ys, ye = max(0, y0), min(frame.shape[0], y0 + CELL)
                if ye <= ys:
                    continue
                whole = (ys == y0 and ye == y0 + CELL
                         and win_top <= y0 and y0 + CELL <= win_bot)
                for cx in range(COLS):
                    live = frame[ys:ye, cx * CELL:(cx + 1) * CELL]
                    cur = shown.get((i, cx))
                    if cur is None:
                        continue
                    # A band STRADDLING the letterbox edge is half black.  Do not
                    # skip it (that froze cells whose visible half really did
                    # change) and do not fit on it raw (that baked the mask edge
                    # in, and the palette claimed for it was mostly black --
                    # stray reds across Jessica's leg).  Composite: take the
                    # in-window rows from the live frame, keep the rest as shown.
                    if not whole:
                        # build a full cell from `cur`, overwriting only rows
                        # that are present in the frame AND inside the window
                        merged = cur.copy()
                        for yy in range(ys - y0, ye - y0):
                            if win_top <= y0 + yy < win_bot:
                                merged[yy] = live[yy - (ys - y0)]
                        live = merged
                    # PRIME: a band's first live look after it is painted always
                # gets compared, whatever the trigger says.  The panorama band
                # is stitched from the frame where it sits nearest screen
                # centre -- a LATER moment -- so at the scene's start a cell
                # can hold the wrong art from the outset and simply sit there
                # until some later change happens to trip the trigger.  That
                # is why Jessica's lips read blue for three seconds before
                # snapping to red.
                # Low trigger threshold on purpose: the QUALITY GATE below
                    # is the real arbiter (a refresh that does not improve the
                    # cell is rejected), so a tight threshold here only causes
                    # STALE cells -- Jessica's ankle kept frame 970's shading
                    # for the rest of the scene because the change never cleared
                    # a 3.0 bar.
                    # trigger on the WORST 4x4 BLOCK as well as the
                    # cell mean.  Jessica's lips are ~12 px of a 256 px cell,
                    # so a mouth movement is ~0.7 at cell level and fell under
                    # the 0.8 bar -- the source animates her mouth all through
                    # the opening speech (deltas 0.09-0.92) and the build
                    # showed 0.00.  A small bright change is exactly what the
                    # eye tracks, so it must not be averaged away.
                    _d = np.abs(live.astype(int) - cur.astype(int)).mean(axis=2)
                    _blk = float(_d.reshape(4, 4, 4, 4).mean(axis=(1, 3)).max())
                    _TR = OPT["trace_cell"]
                    _TRK = OPT["trace_runs"]
                    _TRA, _TRB = (int(v) for v in _TRK.split("-"))
                    if _TR and f"{i},{cx}" == _TR and _TRA <= k <= _TRB:
                        print(f"    c{k}: mean {_d.mean():.2f} blk {_blk:.2f}")
                    if (float(_d.mean()) <= 0.8 and _blk <= 1.2
                            and i in primed):
                        if _TR and f"{i},{cx}" == _TR and _TRA <= k <= _TRB:
                            print("        -> dropped: below trigger")
                        continue
                    # NEVER let the source's letterbox bars overwrite art: the
                    # frames are masked (art rows ~32..159, black outside), so a
                    # band sampled in a bar is uniformly black -- refreshing from
                    # it painted a corrupt black band mid-pan.  Guard on the
                    # CONTENT being black rather than on a fixed row window: at
                    # the tail the CD opens its window and the feet/shadow
                    # animation lives near the frame bottom, so a row clamp
                    # silently froze exactly the animation this pass exists for.
                    if int(live.max()) < 12:
                        continue
                    # Pick the BEST-FITTING palette for the LIVE content, not
                    # the one the panorama cell was assigned: animated pixels
                    # (Jessica's feet and their shadow at the tail) are new
                    # colours, and forcing them through the original cell's
                    # palette mis-quantises them -- visible as a palette glitch
                    # around her feet.  The attr word carries the palette per
                    # cell, so a refresh is free to change it.
                    lcols = {(int(r) << 8) | (int(g) << 4) | int(bb)
                             for r, g, bb in quant12(live).reshape(-1, 3)}
                    # Choose by ACTUAL RENDERED ERROR, not colour-set overlap:
                    # overlap is an exact-match score, so a palette holding
                    # near-miss shades scores zero.  That is how the shoe came
                    # out dark -- the bright reds it needed lived in a palette
                    # whose exact codes did not intersect the live cell's.
                    # Shortlist by overlap (cheap), then render and measure.
                    short = sorted(range(len(pals)),
                                   key=lambda q: -(len(lcols & pals[q])
                                                   - 0.01 * len(pals[q])))[:6]
                    pi, best_ch, best_rgb, best_err = None, None, None, None
                    for q in short:
                        c_ch, c_rgb = tile_of(live, q)
                        e = float(np.abs(c_rgb.astype(int) - live.astype(int)).mean())
                        if best_err is None or e < best_err:
                            pi, best_ch, best_rgb, best_err = q, c_ch, c_rgb, e
                    # If nothing fits, CLAIM A SPARE PALETTE.  The palettes were
                    # fitted on the panorama, so content that only appears later
                    # has no home: the END card is lavender and came out cream
                    # because the closest pan palette had no purple.  The block
                    # holds 32 and the pan needs ~27.
                    # Claim readily: the panorama fit leaves spare slots (27 of
                    # 32 used) and only a handful of animated cells ever need
                    # one.  A high bar leaves refreshed cells stuck with a
                    # mediocre palette -- Jessica's ankle renders as a
                    # checkerboard when the closest existing palette has no
                    # matching brown, at an error just under the threshold.
                    if best_err > 2.0 and _pass == 0:
                        # pass 0 only records the demand; nothing is claimed,
                        # so every candidate is measured on the same footing.
                        # Rank on the WORST 4x4 patch, not the cell mean: a
                        # small region that is completely wrong matters more
                        # than a whole cell that is slightly off, and the mean
                        # buries it.  Jessica's lips are ~12 px of a 256 px
                        # cell -- rendered with no red in the palette they read
                        # blue for three seconds, while the cell mean stayed
                        # low enough to lose every spare palette to duller
                        # but larger misses.
                        diff = np.abs(best_rgb.astype(int)
                                      - live.astype(int)).mean(axis=2)
                        worst = float(diff.reshape(4, 4, 4, 4).mean(axis=(1, 3)).max())
                        # the PLANE's spare palettes are ranked by
                        # worst PIXEL too.  A cell that renders flat white over
                        # Jessica's lip is only moderate in mean, so it lost
                        # every spare to duller but larger misses and stayed
                        # white from the panorama's first paint -- the block
                        # seen in review beside her mouth, which no amount of
                        # sprite-patching downstream could reach because the
                        # cell was never a refresh candidate.
                        _pxmax = float(np.abs(best_rgb.astype(int)
                                              - live.astype(int)).max())
                        rank_claims.append((max(best_err, worst, _pxmax / 3.0),
                                            i, cx))
                        if True:
                            # remember the WORST cells with the
                            # run they occur in.  A cell straddling two
                            # colour worlds (Jessica's lip against Cody's
                            # shirt; her heel against the ground) cannot be
                            # fitted in 15 entries NO MATTER how many spare
                            # palettes exist -- sweeping the panorama cap
                            # from 27 down to 18 moves the spares 5 -> 14 and
                            # leaves the worst miss at 38.2.  Those cells
                            # need a SPRITE, which is transparent over the
                            # boundary.  The settled-tail patch pass already
                            # does exactly this; it was just restricted to
                            # one moment.  With end gates (107r) it can cover
                            # them wherever they occur.
                            # Rank by the WORST PIXEL, not the cell mean.  A
                            # cell that renders flat white over Jessica's lip
                            # is catastrophic to look at but only moderate in
                            # mean, so it lost every spare palette to duller
                            # but larger misses -- review saw exactly that
                            # white block beside her mouth survive two
                            # rounds of this pass.
                            pxmax = float(np.abs(best_rgb.astype(int)
                                                 - live.astype(int)).max())
                            bad_cells.append((max(best_err, worst, pxmax / 3.0),
                                              i, cx, k, live.copy()))
                    if (best_err > 2.0 and _pass == 1
                            and (i, cx) in claim_allow and len(pals) < 32):
                        lcnt = {}
                        for r, g, bb in quant12(live).reshape(-1, 3):
                            kk = (int(r) << 8) | (int(g) << 4) | int(bb)
                            lcnt[kk] = lcnt.get(kk, 0) + 1
                        pals.append(reduce15w(lcnt))
                        pal_order.append(sorted(pals[-1]))
                        lut.append({c: n for n, c in enumerate(pal_order[-1])})
                        c_ch, c_rgb = tile_of(live, len(pals) - 1)
                        e = float(np.abs(c_rgb.astype(int) - live.astype(int)).mean())
                        if e < best_err:
                            pi, best_ch, best_rgb, best_err = len(pals) - 1, c_ch, c_rgb, e
                    ch, rgb = best_ch, best_rgb
                    # A refresh must IMPROVE the cell.  Repainting a background
                    # cell that only drifted by noise replaced the panorama's
                    # dithered ground with a flat block -- visible as a patch
                    # beside Jessica's shoe.  Keep the existing cell unless the
                    # new one is meaningfully closer to the live frame.
                    err_new = float(np.abs(rgb.astype(int) - live.astype(int)).mean())
                    err_cur = float(np.abs(cur.astype(int) - live.astype(int)).mean())
                    # The 0.95 bar keeps marginal REQUANTISATION out of the
                    # plane, where a repaint costs a palette.  An object costs
                    # neither, and rejecting a moving edge cell leaves the
                    # panorama's old art beside the new pose -- which is how
                    # Jessica's leg came apart as she lifts.  On the object
                    # path, any real improvement is taken.
                    # the 0.95 bar keeps marginal REQUANTISATION
                    # out, but it also rejected Jessica's mouth -- a lip
                    # movement does not make a 256 px cell 5% better, so her
                    # mouth never animated through the opening speech while
                    # the source animates it throughout.  Accept when the
                    # WORST BLOCK improves clearly, even if the cell mean
                    # barely moves.
                    bar = err_cur if args.anim_objects else err_cur * 0.95
                    _bn = float(np.abs(rgb.astype(int) - live.astype(int))
                                .mean(axis=2).reshape(4, 4, 4, 4)
                                .mean(axis=(1, 3)).max())
                    _bc = float(np.abs(cur.astype(int) - live.astype(int))
                                .mean(axis=2).reshape(4, 4, 4, 4)
                                .mean(axis=(1, 3)).max())
                    # A REFRESH MUST NOT MAKE ANY SMALL REGION
                    # WORSE.  The gate above only ever asked whether the cell
                    # got BETTER -- by mean, or in its worst block -- so a
                    # re-fit that lowered the cell mean while wrecking a
                    # corner sailed through.  That is what review saw as the
                    # pan began: cell (6,12) holds Jessica's chin against her
                    # hair and the sky, and its repaint rendered the jawline
                    # in mottled blue and pink.  The cell mean improved; the
                    # part anyone looks at did not.
                    # Rejected here, the cell keeps its art and is recorded
                    # as a bad-cell candidate instead, so a SPRITE can carry
                    # it -- with its own palette, which is the only way a
                    # cell straddling three colour worlds is ever right.
                    if _bn > max(_bc * 1.15, _bc + 1.0):
                        if _TR and f"{i},{cx}" == _TR and _TRA <= k <= _TRB:
                            print(f"        -> dropped: block worsens "
                                  f"({_bc:.2f} -> {_bn:.2f})")
                        continue
                    if err_new >= bar and not (_bn < _bc * 0.85 and _bc > 1.2):
                        if _TR and f"{i},{cx}" == _TR and _TRA <= k <= _TRB:
                            print(f"        -> dropped: no gain "
                                  f"(new {err_new:.2f} cur {err_cur:.2f} "
                                  f"blk {_bn:.2f}/{_bc:.2f})")
                        continue
                    if args.anim_objects:
                        # this cell ANIMATES.  Record it for the
                        # OBJ layer instead of repainting the plane.  A plane
                        # refresh has to fit the cell into the BACKGROUND's
                        # 32 palettes, and a cell holding both Jessica's heel
                        # and the ground then repaints the ground through her
                        # palette -- the miscoloured background tile.  On OBJ
                        # the cell carries its own palette from a SEPARATE
                        # bank, so the two can never contend.  `shown` is
                        # deliberately NOT updated: the plane keeps showing
                        # the panorama, and the object is drawn over it.
                        if obj_src is not None:
                            oi = obj_src[runs[k][1]]
                            ocell = oi[ys:ye, cx * CELL:(cx + 1) * CELL]
                            if ocell.shape[0] != CELL:
                                pad = np.zeros((CELL, CELL, 4), ocell.dtype)
                                pad[:ocell.shape[0]] = ocell
                                ocell = pad
                            if ocell[..., 3].max() == 0:
                                continue      # no character here: leave the
                                              # panorama alone
                            anim.setdefault((i, cx), []).append((k, ocell.copy()))
                        else:
                            anim.setdefault((i, cx), []).append((k, live.copy()))
                        refreshed += 1
                        continue
                    if ch not in tiles:
                        tiles[ch] = len(tiles)
                    chunk += struct.pack(">3H", cell_off(COL0 + cx, ROW0 + i),
                                         BASE + tiles[ch], pi)
                    shown[(i, cx)] = rgb        # what is actually on screen now
                    shown_pi[(i, cx)] = pi
                    refreshed_cells.add((i, cx))
                    refreshed += 1
                # a band is primed only once it has had a live look -- NOT in the
            # same event that paints it, which is when it is still showing the
            # panorama's version of a later moment
            primed.update(b for b in range(top, bot + 1) if b not in painted_now)
            d0 = len(deltas)
            deltas += chunk
            dc = len(chunk) // 6
            script.append((0, 0, d0, dc | 0x8000, fe - fs + 1, SX0, sy))
            if _pass == 1 and k >= len(runs) - 24:
                # keep the last runs' screen state for the closing-fade pass
                tail_state[k] = ({c: v.copy() for c, v in shown.items()},
                                 dict(shown_pi), cum[k])
        if _pass == 0:
            # rank by miss, keep only as many as there are spare slots
            rank_claims.sort(reverse=True)
            claim_allow = {(i, cx) for _, i, cx in rank_claims[:SPARES]}
            worst = rank_claims[0][0] if rank_claims else 0.0
            print(f"  {len(rank_claims)} cells want a dedicated palette, "
                  f"{SPARES} spare(s) available (worst miss {worst:.1f})")
    print(f"{refreshed} animation refresh cells, {len(tiles)} tiles total")

    # ---- palette block (32 palettes x 16 words)
    pb = b""
    for pl in pal_order:
        words = []
        for c in pl:
            r, g, b = (c >> 8) & 15, (c >> 4) & 15, c & 15
            r, g, b = [0 if v == 1 else v for v in (r, g, b)]
            words.append(0xF000 | (r << 8) | (g << 4) | b)
        words += [0xF000] * (16 - len(words))
        pb += struct.pack(">16H", *words)
    pb += b"\x00" * (32 * 32 - len(pb))

    # ---- SPRITE PATCH LAYER.
    # One CPS1 palette must cover a whole 16x16 tile, but the Mega CD
    # composites SPRITES (their own palette, their own grid) over the
    # background plane -- so a cell straddling Jessica's leg and the
    # dithered ground has to serve two colour regimes from 15 entries and
    # renders as a checkerboard.  Sprites on CPS1 have the same escape
    # hatch: they carry an independent palette.  So the worst-fitting
    # cells are re-encoded as sprite tiles with dedicated palettes and
    # laid ON TOP of the plane, scrolling with it (the engine derives each
    # sprite's screen row from the live sy).
    # WIP, off by default: the sprites
    # place and colour correctly, but their tile content does not yet match
    # the cell beneath, so they read as a floating block.  Left in behind a
    # flag rather than shipped half-working.
    print(f"palettes: {len(pals)}/32 used "
          f"(panorama cap {PANO_CAP}, refresh claimed {len(pals) - PANO_CAP})")
    # Sprite patches are part of the shipped encoding -- default ON so a
    # fresh pipeline run cannot silently drop them.  --no-pan-patches is
    # the escape hatch for A/B measurement.
    PATCHES_ON = not OPT["no_pan_patches"]
    patches = []          # (band, col, tile_index, obj_pal_index)
    patch_tiles = []      # chunky bytes, appended after the plane's tiles
    patch_pals = []       # up to OBJ_PALS palettes, emitted as their own block
    # 24 -> 30.  The JP farewell has far more palette pressure than the USA
    # one (4,734 cells want a dedicated palette against 5 spares, worst miss
    # 33.8), and at 24 the budget ran out before Jessica's heel -- the same
    # checkerboard-under-the-leg defect the USA set fought.  ENGINE2's
    # Guy/Cody walker and ENGINE3's pan never run at the same time, so the
    # two can share the OBJ palette bank.
    OBJ_PALS = 30
    def make_patch(src_cell):
        """Encode one cell as a sprite tile + its own 14-colour palette.

        Returns (chunky, palette, rendered_rgb) without committing it, so the
        caller can measure whether the patch is actually an improvement.
        """
        cnt = {}
        for r, g, b in quant12(src_cell).reshape(-1, 3):
            kk = (int(r) << 8) | (int(g) << 4) | int(b)
            cnt[kk] = cnt.get(kk, 0) + 1
        # sprite pen 0 and 15 are transparent, so a sprite palette has 14
        # usable entries -- reserve pen 0 and keep the 14 most common
        want = sorted(cnt.items(), key=lambda kv: -kv[1])[:14]
        pal = [c for c, _ in want]
        lutp = {c: n + 1 for n, c in enumerate(pal)}      # pens 1..14
        q = quant12(src_cell)
        pens = np.zeros((CELL, CELL), dtype=np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                r, g, b = (int(v) for v in q[y, x])
                c = (r << 8) | (g << 4) | b
                if c not in lutp:
                    c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                            + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
                pens[y, x] = lutp[c]
        ch = bytearray(128)
        for y in range(CELL):
            for kx in range(8):
                ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                                  | int(pens[y, 2 * kx + 1]))
        rgb = np.zeros((CELL, CELL, 3), dtype=np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                c = pal[int(pens[y, x]) - 1]
                rgb[y, x] = (((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17,
                             (c & 15) * 17)
        return bytes(ch), pal, rgb

    patch_rgb = []           # what each patch RENDERS -- for --predict

    NEVER = 0x7FFF                     # end gate: compared SIGNED

    ptile_cache: dict[bytes, int] = {}

    def add_patch(ch, pal, i, cx, mind1, rgb=None, maxd1=NEVER, pal_index=None):
        # DEDUPE the sprite tiles.  Carrying every character cell
        # (not just the changing ones) is 26,223 patches, and one tile each
        # would be 3.3 MB -- but a held pose repeats its cell across every run
        # it spans, so the distinct art is a small fraction of that.
        ti = ptile_cache.get(ch)
        if ti is None:
            ti = len(patch_tiles)
            ptile_cache[ch] = ti
            patch_tiles.append(ch)
        if pal_index is None:
            patch_pals.append(pal)
            pal_index = len(patch_pals) - 1
        patch_rgb.append(rgb)
        patches.append((i, cx, ti, pal_index, mind1, maxd1))

    def make_patch_rgba(cell):
        """Isolated character cell -> sprite tile + palette of ITS colours.

        Pen 0 is transparent, so the background beneath is the panorama's --
        never re-quantised, never fighting for a palette entry.  That is the
        whole point of carrying the figures as sprites rather than as cells
        cut out of the composited frame.
        """
        rgb = cell[..., :3]
        opaque = cell[..., 3] > 0
        cnt = {}
        for r, g, b in quant12(rgb)[opaque].reshape(-1, 3):
            kk = (int(r) << 8) | (int(g) << 4) | int(b)
            cnt[kk] = cnt.get(kk, 0) + 1
        pal = [c for c, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:14]]
        lutp = {c: n + 1 for n, c in enumerate(pal)}
        q = quant12(rgb)
        pens = np.full((CELL, CELL), 15, dtype=np.uint8)   # 15 = transparent
        for y in range(CELL):
            for x in range(CELL):
                if not opaque[y, x]:
                    continue                      # pen 15 = show the plane
                r, g, b = (int(v) for v in q[y, x])
                c = (r << 8) | (g << 4) | b
                if c not in lutp:
                    c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                            + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
                pens[y, x] = lutp[c]
        ch = bytearray(128)
        for y in range(CELL):
            for kx in range(8):
                ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                                  | int(pens[y, 2 * kx + 1]))
        out = np.zeros((CELL, CELL, 3), np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                if pens[y, x] and pens[y, x] != 15:
                    c = pal[int(pens[y, x]) - 1]
                    out[y, x] = (((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17,
                                 (c & 15) * 17)
        return bytes(ch), pal, out

    def encode_rgba_with(cell, pal):
        rgb = cell[..., :3]
        opaque = cell[..., 3] > 0
        lutp = {c: n + 1 for n, c in enumerate(pal)}
        q = quant12(rgb)
        pens = np.full((CELL, CELL), 15, dtype=np.uint8)   # 15 = transparent
        for y in range(CELL):
            for x in range(CELL):
                if not opaque[y, x]:
                    continue
                r, g, b = (int(v) for v in q[y, x])
                c = (r << 8) | (g << 4) | b
                if c not in lutp:
                    c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                            + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
                pens[y, x] = lutp[c]
        ch = bytearray(128)
        for y in range(CELL):
            for kx in range(8):
                ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                                  | int(pens[y, 2 * kx + 1]))
        out = np.zeros((CELL, CELL, 3), np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                if pens[y, x] and pens[y, x] != 15:
                    c = pal[int(pens[y, x]) - 1]
                    out[y, x] = (((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17,
                                 (c & 15) * 17)
        err = float(np.abs(out[opaque].astype(int)
                           - rgb[opaque].astype(int)).mean()) if opaque.any() else 0.0
        return bytes(ch), out, err

    def encode_with(src_cell, pal):
        """Encode a cell against an EXISTING sprite palette.

        108 animating cells cannot each mint their own palette --
        the OBJ bank is 30 and the settled-tail patches already hold it.
        Reusing a palette that already covers the cell's colours costs a tile
        and nothing else, which is what makes the animation affordable.
        """
        lutp = {c: n + 1 for n, c in enumerate(pal)}
        q = quant12(src_cell)
        pens = np.zeros((CELL, CELL), dtype=np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                r, g, b = (int(v) for v in q[y, x])
                c = (r << 8) | (g << 4) | b
                if c not in lutp:
                    c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                            + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
                pens[y, x] = lutp[c]
        ch = bytearray(128)
        for y in range(CELL):
            for kx in range(8):
                ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                                  | int(pens[y, 2 * kx + 1]))
        rgb = np.zeros((CELL, CELL, 3), np.uint8)
        for y in range(CELL):
            for x in range(CELL):
                c = pal[int(pens[y, x]) - 1]
                rgb[y, x] = ((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17, (c & 15) * 17
        return bytes(ch), rgb

    # (a) RETIRED: cells the PANORAMA cannot fit, patched from frame 0.
    #
    # A gate-0 patch is a sprite pinned at y = band*16 - (sy - SY0).  That
    # tracks the plane only while the plane is STILL.  Measured on the JP
    # build (hbmame, screen grabs vs the Mega CD capture, aligned per frame
    # so pan lag is not counted), column 1 -- the left tree trunk, which
    # held five of the fifteen gate-0 patches:
    #
    # ENGINE3 frame   840   860   880   900   920   940
    # with patches    6.1   6.1   8.4  12.1  13.7  11.6
    # patch table off 4.0   4.0   4.0   4.0   4.0   4.1
    #
    # and over the static opening hold (frames 100/300/500/700) the two are
    # identical to 0.01 -- the class never paid for itself anywhere.  So it
    # bought nothing while the camera was still and wrecked a trunk column
    # once it moved: the checkerboard-under-the-leg defect reported against
    # both regional sets.  The trunk's dither is 1px, so a sprite that slips
    # against the plane reads as a scrambled block even though its palette
    # is right -- which is why every palette-space measurement came back
    # clean while the screen was visibly wrong.
    #
    # The whole OBJ palette budget now goes to (b), which gates on the
    # settled event and is measurably correct.  Re-enabling this class needs
    # an END gate in the patch record (the format carries a start gate only),
    # so a patch can be dropped before the camera moves.
    cell_err = []
    for i in range(Hc):
        for cx in range(COLS):
            pi = assign[i * COLS + cx]
            src_cell = pano[i * CELL:(i + 1) * CELL, cx * CELL:(cx + 1) * CELL]
            _, got = tile_of(src_cell, pi)
            e = float(np.abs(got.astype(int) - src_cell.astype(int)).mean())
            cell_err.append((e, i, cx))
    cell_err.sort(reverse=True)
    static_n = len(patches)

    # (b) cells still wrong once the camera SETTLES.  The pan stops with ~200
    # frames left, and the scene holds there under the END card -- so this is
    # the state the eye has longest to study.  Those cells are animated
    # (Jessica's feet and the ground behind her heel), which is why (a) skips
    # them and why the refresh cannot help: all 32 plane palettes are spent,
    # so the cell holding both her red shoe and the grey-green ground rendered
    # the ground in skin tones.  A patch carrying its own palette fixes it,
    # gated on the settled EVENT so it never paints over the animation.
    # Rank by IMPROVEMENT, not by residual, and only take decisive wins.
    # Ranking by residual picked Cody's jeans -- a smooth blue gradient the
    # plane renders acceptably and a 14-colour sprite renders about as well.
    # Swapping one for the other only changes the quantisation, and a 16x16
    # island quantised differently from its neighbours reads as a block:
    # the same failure the refresh's quality gate exists to prevent.  A cell
    # whose palette is categorically wrong (ground rendered in skin tones)
    # improves several-fold, so require the patch to at least halve the error.
    # NOT runs[-1]: the capture's final frame is a 1-frame run whose blues are
    # already dropping (the fade out of the scene), and patches built from it
    # painted a darker block over art the plane rendered correctly.  Take the
    # LONGEST run at the settled scroll -- the state the scene actually holds.
    settled = [k for k in range(len(runs)) if cum[k] == travel]
    k_ref = max(settled, key=lambda k: runs[k][2] - runs[k][1])
    final_live = imgs[runs[k_ref][0]]
    print(f"settled reference: run {k_ref} "
          f"(source frames {runs[k_ref][1]}-{runs[k_ref][2]})")
    tail_err = []
    for i in range(Hc):
        y0 = i * CELL - travel
        if y0 < ART_TOP or y0 + CELL > ART_BOT:
            continue                      # not on screen at the settled scroll
        for cx in range(COLS):
            cur = shown.get((i, cx))
            if cur is None:
                continue
            live = final_live[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
            e = float(np.abs(cur.astype(int) - live.astype(int)).mean())
            if e < 2.0:
                continue
            ch, pal, got = make_patch(live)
            pe = float(np.abs(got.astype(int) - live.astype(int)).mean())
            # Ratio AND absolute gain.  A pure ratio bar rejected the cell
            # holding Jessica's heel against the ground (4.3 -> 2.6): a real
            # improvement the eye can see, but not a halving.  The absolute
            # term is what keeps marginal requantisation out -- swapping a
            # cell for one no better just changes the dither and reads as a
            # block against its neighbours.
            if pe > e * 0.75 or (e - pe) < 1.5:
                continue
            tail_err.append((e - pe, e, pe, i, cx, ch, pal, got, live))
    # ---- ANIMATING CELLS AS GATED OBJECTS.
    # Emitted BEFORE the settled-tail patches on purpose: the
    # animation is what the eye follows, so it mints the OBJ
    # palettes it needs first and the static corrections reuse
    # what is left.  The other order left 187 poses on a poor
    # fit; this order leaves the corrections -- which are a
    # refinement of an already-acceptable cell -- to absorb it.
    # Each cell recorded during the refresh pass becomes one patch per RUN
    # of identical content, gated [first event, last event + 1).  The end
    # gate is what makes this possible at all: without it a
    # pose could never be retired and every frame's art would pile up.
    # These draw from the OBJ palette bank, so they cannot take a palette
    # from the background -- which is the whole point.  The figures stay in
    # the panorama; only what MOVES leaves.
    # with a BACKGROUND-ONLY panorama the characters have no
    # other source, so EVERY character cell must be an object -- not just the
    # ones the refresh pass flagged as changing.  Emitting only the changing
    # ones left the static parts of the figures unpainted, showing the
    # background through: the tan strip across the top of Cody's jeans, and
    # the review note "the top of her legs don't animate, causing a break".
    # Bands are fixed and their SCREEN position moves (y = band*16 - cum), so
    # the sampling mirrors the refresh loop's own geometry.
    if args.anim_objects and obj_src is not None:
        # Sample in the VM's OWN scroll, exported beside the isolated frames.
        # The converter's measured `cum` drifts a pixel against the real
        # vscroll, and a band sampled at the wrong offset slides across the
        # character art -- every run then looks like a new pose, which is how
        # 477 slots became 26,138 records (315 KB, overflowing E-SCRIPT).
        true_sy = {}
        stsv = args.obj_from / "scroll.tsv"
        if stsv.exists():
            for line in stsv.read_text().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                a_, b_ = line.split("\t")
                true_sy[int(a_)] = int(b_)
        anim = {}
        for k, (h, fs, fe) in enumerate(runs):
            oi = obj_src[fs]
            off = -true_sy[fs] if fs in true_sy else cum[k]
            for i in range(Hc):
                y0 = i * CELL - off
                if y0 < 0 or y0 + CELL > H:
                    continue
                for cx in range(COLS):
                    cell = oi[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                    if cell[..., 3].max() == 0:
                        continue            # no character in this cell
                    anim.setdefault((i, cx), []).append((k, cell.copy()))
        print(f"  character cells: {len(anim)} slot(s) over {len(runs)} runs")

    if args.anim_objects and anim:
        n_obj = reused = forced = 0
        errs = []
        # Collect every (cell, pose) first, then spend palettes HARDEST-FIRST.
        # Emitting in cell order is first-come allocation -- the same mistake
        # the plane side made, where late cells were stuck with whatever was
        # left (that is how the ground under Jessica's heel ended up in a skin
        # palette).  Hardness = distinct 12-bit colours in the cell: a cell
        # with many colours cannot be served by someone else's 14 entries,
        # while a flat one usually can.
        todo = []
        for (i, cx), samples in sorted(anim.items()):
            # emit the slot's DOMINANT content once across its
            # whole span, and only the exceptions per run.  Grouping strictly
            # by consecutive runs produced 26,223 records (315 KB, overflowing
            # E-SCRIPT) because a held pose re-emits every time the measured
            # scroll wobbles a pixel against the band grid.  A cell that shows
            # the same art for most of the scene should cost ONE record.
            from collections import Counter
            cnt = Counter(r.tobytes() for _k, r in samples)
            dom, ndom = cnt.most_common(1)[0]
            ks = [k for k, _ in samples]
            groups = []
            if ndom > 1:
                groups.append([dom, min(ks), max(ks),
                               next(r for _k, r in samples
                                    if r.tobytes() == dom)])
            run = None
            for k, rgbv in samples:
                key = rgbv.tobytes()
                if key == dom and ndom > 1:
                    run = None
                    continue
                if run is not None and run[0] == key and run[2] == k - 1:
                    run[2] = k
                else:
                    run = [key, k, k, rgbv]
                    groups.append(run)
            for _key, k0, k1, rgbv in groups:
                base = rgbv[..., :3] if rgbv.shape[-1] == 4 else rgbv
                q = quant12(base).reshape(-1, 3)
                ncol = len({(int(r) << 8) | (int(g) << 4) | int(b)
                            for r, g, b in q})
                todo.append((-ncol, i, cx, k0, k1, rgbv))
        todo.sort(key=lambda t: t[0])
        for _hard, i, cx, k0, k1, rgbv in todo:
            if rgbv.shape[-1] == 4:
                # ISOLATED character cell -- its palette is HERS alone, and
                # pen 0 lets the panorama's background through untouched
                best = None
                for pn, pl in enumerate(patch_pals):
                    ch2, rgb2, e = encode_rgba_with(rgbv, pl)
                    if best is None or e < best[0]:
                        best = (e, pn, ch2, rgb2)
                if best is not None and best[0] <= 5.0:
                    add_patch(best[2], None, i, cx, k0, best[3],
                              maxd1=k1 + 1, pal_index=best[1])
                    n_obj += 1; reused += 1; errs.append(best[0])
                elif len(patch_pals) < OBJ_PALS:
                    ch, pal, rgb = make_patch_rgba(rgbv)
                    add_patch(ch, pal, i, cx, k0, rgb, maxd1=k1 + 1)
                    n_obj += 1; errs.append(0.0)
                elif best is not None:
                    add_patch(best[2], None, i, cx, k0, best[3],
                              maxd1=k1 + 1, pal_index=best[1])
                    n_obj += 1; forced += 1; errs.append(best[0])
            else:
                # reuse the best existing OBJ palette when it is good enough
                best = None
                for pn, pl in enumerate(patch_pals):
                    ch2, rgb2 = encode_with(rgbv, pl)
                    e = float(np.abs(rgb2.astype(int) - rgbv.astype(int)).mean())
                    if best is None or e < best[0]:
                        best = (e, pn, ch2, rgb2)
                if best is not None and best[0] <= 5.0:
                    add_patch(best[2], None, i, cx, k0, best[3],
                              maxd1=k1 + 1, pal_index=best[1])
                    n_obj += 1
                    reused += 1
                    errs.append(best[0])
                elif len(patch_pals) < OBJ_PALS:
                    ch, pal, rgb = make_patch(rgbv)
                    add_patch(ch, pal, i, cx, k0, rgb, maxd1=k1 + 1)
                    n_obj += 1
                    errs.append(float(np.abs(rgb.astype(int)
                                             - rgbv.astype(int)).mean()))
                elif best is not None:
                    add_patch(best[2], None, i, cx, k0, best[3],
                              maxd1=k1 + 1, pal_index=best[1])
                    n_obj += 1
                    forced += 1
        errs.sort()
        q = lambda f: errs[min(len(errs) - 1, int(len(errs) * f))] if errs else 0
        print(f"  anim objects: {len(anim)} animating cell(s) -> {n_obj} "
              f"gated patch(es), {len(patch_pals)}/{OBJ_PALS} OBJ palettes "
              f"({reused} reused a palette, {forced} forced onto a poor fit)")
        print(f"  anim fit error: median {q(0.5):.2f}, p90 {q(0.9):.2f}, "
              f"p99 {q(0.99):.2f}, worst {errs[-1] if errs else 0:.2f}")

    tail_err.sort(key=lambda t: -t[0])
    # the settled-tail patches REUSE a palette when one already
    # fits.  Minting one each took the whole 30-entry OBJ bank before the
    # animating cells got a look in, and 187 of them were then forced onto a
    # poor fit -- the plane's palette starvation moved to OBJ rather than
    # cured.  These are all ground/figure cells at the same moment, so they
    # share palettes readily.
    # hold back part of the OBJ bank for CATASTROPHIC cells.
    # The settled-tail pass runs first (measured best overall) but it spent
    # the bank down to 13 spare, and the cell holding Jessica's lip against
    # Cody's white shirt -- which renders FLAT WHITE, the block seen in review
    # beside her mouth -- never got one.  A cell whose worst pixel is this
    # far out is not a refinement; it is a hole in the picture.
    TAIL_CAP = OBJ_PALS - 12

    def _hold_until(i, cx, art, k_from):
        """First run after k_from where the DISC stops showing this art.

        A settled-tail patch needs an end gate.  With none it is right for
        the pan's static tail and wrong for its last second: the scene FADES
        OUT, the plane repaints itself through the fade (the converter sees
        the source darken and refreshes), and the sprite would go on drawing
        the full-bright cell over it -- Cody's shoe cold blue-white against
        ground that had already gone warm.  The patch is only correct while
        its art is.
        """
        for k in range(k_from + 1,
                       len(runs) if k_fade is None else k_fade):
            y0 = i * CELL - cum[k]
            fr = imgs[runs[k][0]]
            if y0 < 0 or y0 + CELL > fr.shape[0]:
                return k
            now = fr[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
            if float(np.abs(now.astype(int) - art.astype(int)).mean()) > 2.0:
                return k
        return NEVER

    for _gain, e, pe, i, cx, ch, pal, got, live in tail_err:
        if not PATCHES_ON or len(patch_pals) >= TAIL_CAP:
            break
        reuse = None
        for pn, pl in enumerate(patch_pals):
            c2, r2 = encode_with(live, pl)
            er = float(np.abs(r2.astype(int) - live.astype(int)).mean())
            if reuse is None or er < reuse[0]:
                reuse = (er, pn, c2, r2)
        k_end = _hold_until(i, cx, live, k_ref)
        if reuse is not None and reuse[0] <= max(pe * 1.15, pe + 0.5):
            add_patch(reuse[2], None, i, cx, k_ref, reuse[3],
                      maxd1=k_end, pal_index=reuse[1])
        elif len(patch_pals) < OBJ_PALS:
            add_patch(ch, pal, i, cx, k_ref, got, maxd1=k_end)
        elif reuse is not None:
            add_patch(reuse[2], None, i, cx, k_ref, reuse[3],
                      maxd1=k_end, pal_index=reuse[1])
    # Optional self-check: render what the converter BELIEVES is on screen at
    # the settled scroll.  Diffing this against a frame grabbed from the ROM
    # separates the two failure classes that look identical on screen -- a
    # model bug (prediction != hardware) from an encoding limit (prediction ==
    # hardware, but neither matches the CD).
    if OPT["predict"]:
        # What the SETTLED frame should look like on hardware: the plane,
        # then the sprite patches composited ON TOP exactly as the OBJ
        # layer draws them.
        #
        # The prediction composites the patch layer ON TOP of the plane, not
        # the plane alone: the diagnostic's whole purpose is to separate a
        # model bug (prediction != hardware) from an encoding limit
        # (prediction == hardware, neither matches the CD), and a plane-only
        # prediction cannot see the patch layer at all -- it would read
        # "prediction-vs-hardware 0.00" on a cell whose patch is the thing at
        # fault.
        pred = np.zeros((H, W, 3), np.uint8)
        for i in range(Hc):
            y0 = i * CELL - travel
            if y0 < 0 or y0 + CELL > H:
                continue
            for cx in range(COLS):
                cur = shown.get((i, cx))
                if cur is None:
                    continue
                pred[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL] = cur
        drawn = 0
        for (i, cx, ti, _pi, _m), rgb in zip(patches, patch_rgb):
            if rgb is None:
                continue
            y0 = i * CELL - travel
            if y0 < 0 or y0 + CELL > H:
                continue
            pred[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL] = rgb
            drawn += 1
        Image.fromarray(pred).save(OPT["predict"])
        print(f"  predicted settled frame ({drawn}/{len(patches)} patches "
              f"composited) -> {OPT['predict']}")
    # ---- SCENE-WIDE bad-cell patches, emitted BEFORE the
    # settled-tail pass.  The tail pass took 23 of the 30 OBJ palettes
    # first and left these 7, so the cells that are wrong DURING the pan
    # -- the grey blocks on the ground either side of Jessica's heel --
    # went unpatched.  The worst cells claim palettes first; the tail
    # corrections, which refine an already-acceptable cell, take what is
    # left.  Same ordering lesson as the plane's rank-then-spend.
    # The settled-tail pass fixes the cells that are wrong once the camera
    # stops.  The cells reported in review -- the white block at Jessica's mouth,
    # the wrong palette either side of her heel -- are wrong DURING the pan,
    # and no amount of palette budget fixes them: they straddle two colour
    # worlds and cannot be held in 15 entries (the panorama cap sweep moved
    # the spares 5 -> 14 and left the worst miss at 38.2).  A sprite is
    # transparent over the boundary, so it can.  End gates (107r) let one
    # cover just the runs where it is needed.
    print(f'  bad-cell candidates recorded: {len(bad_cells)}, OBJ palettes used {len(patch_pals)}/{OBJ_PALS}')
    if bad_cells and PATCHES_ON:
        by_cell: dict[tuple[int, int], list] = {}
        for err, i, cx, k, live in bad_cells:
            by_cell.setdefault((i, cx), []).append((err, k, live))
        cand = []
        for (i, cx), lst in by_cell.items():
            lst.sort(key=lambda t: -t[0])
            cand.append((lst[0][0], i, cx, lst))
        cand.sort(key=lambda t: -t[0])
        BAD_BAR = 12.0                  # below this the cell reads as fine
        n_bad = 0
        # MEASURED ordering: this pass runs AFTER the settled-tail pass and
        # takes what is left.  Running it first, or splitting the bank in
        # half, both made things worse against the shipped build --
        # US 0.96 -> 23.04 (first) and -> 5.52 (split), JP 0.17 -> 0.29 --
        # because the tail corrections carry more of the scene than the
        # scene-wide outliers do.  Ordering by measurement, not by argument.
        BAD_SHARE = OBJ_PALS
        for err, i, cx, lst in cand:
            if err < BAD_BAR or len(patch_pals) >= BAD_SHARE:
                break
            # Each SPAN carries ITS OWN art.  Taking the worst frame's cell
            # and stamping it across every span the cell was bad painted a
            # LATER moment early -- review saw Jessica's foot appear at its
            # final position while she was still flat, and a white block by
            # her mouth that cleared only when the refresh caught up.  The
            # gate says WHEN; the art has to match that when.
            by_run = {}
            for _e, k, l in lst:
                by_run.setdefault(k, l)
            runs_k = sorted(by_run)
            span = [[runs_k[0], runs_k[0]]]
            for k in runs_k[1:]:
                if k == span[-1][1] + 1 and \
                        by_run[k].tobytes() == by_run[span[-1][0]].tobytes():
                    span[-1][1] = k
                else:
                    span.append([k, k])
            for si, (k0, k1) in enumerate(span):
                live = by_run[span[si][0]]
                if si == len(span) - 1:
                    # THE LAST SPAN RUNS ON WHILE THE DISC STILL
                    # SHOWS THIS ART -- it must not expire just because the
                    # camera started moving.
                    # Read from the OBJ page in game: the two patches over
                    # Jessica's jaw, cells (6,11) and (7,9), were gated
                    # `..71` and vanished on the exact frame the pan begins,
                    # uncovering the plane's unpatched quantisation of her
                    # chin -- pale blue and pink where the disc has skin.
                    # That is what review saw "when the scene scrolls up".
                    # Nothing about the disc changed at event 71; what
                    # stopped was the RECORDING (the refresh returns early
                    # once the camera moves), and the span end was being
                    # taken from the last recorded run.  A gate has to be
                    # decided by the picture, not by our bookkeeping.
                    while k1 + 1 < len(runs):
                        y0 = i * CELL - cum[k1 + 1]
                        fr = imgs[runs[k1 + 1][0]]
                        if y0 < 0 or y0 + CELL > fr.shape[0]:
                            break
                        nxt = fr[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                        if float(np.abs(nxt.astype(int)
                                        - live.astype(int)).mean()) > 2.0:
                            break
                        k1 += 1
                if si == 0:
                    # the FIRST span reaches BACK over the runs
                    # whose correct content is the same art.
                    #
                    # A band is not `primed` until its second run, so the
                    # opening runs are never recorded and the cell kept the
                    # panorama's wrong version through them -- with the
                    # 159-frame lead-in hold that is ~2.6 s of Jessica's
                    # cheek in the wrong colour, clearing only when the
                    # patches happened to start.
                    #
                    # Reaching back to event 0 unconditionally is WRONG, and
                    # measurably so: the END card is revealed a letter at a
                    # time, and a blanket extension put all three letters on
                    # screen from the first frame of the scene.  Walk back
                    # only while the DISC still shows this same art -- that
                    # is exactly the interval the patch is right for, and it
                    # reaches event 0 for a genuinely static cell.
                    while k0 > 0:
                        y0 = i * CELL - cum[k0 - 1]
                        fr = imgs[runs[k0 - 1][0]]
                        if y0 < 0 or y0 + CELL > fr.shape[0]:
                            break
                        was = fr[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                        if float(np.abs(was.astype(int)
                                        - live.astype(int)).mean()) > 2.0:
                            break
                        k0 -= 1
                # REUSE an existing OBJ palette when one already fits.  Minting
                # one per patch stopped this pass at 13 cells, and the flat
                # white block by Jessica's mouth was never among them.
                reuse = None
                for pn, pl in enumerate(patch_pals):
                    c2, r2 = encode_with(live, pl)
                    er = float(np.abs(r2.astype(int) - live.astype(int)).mean())
                    if reuse is None or er < reuse[0]:
                        reuse = (er, pn, c2, r2)
                ch, pal, got = make_patch(live)
                pe = float(np.abs(got.astype(int) - live.astype(int)).mean())
                if reuse is not None and reuse[0] <= max(pe * 1.25, pe + 1.0) \
                        and reuse[0] < err * 0.75:
                    add_patch(reuse[2], None, i, cx, k0, reuse[3],
                              maxd1=k1 + 1, pal_index=reuse[1])
                    n_bad += 1
                elif pe <= err * 0.75 and len(patch_pals) < BAD_SHARE:
                    add_patch(ch, pal, i, cx, k0, got, maxd1=k1 + 1)
                    n_bad += 1
        if n_bad:
            print(f"  scene-wide bad-cell patches: {n_bad} over "
                  f"{len(patch_pals)}/{OBJ_PALS} OBJ palettes "
                  f"(worst cell fixed {cand[0][0]:.1f})")

    print(f"{len(patches)} sprite patches, all settled-tail "
          f"(panorama class retired; its worst unpatched cell is "
          f"{cell_err[0][0]:.1f}). "
          + (f"best gain {tail_err[0][1]:.1f}->{tail_err[0][2]:.1f}, "
             f"worst taken {tail_err[min(len(tail_err), len(patches)) - 1][1]:.1f}"
             f"->{tail_err[min(len(tail_err), len(patches)) - 1][2]:.1f}"
             if tail_err else "no settled-tail cell qualified"))

    # ---- THE CLOSING FADE, AS PALETTE BLOCKS.
    # The scene ends on a fade, and until now the plane simply did not
    # follow it: the pan's 32 palettes are fitted on the FULL-BRIGHT
    # panorama, every one of them is spent, and a refresh cannot invent a
    # tinted version -- so the quality gate rejected the repaint and the
    # last runs kept bright art.  JP dims (small residual); the US fade
    # goes MAUVE, and the ground stayed olive under a pink sky for the
    # final second: 17 bad cells an event, the worst thing left in either
    # region.
    #
    # A fade is not an art change, it is a PALETTE change, and the engine
    # already indexes a palette block per event -- record word 1, read as
    # `palblocks_base + block*0x400` (build_opening_set, "palettes every
    # tick").  The pan converter always emitted block 0.  So: for each tail
    # run, recolour each PEN to what the disc shows at the pixels that pen
    # covers, and hand the event its own block.  The tiles never move, no
    # cell is repainted, and the cost is 1 KB per distinct step.
    fade_blocks: list[bytes] = []
    if tail_state:
        pen_rgb = [{tuple(int(v) for v in
                          ((c >> 8) & 15, (c >> 4) & 15, c & 15))
                    for c in pl} for pl in pal_order]
        for k in sorted(tail_state):
            if k <= k_ref or (k_fade is not None and k >= k_fade):
                continue
            snap, snap_pi, off = tail_state[k]
            frame = imgs[runs[k][0]]
            acc: dict[tuple[int, int], dict[int, int]] = {}
            err_cur = err_new = 0.0
            ncell = 0
            for (i, cx), cur in snap.items():
                pi = snap_pi.get((i, cx))
                if pi is None:
                    continue
                y0 = i * CELL - off
                if y0 < ART_TOP or y0 + CELL > min(ART_BOT, frame.shape[0]):
                    continue
                live = frame[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                if int(live.max()) < 12:
                    continue
                lq = quant12(live)
                pal = pal_order[pi]
                inv = {(((c >> 8) & 15) * 17, (((c >> 4) & 15)) * 17,
                        (c & 15) * 17): n for n, c in enumerate(pal)}
                for y in range(CELL):
                    for x in range(CELL):
                        n = inv.get(tuple(int(v) for v in cur[y, x]))
                        if n is None:
                            continue
                        r, g, bb = (int(v) for v in lq[y, x])
                        kk = (r << 8) | (g << 4) | bb
                        d = acc.setdefault((pi, n), {})
                        d[kk] = d.get(kk, 0) + 1
                ncell += 1
            if not ncell:
                continue
            new_order = [list(pl) for pl in pal_order]
            for (pi, n), cnt in acc.items():
                new_order[pi][n] = max(cnt.items(), key=lambda kv: kv[1])[0]
            # Score it: only take the block if it really tracks the fade.
            for (i, cx), cur in snap.items():
                pi = snap_pi.get((i, cx))
                if pi is None:
                    continue
                y0 = i * CELL - off
                if y0 < ART_TOP or y0 + CELL > min(ART_BOT, frame.shape[0]):
                    continue
                live = frame[y0:y0 + CELL, cx * CELL:(cx + 1) * CELL]
                if int(live.max()) < 12:
                    continue
                pal = pal_order[pi]
                inv = {(((c >> 8) & 15) * 17, (((c >> 4) & 15)) * 17,
                        (c & 15) * 17): n for n, c in enumerate(pal)}
                rgb = cur.copy()
                for y in range(CELL):
                    for x in range(CELL):
                        n = inv.get(tuple(int(v) for v in cur[y, x]))
                        if n is None:
                            continue
                        c = new_order[pi][n]
                        rgb[y, x] = (((c >> 8) & 15) * 17,
                                     (((c >> 4) & 15)) * 17, (c & 15) * 17)
                err_cur += float(np.abs(cur.astype(int) - live.astype(int)).mean())
                err_new += float(np.abs(rgb.astype(int) - live.astype(int)).mean())
            err_cur /= ncell
            err_new /= ncell
            if err_new > err_cur * 0.75:
                continue
            blk = b""
            for pl in new_order:
                words = []
                for c in pl:
                    r, g, bb = (c >> 8) & 15, (c >> 4) & 15, c & 15
                    r, g, bb = [0 if v == 1 else v for v in (r, g, bb)]
                    words.append(0xF000 | (r << 8) | (g << 4) | bb)
                words += [0xF000] * (16 - len(words))
                blk += struct.pack(">16H", *words)
            blk += b"\x00" * (32 * 32 - len(blk))
            if blk in fade_blocks:
                bi = fade_blocks.index(blk) + 1
            else:
                fade_blocks.append(blk)
                bi = len(fade_blocks)
            r = list(script[k])
            r[1] = bi
            script[k] = tuple(r)
            print(f"  closing fade: run {k} -> palette block {bi} "
                  f"({err_cur:.1f} -> {err_new:.1f})")
        # once a run switches block, every LATER run keeps it until the
        # next switch -- the engine reloads the block every tick from the
        # record, so an un-set record would snap back to full brightness
        # for a frame.
        carry = 0
        for k in range(len(script)):
            if script[k][1]:
                carry = script[k][1]
            elif carry:
                r = list(script[k])
                r[1] = carry
                script[k] = tuple(r)
        if fade_blocks:
            pb += b"".join(fade_blocks)
            print(f"  closing fade: {len(fade_blocks)} extra palette "
                  f"block(s), {len(fade_blocks) * 1024} B")



    # ---- emit
    out = args.outdir
    out.mkdir(parents=True, exist_ok=True)
    plane_tiles = [t for t, _ in sorted(tiles.items(), key=lambda kv: kv[1])]
    (out / "tiles.bin").write_bytes(b"".join(plane_tiles + patch_tiles))
    # patch table: u16 band, col, MIN event, MAX event, tile CODE, obj palette
    # the record grew an END gate.  With a start gate only a
    # patch can never be retired, which is fine for a static correction and
    # useless for animation -- it is why the figures could not be carried
    # here and had to be flattened into the plane, where a refresh repaints
    # background through a character's palette (the miscoloured tile
    # under Jessica's heel).  0x7FFF means "never expires"; it is compared
    # SIGNED, so 0xFFFF would read as -1 and retire every patch instantly.
    if k_fade is not None:
        # The picture is FROZEN from k_fade (the disc's closing fade, which
        # this build holds at full brightness rather than encode).  A patch
        # whose gate closes in there would uncover the plane's unpatched
        # version for the last frames of the scene -- the defect the patch
        # exists to hide, revealed exactly where the eye is resting.  Hold
        # every still-live patch to the end instead.
        held = 0
        for n, (band, col, ti, pal_i, mind1, maxd1) in enumerate(patches):
            if mind1 < k_fade <= maxd1 < NEVER:
                patches[n] = (band, col, ti, pal_i, mind1, NEVER)
                held += 1
        if held:
            print(f"  closing fade: {held} patch(es) held to the end")
    ptab = b""
    for band, col, ti, pal_i, mind1, maxd1 in patches:
        # +1: OBJ palette 0 is the engine's letterbox black, so the patch
        # palettes are uploaded from index 1 upward.  Emitting the raw index
        # made every patch draw with the black palette -- a solid black
        # square on screen.
        # mind1 = the EVENT INDEX the patch becomes valid at (0 = always).
        ptab += struct.pack(">6H", band, col, mind1, maxd1,
                            BASE + len(plane_tiles) + ti, pal_i + 1)
    ptab += struct.pack(">H", 0xFFFF)
    (out / "patches.bin").write_bytes(ptab)
    # OBJ palette block: OBJ_PALS x 16 words, pen 0 unused (transparent)
    pblk = b""
    for pal in patch_pals:
        words = [0xF000]
        for c in pal:
            r, g, b = (c >> 8) & 15, (c >> 4) & 15, c & 15
            r, g, b = [0 if v == 1 else v for v in (r, g, b)]
            words.append(0xF000 | (r << 8) | (g << 4) | b)
        words += [0xF000] * (16 - len(words))
        pblk += struct.pack(">16H", *words)
    pblk += b"\x00" * (OBJ_PALS * 32 - len(pblk))
    (out / "objpals.bin").write_bytes(pblk)
    (out / "palblocks.bin").write_bytes(pb)
    (out / "basemaps.bin").write_bytes(b"")     # unused at runtime
    (out / "deltas.bin").write_bytes(deltas)
    if args.hold_start and script:
        # Lengthen the opening hold.  The dialogue is carried by the CD
        # track and does not move, so holding here keeps the pair on screen
        # through her first lines instead of panning away under them.
        #
        # Pad the SETTLED hold, not event 0.  Event 0 is the base paint and
        # the CD leaves it up for only a few frames before its first delta
        # corrects the palette -- padding there froze the pre-correction
        # colours for three seconds (Cody's shirt read blue until it
        # suddenly warmed).  Among the leading events that have not started
        # panning, the longest is the scene's real hold beat.
        lead = [i for i, r in enumerate(script) if r[6] == SY0]
        lead = lead[:lead.index(max(lead)) + 1] if lead else []
        k = max(lead, key=lambda i: script[i][4]) if lead else 0
        r = list(script[k])
        r[4] += args.hold_start
        script[k] = tuple(r)
        print(f"opening hold +{args.hold_start} frames "
              f"(event {k} now {r[4]}f)")
    if args.anchors:
        pts = [tuple(int(v) for v in a.split(":")) for a in args.anchors.split(",")]
        pts = sorted(set([(0, 0)] + pts))
        def warp(x):
            if x <= pts[0][0]:
                return pts[0][1]
            for (s0, e0), (s1, e1) in zip(pts, pts[1:]):
                if x <= s1:
                    return e0 + (x - s0) * (e1 - e0) / max(1, s1 - s0)
            (s0, e0), (s1, e1) = pts[-2], pts[-1]
            return e1 + (x - s1) * (e1 - e0) / max(1, s1 - s0)
        cum, out_s = 0, []
        for rec in script:
            out_s.append((cum, rec))
            cum += rec[4]
        new_script = []
        for k, (start, rec) in enumerate(out_s):
            end = out_s[k + 1][0] if k + 1 < len(out_s) else cum
            d = int(round(warp(end))) - int(round(warp(start)))
            r = list(rec)
            r[4] = max(1, d)
            new_script.append(tuple(r))
        before, after = cum, sum(r[4] for r in new_script)
        script = new_script
        print(f"lip-sync warp: {len(pts)} anchors, {before} -> {after} frames")
    with open(out / "script.bin", "wb") as f:
        for rec in script:
            f.write(struct.pack(">HHIHHHH", *rec))
    with open(out / "script.tsv", "w") as f:
        f.write("# base_block\tpal_block\tdelta_off\tdcnt_flag\tframes\tsx\tsy\n")
        for rec in script:
            f.write("\t".join(str(v) for v in rec) + "\n")
    manifest = dict(
        base_code=BASE, events=len(script), pan_travel=travel,
        pano_rows=Hc, unique_tiles=len(tiles), tile_bytes=len(tiles) * 128,
        pal_blocks=len(pb) // 1024, palblock_bytes=len(pb),
        basemap_bytes=0,
        delta_bytes=len(deltas), script_bytes=len(script) * 16,
        total_frames=sum(r[4] for r in script),
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
