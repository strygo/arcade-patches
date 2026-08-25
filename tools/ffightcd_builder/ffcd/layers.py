#!/usr/bin/env python3
"""Extract the JP Guy/Cody farewell scene as LAYERS from a VDP dump.

The composited frames of this scene cost 11,377 tiles to convert, because
the background scrolls while BOTH characters move independently -- every
frame is unique.  The Mega CD itself spends a few KB: plane B is a repeating
pillar background driven by a scroll curve, plane A holds Cody's figure
(moved by plane-A hscroll), and Guy is ~16 hardware sprites.  This tool
recovers exactly those parts so the CPS1 port can rebuild the scene the
same way: background -> scroll2 panorama + sx curve, characters -> OBJ
sprite groups with per-event positions.

Input: a md_vdp_dump.lua directory (layers.txt + vram_<f>.bin/cram_<f>.bin
at key frames spanning the scene).

Output (into <outdir>):
    bg_pano.png            plane B's art rows, full plane width (wraps)
    cody_<n>.png           deduped Cody cels (plane A, bbox-cropped)
    guy_<n>.png            deduped Guy cels (sprite composite, bbox-cropped)
    track.tsv              frame  hsA  hsB  cody_cel  cody_x  cody_y
                                  guy_cel  guy_x  guy_y   (screen coords)

Coordinate conventions (measured against composite_5650.png):
    plane pixel p with hscroll h renders at screen x = (p + h) & 511
    sprite (sat_x, sat_y) renders at screen (sat_x - 128, sat_y - 128)
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent

ART_TOP, ART_BOT = 32, 160          # the letterbox window, screen rows


def load_words(p: Path) -> np.ndarray:
    return np.frombuffer(p.read_bytes(), dtype="<u2")


def cram_palette(cram: np.ndarray):
    # channel decode from md_vdp_render.cram_rgb (a hand-rolled version got
    # the green shift wrong and painted the teal corridor purple), but with
    # MAME's own non-linear intensity ramp instead of a linear *255//7 --
    # measured against the composite capture: my 36->52, 72->87, 109->116,
    # 145->144, 182->172, 218->206.  Using the same ramp keeps these layers
    # byte-consistent with the reunion conversion, which came from that
    # same capture.
    RAMP = (0, 52, 87, 116, 144, 172, 206, 255)
    pal = []
    for v in cram:
        r, g, b = (v >> 1) & 7, (v >> 5) & 7, (v >> 9) & 7
        pal.append((RAMP[r], RAMP[g], RAMP[b]))
    return pal


_TILES: dict = {}


def all_tiles(vram: np.ndarray):
    """Every 8x8 tile in VRAM at once -> (ntiles, 8, 8) pen indices.

    `tile_rows` was called 1,993,743 times per JP build to re-unpack the
    same few hundred tiles -- 68.8% of the export_layers stage.  A plane is
    64x32 cells drawn from a tile set that does not change while it is
    drawn, so the unpack is done once for the whole of VRAM instead.

    Keyed on VRAM's CONTENTS: the VM writes to VRAM between frames, so an
    identity- or index-keyed cache would hand back a stale tile set.  The
    hash costs ~30 us against the ~2 M calls it removes.  Padded to 2048
    tiles because nametable entries mask to 0x7FF and may name a tile past
    the end of a short dump -- a slice-based unpack would raise on that
    rather than returning blank.
    """
    key = vram.tobytes()
    hit = _TILES.get(key)
    if hit is not None:
        return hit
    n = len(vram) // 16
    w = vram[:n * 16].reshape(n, 8, 2).astype(np.uint16)
    # ">HH" then nibbles high-first == (w>>12, w>>8, w>>4, w) & 15
    t = np.stack([(w >> 12) & 15, (w >> 8) & 15, (w >> 4) & 15, w & 15],
                 axis=-1).reshape(n, 8, 8).astype(np.uint8)
    if n < 2048:
        t = np.concatenate([t, np.zeros((2048 - n, 8, 8), np.uint8)])
    if len(_TILES) > 4:                      # bounded; VRAM is 64 KB a copy
        _TILES.clear()
    _TILES[key] = t
    return t


def tile_rows(vram: np.ndarray, tile: int):
    """8x8 tile -> 8 rows of 8 pen indices (MD 4bpp packed)."""
    return all_tiles(vram)[tile]


def render_plane(vram, pal, nt_base, cw, ch):
    nt = vram[nt_base // 2: nt_base // 2 + cw * ch].astype(np.int64)
    t = all_tiles(vram)[nt & 0x7FF]                       # (cells, 8, 8)
    t = np.where(((nt & 0x800) != 0)[:, None, None], t[:, :, ::-1], t)
    t = np.where(((nt & 0x1000) != 0)[:, None, None], t[:, ::-1, :], t)
    palarr = np.asarray(pal, np.uint8)
    rgb = palarr[((nt >> 13) & 3)[:, None, None] * 16 + t]
    ink = (t != 0)                       # pen 0 stays fully transparent
    cells = np.zeros((len(nt), 8, 8, 4), np.uint8)
    cells[..., :3] = np.where(ink[..., None], rgb, 0)
    cells[..., 3] = np.where(ink, 255, 0)
    # cell i sits at (i // cw, i % cw), so regroup rather than blit
    return (cells.reshape(ch, cw, 8, 8, 4).transpose(0, 2, 1, 3, 4)
            .reshape(ch * 8, cw * 8, 4))


def render_sprites(vram, pal, sat, skip_mask_row=True):
    """Composite SAT entries into a screen-sized RGBA canvas."""
    img = np.zeros((224, 320, 4), np.uint8)
    for idx, y, sz, at, x in sat:
        sy, sx = y - 128, x - 128
        if skip_mask_row and sy < 16:
            continue                        # the letterbox mask sprites
        hs = ((sz >> 10) & 3) + 1
        vs = ((sz >> 8) & 3) + 1
        tile0 = at & 0x7FF
        p = (at >> 13) & 3
        hf, vf = at & 0x800, at & 0x1000
        for cx in range(hs):
            for cy in range(vs):
                t = tile_rows(vram, tile0 + cx * vs + cy)
                dx = (hs - 1 - cx) * 8 if hf else cx * 8
                dy = (vs - 1 - cy) * 8 if vf else cy * 8
                if hf:
                    t = t[:, ::-1]
                if vf:
                    t = t[::-1]
                for r in range(8):
                    yy = sy + dy + r
                    if not 0 <= yy < 224:
                        continue
                    for c in range(8):
                        xx = sx + dx + c
                        if not 0 <= xx < 320:
                            continue
                        pen = t[r, c]
                        if pen and not img[yy, xx, 3]:
                            col = pal[p * 16 + pen]
                            img[yy, xx] = (*col, 255)
    return img


def bbox(img):
    a = img[:, :, 3]
    ys, xs = np.nonzero(a)
    if not len(ys):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def parse_layers(p: Path):
    frames = {}
    for l in p.read_text().splitlines():
        if not l.startswith("f="):
            continue
        f = int(l.split()[0][2:])
        hs = [int(v) for v in l.split("hs=")[1].split()[0].split(",")]
        sat = []
        sats = l.split("sat=")[1] if "sat=" in l else ""
        for e in sats.split(";"):
            if ":" not in e:
                continue
            i, rest = e.split(":")
            y, sz, at, x = (int(v) for v in rest.split(","))
            sat.append((int(i), y, sz, at, x))
        def signed(v):
            return v - 65536 if v > 32767 else v
        frames[f] = dict(hsA=signed(hs[0]), hsB=signed(hs[1]), sat=sat)
    return frames


def main() -> int:
    dump, outdir = Path(sys.argv[1]), Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    frames = parse_layers(dump / "layers.txt")
    keys = sorted(int(p.stem.split("_")[1]) for p in dump.glob("vram_*.bin"))
    print(f"{len(frames)} sampled frames, {len(keys)} vram keys")

    # ---- background panorama from the FIRST key (plane B is static art;
    # verified below against every later key)
    vram0 = load_words(dump / f"vram_{keys[0]}.bin")
    pal0 = cram_palette(load_words(dump / f"cram_{keys[0]}.bin"))
    # nametable bases from the regs of the closest layers.txt line: the
    # renderer measured A=0x0000 B=0x2000, plane 64x32 -- fixed here, and
    # asserted per key below by comparing plane B content.
    A_BASE, B_BASE, CW, CH = 0x0000, 0x2000, 64, 32
    bgs = {}
    for k in keys:
        v = load_words(dump / f"vram_{k}.bin")
        b = render_plane(v, cram_palette(load_words(dump / f"cram_{k}.bin")),
                         B_BASE, CW, CH)
        bgs[k] = b
    b0 = bgs[keys[0]]
    for k in keys[1:]:
        if not np.array_equal(bgs[k], b0):
            d = (bgs[k][:, :, :3].astype(int) - b0[:, :, :3].astype(int))
            print(f"  NOTE plane B differs at key {k}: mean {np.abs(d).mean():.2f}")
    art = b0[ART_TOP:ART_BOT]
    Image.fromarray(art).save(outdir / "bg_pano.png")
    print(f"bg_pano: {art.shape[1]}x{art.shape[0]}")

    # ---- character cels at each key
    cody_cels, guy_cels = [], []          # [(hash, filename, bbox_size)]
    cody_of, guy_of = {}, {}              # key frame -> (cel_i, bbox)
    for k in keys:
        v = load_words(dump / f"vram_{k}.bin")
        pl = cram_palette(load_words(dump / f"cram_{k}.bin"))
        a = render_plane(v, pl, A_BASE, CW, CH)
        bb = bbox(a)
        if bb:
            cel = a[bb[1]:bb[3], bb[0]:bb[2]]
            h = cel.tobytes()
            for i, (hh, _) in enumerate(cody_cels):
                if hh == h:
                    cody_of[k] = (i, bb)
                    break
            else:
                cody_cels.append((h, cel))
                cody_of[k] = (len(cody_cels) - 1, bb)
        sat = frames[min(frames, key=lambda f: abs(f - k))]["sat"]
        s = render_sprites(v, pl, sat)
        bb = bbox(s)
        if bb:
            cel = s[bb[1]:bb[3], bb[0]:bb[2]]
            h = cel.tobytes()
            for i, (hh, _) in enumerate(guy_cels):
                if hh == h:
                    guy_of[k] = (i, bb)
                    break
            else:
                guy_cels.append((h, cel))
                guy_of[k] = (len(guy_cels) - 1, bb)
    for i, (_, cel) in enumerate(cody_cels):
        Image.fromarray(cel).save(outdir / f"cody_{i}.png")
    for i, (_, cel) in enumerate(guy_cels):
        Image.fromarray(cel).save(outdir / f"guy_{i}.png")
    print(f"cody: {len(cody_cels)} cel(s)  sizes "
          f"{[c.shape[:2] for _, c in cody_cels]}")
    print(f"guy:  {len(guy_cels)} cel(s)  sizes "
          f"{[c.shape[:2] for _, c in guy_cels]}")

    # ---- per-frame track.  Cody's screen x = plane bbox x + hsA (mod 512);
    # cel index from the nearest key.  Guy's anchor from the SAT directly.
    with open(outdir / "track.tsv", "w") as f:
        f.write("# frame\thsA\thsB\tcody_cel\tcody_x\tcody_y\tguy_cel\tguy_x\tguy_y\n")
        for fr in sorted(frames):
            fd = frames[fr]
            k = min(keys, key=lambda kk: abs(kk - fr))
            crow = grow = ""
            if k in cody_of:
                ci, bb = cody_of[k]
                cx = (bb[0] + fd["hsA"]) % 512
                if cx > 320:
                    cx -= 512
                crow = f"{ci}\t{cx}\t{bb[1]}"
            else:
                crow = "-1\t0\t0"
            chars = [(y - 128, x - 128) for _, y, _, _, x in fd["sat"]
                     if y - 128 >= 16]
            if chars:
                gy = min(c[0] for c in chars)
                gx = min(c[1] for c in chars)
                gi = guy_of.get(k, (-1, None))[0]
                grow = f"{gi}\t{gx}\t{gy}"
            else:
                grow = "-1\t0\t0"
            f.write(f"{fr}\t{fd['hsA']}\t{fd['hsB']}\t{crow}\t{grow}\n")
    print(f"track.tsv: {len(frames)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
