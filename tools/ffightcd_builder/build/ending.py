"""The ending, all three of its scenes.

The arcade ending is one sequence carrying three pieces of CD content on a
shared timeline, so they live together:

  * esnaps   -- the reunion, sliced out of the ending sweep at its first
                full-bright frame;
  * layers   -- the JP pillar-hall scene exported AS LAYERS (plane B
                panorama, plane A Cody, hardware sprites Guy): 527 tiles
                against 11,377 for composited frames.  JP only: the US
                ending never plays this scene, though the art is still on
                the US disc;
  * merge    -- that layer set folded into the reunion conv, with the
                sprite table the engine walks;
  * farewell -- the CD-6 pan, rendered from the VM's own run.  Its camera
                is the game's own plane-B vscroll, unwrapped.

    ending.py esnaps
    ending.py layers <outdir> [jp] [f_from f_to step keystep] [--phase P --frame-off N]
    ending.py merge <layers> <rconv> <out> [esnap]
    ending.py farewell <outdir> <jp|us> --window A-B
"""
from __future__ import annotations
import argparse
from PIL import Image
from convert import quant12  # noqa: E402
from pathlib import Path
import importlib.util
import json
import numpy as np
import os
import shutil
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # sibling stages (convert.quant12)
sys.path.insert(0, str(HERE.parent))   # track root: ffcd/ lives there
from ffcd.player import ScriptPlayer    # noqa: E402
from ffcd import megadrive, layers as X  # noqa: E402
from ffcd.files import link_or_copy  # noqa: E402


# ======== esnaps ========================================================
REPO = Path(__file__).resolve().parents[1]
B = REPO / "work/build/vmsweep"
PLANS = (
    ("jp", 229, None),      # slice to sweep end (guycody head beats live here)
    ("us", 231, 1513),      # reunion only; black-pad to the 0-1512 window
)

def _esnaps(a) -> int:
    import argparse
    global B
    if a.root:
        B = Path(a.root)
    for region, fb, pad_to in PLANS:
        end = B / f"{region}_end"
        out = B / f"{region}_esnap"
        if not end.is_dir():
            print(f"{region}_esnap: no {region}_end sweep, skipped")
            continue
        frames = sorted(int(p.stem[1:]) for p in end.glob("f*.png"))
        last = frames[-1]
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        n = 0
        for k in range(0, last - fb + 1):
            link_or_copy(end / f"f{fb + k:06d}.png", out / f"f{k:06d}.png")
            n += 1
        if pad_to is not None:
            for k in range(n, pad_to):
                link_or_copy(end / f"f{last:06d}.png", out / f"f{k:06d}.png")
            n = pad_to
        print(f"{region}_esnap: {n} frames (= {region}_end[{fb}:]"
              f"{f' + black pad to {pad_to}' if pad_to else ''})")
    return 0


# ======== layers ========================================================
FRAME_OFF = {"jp": 3729}
DEFAULTS = dict(f_from=5600, f_to=6540, step=2, keystep=2)
A_BASE, B_BASE, CW, CH = 0x0000, 0x2000, 64, 32     # as the extractor asserts
def sample(player):
    """One frame of layer state: registers, hscroll head, sprite table.

    Both tables live in VRAM at the addresses the registers name (reg13 ->
    hscroll base, reg5 -> SAT base), so this is a read of state the VM
    already produced -- no re-emulation, and the same two reads the VDP
    itself does when it renders the frame.
    """
    vram, cram, regs, vsram = player.snapshot()
    hs_base = regs[13] * 1024
    sat_base = (regs[5] & 0x7F) * 512
    hs = [hs_word(vram, hs_base // 2 + i) for i in range(4)]
    sat = []
    for s in range(80):
        b = sat_base // 2 + s * 4
        y, sz, at, x = vram[b], vram[b + 1], vram[b + 2], vram[b + 3]
        if y != 0 or x != 0:
            sat.append((s, y, sz, at, x))
    return vram, cram, regs, vsram, hs, sat
def hs_word(vram, i):
    return vram[i]
def signed(v):
    return v - 65536 if v > 32767 else v
def walk(region, o, want_key):
    """Run the scene once, yielding (frame, hs, sat, vram, cram, is_key)."""
    off = FRAME_OFF[region]
    p = ScriptPlayer(region=region, bundle="E")
    for vf in range(o["f_to"] - off + 1):
        p.vblank()
        p.tick()
        prev = p.part
        p.after_tick()
        if p.part != prev and p.part >= len(p.PART_CODE):
            raise SystemExit(f"sequence looped at VM f{vf} before f{o['f_to']}")
        f = vf + off
        if f < o["f_from"] or f > o["f_to"] or (f - o["f_from"]) % o["step"]:
            continue
        key = want_key(f)
        vram, cram, regs, vsram, hs, sat = sample(p)
        yield f, hs, sat, (np.array(vram, dtype=np.uint16) if key else None), \
            (np.array(cram, dtype=np.uint16) if key else None), vsram, regs, key
def export(outdir, region="jp", f_from=None, f_to=None, step=None,
           keystep=None):
    """Emit bg_pano/cody_*/guy_*/track.tsv for the scene, VM-direct."""
    o = dict(DEFAULTS)
    for k, v in (("f_from", f_from), ("f_to", f_to), ("step", step),
                 ("keystep", keystep)):
        if v is not None:
            o[k] = v
    os.makedirs(outdir, exist_ok=True)
    keyset = set(range(o["f_from"], o["f_to"] + 1, o["keystep"]))

    frames = {}                     # f -> dict(hsA, hsB, sat)   (every sample)
    keys = []                       # ordered key frames
    cody_cels, guy_cels = [], []    # [(bytes, ndarray)]
    cody_of, guy_of = {}, {}        # key -> (cel index, bbox)
    bg0 = None

    for f, hs, sat, vram, cram, vsram, regs, key in walk(
            region, o, lambda f: f in keyset):
        frames[f] = dict(hsA=signed(hs[0]), hsB=signed(hs[1]), sat=sat)
        if not key:
            continue
        keys.append(f)
        pal = X.cram_palette(cram)
        b = X.render_plane(vram, pal, B_BASE, CW, CH)
        if bg0 is None:
            bg0 = b
        elif not np.array_equal(b, bg0):
            d = b[:, :, :3].astype(int) - bg0[:, :, :3].astype(int)
            print(f"  NOTE plane B differs at key {f}: mean "
                  f"{np.abs(d).mean():.2f}")
        # Cody: plane A, already an isolated figure on transparency
        a = X.render_plane(vram, pal, A_BASE, CW, CH)
        _dedupe(a, cody_cels, cody_of, f)
        # Guy: the sprite set, composited (letterbox mask rows skipped)
        s = X.render_sprites(vram, pal, sat)
        _dedupe(s, guy_cels, guy_of, f)

    art = bg0[X.ART_TOP:X.ART_BOT]
    Image.fromarray(art).save(os.path.join(outdir, "bg_pano.png"))
    for i, (_, cel) in enumerate(cody_cels):
        Image.fromarray(cel).save(os.path.join(outdir, f"cody_{i}.png"))
    for i, (_, cel) in enumerate(guy_cels):
        Image.fromarray(cel).save(os.path.join(outdir, f"guy_{i}.png"))
    _write_track(os.path.join(outdir, "track.tsv"), frames, keys,
                 cody_of, guy_of)
    print(f"{outdir}: bg_pano {art.shape[1]}x{art.shape[0]}, "
          f"cody {len(cody_cels)} cel(s), guy {len(guy_cels)} cel(s), "
          f"{len(frames)} track rows, {len(keys)} keys")
    return outdir
def _dedupe(img, cels, where, f):
    bb = X.bbox(img)
    if not bb:
        return
    cel = img[bb[1]:bb[3], bb[0]:bb[2]]
    h = cel.tobytes()
    for i, (hh, _) in enumerate(cels):
        if hh == h:
            where[f] = (i, bb)
            return
    cels.append((h, cel))
    where[f] = (len(cels) - 1, bb)
def _write_track(path, frames, keys, cody_of, guy_of):
    """The extractor's track math, verbatim: Cody's screen x is his plane
    bbox walked by plane-A hscroll; Guy's anchor comes off the SAT."""
    with open(path, "w") as fh:
        fh.write("# frame\thsA\thsB\tcody_cel\tcody_x\tcody_y"
                 "\tguy_cel\tguy_x\tguy_y\n")
        for fr in sorted(frames):
            fd = frames[fr]
            k = min(keys, key=lambda kk: abs(kk - fr))
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
                grow = (f"{guy_of.get(k, (-1, None))[0]}"
                        f"\t{min(c[1] for c in chars)}"
                        f"\t{min(c[0] for c in chars)}")
            else:
                grow = "-1\t0\t0"
            fh.write(f"{fr}\t{fd['hsA']}\t{fd['hsB']}\t{crow}\t{grow}\n")
def dump_mode(outdir, region="jp", f_from=None, f_to=None, step=None,
              keystep=None):
    """Write a md_vdp_dump.lua-shaped directory instead of layers.

    This is not part of the build path; it exists so the UNMODIFIED
    ffcd/layers.py can be run against VM state as a contract
    test of export() above (the two must agree byte-for-byte).  regs is
    24 entries here, not MAME's 32 -- the extractor never reads them.
    """
    o = dict(DEFAULTS)
    for k, v in (("f_from", f_from), ("f_to", f_to), ("step", step),
                 ("keystep", keystep)):
        if v is not None:
            o[k] = v
    os.makedirs(outdir, exist_ok=True)
    keyset = set(range(o["f_from"], o["f_to"] + 1, o["keystep"]))
    le = lambda w: struct.pack("<%dH" % len(w), *(int(x) & 0xFFFF for x in w))
    with open(os.path.join(outdir, "layers.txt"), "w") as log:
        for f, hs, sat, vram, cram, vsram, regs, key in walk(
                region, o, lambda f: f in keyset):
            log.write("f=%d regs=%s hs=%s nspr=%d sat=%s\n" % (
                f, ",".join(str(r) for r in regs),
                ",".join(str(h) for h in hs), len(sat),
                ";".join("%d:%d,%d,%d,%d" % e for e in sat)))
            if key:
                for name, w in (("vram", vram), ("cram", cram),
                                ("vsram", vsram)):
                    with open(os.path.join(outdir, f"{name}_{f}.bin"),
                              "wb") as fh:
                        fh.write(le(w))
    print(f"{outdir}: md_vdp_dump-format export")
    return outdir

def _layers(a) -> int:
    # THE PILLAR-HALL SCENE IS JP-ONLY.  The US ending never plays it -- the
    # art is still on the US disc (its ENDING.BIN chunks 0 and 3 are
    # byte-identical to JP's and carry the tiles), but the US script chunk
    # does not reference it.  argparse's `choices` makes that a usage error
    # rather than an obscure KeyError on FRAME_OFF.
    if a.phase is not None and a.frame_off is None:
        raise SystemExit("--phase requires --frame-off: the phase "
                         "translates the scene, so the committed "
                         "FRAME_OFF does not carry over")
    if a.phase is not None:
        import ffcd.player as _sp
        _sp.CD_PHASE = a.phase
    if a.frame_off is not None:
        FRAME_OFF["jp"] = a.frame_off
    fn = dump_mode if a.dump else export
    fn(a.outdir, a.region, *a.window[:4])
    return 0


# ======== merge =========================================================
CELL = 16
COLS, ROWS = 20, 14
ROW0, COL0 = 0x11, 0x26
SX0, SY0 = 0x200, 0x100
ART_ROW0 = 2                      # art rows 2..9 = screen 32..159
FPS_STEP = 1                      # one script event per TRACK ROW.
SCENE_F0, SCENE_F1 = 5620, 6480   # bright content window (fade-out excluded)
GAP_F = 50                        # inter-scene black gap.  Was 113, which
SEAM_F = 105                      # disc frames from reunion-window end to scene
HEAD_F = 51                       #   ...of which the engine-side head owns this
TOTAL = 2560                      # take-over length, must not change
OBJ_X, OBJ_Y = 96, 16             # OBJ origin (64) + art-window x (32):
SPR_DY = 3                        # MD sprite layer offset (measured, 0.00 fit)
def be16(v):
    return struct.pack(">H", v & 0xFFFF)
def cell_off(col, row):
    col &= 0x3F
    return ((row & 0x0F) + (col << 4) + ((row & 0x30) << 6)) * 4
def enc_cell16(qcell, pal_order, lut):
    """Encode a quantised 16x16 RGB cell against one palette (scroll cell)."""
    pens = np.zeros((CELL, CELL), np.uint8)
    for y in range(CELL):
        for x in range(CELL):
            r, g, b = (int(v) for v in qcell[y, x])
            c = (r << 8) | (g << 4) | b
            if c not in lut:
                c = min(pal_order, key=lambda pc: ((pc >> 8) - r) ** 2
                        + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
            pens[y, x] = lut[c]
    ch = bytearray(128)
    for y in range(CELL):
        for k in range(8):
            ch[y * 8 + k] = (int(pens[y, 2 * k]) << 4) | int(pens[y, 2 * k + 1])
    return bytes(ch)
HEAD_NOFADE = False

def _merge(a) -> int:
    global HEAD_NOFADE
    HEAD_NOFADE = a.head_nofade
    gdir, rconv, out, esnap = a.gdir, a.rconv, a.out, a.esnap
    out.mkdir(parents=True, exist_ok=True)

    # ---- reunion conversion (verbatim; we append to it)
    rman = json.loads((rconv / "manifest.json").read_text())
    BASE = rman["base_code"]
    r_tiles = (rconv / "tiles.bin").read_bytes()
    r_pal = (rconv / "palblocks.bin").read_bytes()
    r_base = (rconv / "basemaps.bin").read_bytes()
    r_delta = (rconv / "deltas.bin").read_bytes()
    r_script = (rconv / "script.bin").read_bytes()
    n_rev = len(r_script) // 16
    n_rtiles = len(r_tiles) // 128
    n_rpb = len(r_pal) // 1024
    n_rbase = len(r_base) // 1120
    print(f"reunion: {n_rev} events, {n_rtiles} tiles, {n_rpb} palblocks")

    # ---- track + layers
    track = {}
    for l in (gdir / "track.tsv").read_text().splitlines():
        if l.startswith("#"):
            continue
        v = [int(x) for x in l.split("\t")]
        track[v[0]] = v[1:]
    frames = sorted(f for f in track if SCENE_F0 <= f <= SCENE_F1)
    bg = np.asarray(Image.open(gdir / "bg_pano.png").convert("RGB"))

    def cels_of(tag, n):
        out_ = []
        for i in range(n):
            p = gdir / f"{tag}_{i}.png"
            out_.append(np.asarray(Image.open(p)) if p.exists() else None)
        return out_
    cody = cels_of("cody", 40)
    guy = cels_of("guy", 40)
    cody = [c for c in cody if c is not None]
    guy = [c for c in guy if c is not None]

    # The CD dims the characters over f6180-6330 (~26%: character-region
    # brightness 131 -> 97) as the talk ends.  The first design swapped any
    # cel referenced ONLY in that window for a bright "pose-twin by nearest
    # alpha mask", assuming every dim pose recurs bright elsewhere.  That
    # assumption is FALSE for the one moment that matters: Cody's turn
    # toward the camera as he crosses in front of Guy happens entirely
    # inside the window, has no bright twin, and the nearest-mask
    # substitute was a standing profile -- the turn simply vanished from
    # the ROM (user "frames of the cody/guy animation are
    # missing (cody looking at the camera)").
    #
    # Dim-only cels are now encoded as REAL cels.  Two palettes stay
    # bright-only (char_pal below still receives `bright`): enc_char_cel
    # nearest-matches off-palette colours, so a dim cel renders through the
    # bright palette -- ~26% lighter than the CD for ~1.5 s, which the
    # no-fade policy already accepts elsewhere, against an entire missing
    # animation the other way.
    DIM0, DIM1 = 6180, 6330
    used = {"cody": set(), "guy": set()}     # every referenced cel: ENCODED
    bright = {"cody": set(), "guy": set()}   # bright-referenced: PALETTE
    for f in frames:
        hsA, hsB, cc, cx, cy, gc, gx, gy = track[f]
        if cc >= 0:
            used["cody"].add(cc)
        if gc >= 0:
            used["guy"].add(gc)
        if not (DIM0 <= f <= DIM1):
            if cc >= 0:
                bright["cody"].add(cc)
            if gc >= 0:
                bright["guy"].add(gc)
    ctwin, gtwin = {}, {}                    # no substitution
    print(f"cels: cody {sorted(used['cody'])} (dim-only "
          f"{sorted(used['cody'] - bright['cody'])}) "
          f"guy {sorted(used['guy'])} (dim-only "
          f"{sorted(used['guy'] - bright['guy'])})")

    # ---- character OBJ palettes: cody <=14; guy merged to <=14
    def char_pal(cels, idxs, name):
        cnt = {}
        for i in idxs:
            vis = quant12(cels[i][:, :, :3])[cels[i][:, :, 3] > 0]
            for r, g, b in vis:
                k = (int(r) << 8) | (int(g) << 4) | int(b)
                cnt[k] = cnt.get(k, 0) + 1
        cols = sorted(cnt, key=lambda k: -cnt[k])
        while len(cols) > 14:
            # merge the rarest colour into its nearest neighbour
            drop = cols[-1]
            cols = cols[:-1]
            print(f"  {name}: merged colour {drop:#05x} "
                  f"({cnt[drop]} px) into nearest")
        return cols
    cpal = char_pal(cody, bright["cody"], "cody")
    gpal = char_pal(guy, bright["guy"], "guy")

    # ---- character cel -> OBJ entries (16x16 cells, anchor-relative)
    tiles = bytearray(r_tiles)
    tcache: dict[bytes, int] = {}
    def tile_id(ch):
        if ch not in tcache:
            tcache[ch] = len(tiles) // 128
            tiles.extend(ch)
        return tcache[ch]

    def enc_char_cel(cel, pal, obj_pal_i):
        """RGBA cel -> [(dx, dy, code, attr)], colours at pens 1..14.

        Transparent pixels are PEN 15: CPS1 sprites key on pen 15 (MAME's
        cps1 driver, transpen 15).  Encoding them as pen 0 drew every cel
        as its full bounding box with an opaque black border -- the black
        rectangles around both characters in the first hardware run.
        """
        lut = {c: n + 1 for n, c in enumerate(pal)}
        q = quant12(cel[:, :, :3])
        a = cel[:, :, 3] > 0
        h, w = a.shape
        ents = []
        for cy in range(0, h, CELL):
            for cx in range(0, w, CELL):
                sub_a = a[cy:cy + CELL, cx:cx + CELL]
                if not sub_a.any():
                    continue
                pens = np.full((CELL, CELL), 15, np.uint8)
                sub_q = q[cy:cy + CELL, cx:cx + CELL]
                for y in range(sub_a.shape[0]):
                    for x in range(sub_a.shape[1]):
                        if not sub_a[y, x]:
                            continue
                        r, g, b = (int(v) for v in sub_q[y, x])
                        c = (r << 8) | (g << 4) | b
                        if c not in lut:
                            c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                                    + (((pc >> 4) & 15) - g) ** 2
                                    + ((pc & 15) - b) ** 2)
                        pens[y, x] = lut[c]
                ch = bytearray(128)
                for y in range(CELL):
                    for k in range(8):
                        ch[y * 8 + k] = ((int(pens[y, 2 * k]) << 4)
                                         | int(pens[y, 2 * k + 1]))
                ents.append((cx, cy, BASE + tile_id(bytes(ch)), obj_pal_i))
        return ents

    cel_entries = []                      # global cel table
    cel_key = {}                          # (tag, idx) -> global cel id
    for tag, cels_, idxs, pal, opi in (("cody", cody, used["cody"], cpal, 1),
                                       ("guy", guy, used["guy"], gpal, 2)):
        for i in sorted(idxs):
            cel_key[(tag, i)] = len(cel_entries)
            cel_entries.append(enc_char_cel(cels_[i], pal, opi))
    maxc = max(len(e) for e in cel_entries)
    print(f"{len(cel_entries)} OBJ cels, max {maxc} cells; "
          f"tiles now {len(tiles)//128}")

    # ---- background: quantise the pano into cells, one palette block
    qbg = quant12(bg)
    Wc = bg.shape[1] // CELL              # 32 pano columns
    Hc = bg.shape[0] // CELL              # 8 art rows
    cnt = {}
    for r, g, b in qbg.reshape(-1, 3):
        k = (int(r) << 8) | (int(g) << 4) | int(b)
        cnt[k] = cnt.get(k, 0) + 1
    # RESERVE a true black.  The pano's own darkest colour is (3,5,5) -- a
    # dark teal -- so encoding an all-black cell against a palette taken
    # purely from the art snapped every pixel to that teal, and the scene
    # played with dark-green letterbox bars instead of black ones.  Black is
    # not in the art but the scene needs it, so it is added explicitly and
    # the histogram gets the remaining 14 slots.
    bgpal = [0x000] + [c for c in sorted(cnt, key=lambda k: -cnt[k])
                       if c != 0x000][:14]
    bglut = {c: n for n, c in enumerate(sorted(bgpal))}
    bgorder = sorted(bgpal)
    bg_cells = {}
    for j in range(Wc):
        for r in range(Hc):
            ch = enc_cell16(qbg[r*CELL:(r+1)*CELL, j*CELL:(j+1)*CELL],
                            bgorder, bglut)
            bg_cells[(j, r)] = BASE + tile_id(ch)
    black_ch = enc_cell16(np.zeros((CELL, CELL, 3), int), bgorder, bglut)
    black_code = BASE + tile_id(black_ch)

    # palette block for the scene: pal 0 = bg palette (+ black at pen of 0)
    pb = bytearray()
    words = [0xF000 | (0 if v == 1 else v)
             for v in ((c >> 8) << 8 | 0 for c in [])]  # (built below)
    def pal_words(order):
        ws = []
        for c in order:
            r, g, b = (c >> 8) & 15, (c >> 4) & 15, c & 15
            r, g, b = [0 if v == 1 else v for v in (r, g, b)]
            ws.append(0xF000 | (r << 8) | (g << 4) | b)
        ws += [0xF000] * (16 - len(ws))
        return ws
    pb += struct.pack(">16H", *pal_words(bgorder))
    pb += b"\x00" * (1024 - len(pb))


    # ---- FREEZE at the reunion's head.
    # TIMING IS OWNED BY ENDING_ANCHORS_JP (rom.py).  The measured piecewise
    # anchors correct rate distortion inside each line, which a head freeze
    # plus a budget-neutral trim cannot: the E6 attract capture plays each
    # talk section at distorted RATES (Jessica's first line 1.45x slow,
    # Haggar's long section 1.20x fast at the tail), so pinning block STARTS
    # alone leaves syllables drifting inside every line -- user "The
    # reference material's voice movements better align with the syllables
    # spoken than ours."
    #
    # Duration edits DO move the mouth channel: the US set's
    # ENDING_ANCHORS_US (same emap, same engine) measurably moves its blocks.
    # A per-line anchor pass depends on correct anchor data -- a mouth-box
    # scan must not mistake Haggar's third block for Jessica's line.
    _cum = 0
    for _i in range(len(r_script) // 16):
        _cum += struct.unpack_from(">H", r_script, _i*16 + 10)[0]
    print(f"reunion raw span: {_cum} source frames over {n_rev} events "
          f"(anchor pin domain)")

    # ---- script events for the scene.  All of them use base 0 with the
    # base-skip flag (bit 15) -- the fixed BASEMAPS region has no room for
    # another 1120-byte block, and deltas do the same job.
    deltas = bytearray(r_delta)
    script = bytearray(r_script)
    # (user: option 2): reclaim 26 frames from the reunion's
    # final bright hold so the head can run the CD's full cadence.  The
    # tail events are 6f holds of the settled still; shaving 26 is a
    # ~0.4s shorter last pose, and the SCENE START then lands on exactly
    # the same take-over frame as before (reunion-26 + head 76 == the
    # old reunion + gap 50), so the anchors and captions do not move.
    reunion_span = 0
    for _i in range(len(script) // 16):
        reunion_span += struct.unpack_from(">H", script, _i*16 + 10)[0]
    # per-event FADE CODES.  The engine (build_opening_set) reads word-0's
    # top nibble and rewrites the CPS-1 brightness nibble during the per-tick
    # palette staging copy (and the walker's OBJ palette re-assert): code 0 =
    # full, code c = level 15-c.  Zero palette-block cost, and it fades the
    # char palette in step with the pano -- a scaled-block fade instead
    # leaves the char palette bright over a dim pano: striped garbage, the
    # user's "flicker".
    def stamp_fade(i_ev, level):
        off = i_ev * 16
        w0 = struct.unpack_from(">H", script, off)[0]
        struct.pack_into(">H", script, off,
                         (w0 & 0x0FFF) | ((15 - max(0, min(15, level))) << 12))

    # Reunion FADE-OUT (user: "the fade out happens too soon --
    # before the final line").  Trimming 26 frames for the head and fading
    # the last 42 capture frames would land exactly where Jessica's closing
    # line lives (VO capture 1441-1493 vs the trimmed end 1487) -- the scene
    # would fade THROUGH her line.  So the reunion runs its full 1513 (the
    # anchor-pin frame), and her line ends ~31 CPS frames before the scene
    # does; a 4-frame bright beat follows
    # the line and the fade runs AFTER it: 16 capture frames split into
    # 2f slices, 8 descending levels (~25 CPS after the 1.55x anchor
    # stretch).  2f-per-level cadence, not ~6f plateaus -- the plateaus read
    # as steps (user: "looked bad").  The head trims 76 -> 50 frames so the
    # scene start and its anchor-owned captions do not move.
    FADE_OUT_F = 16
    OUT_LEVELS = (13, 11, 9, 7, 5, 3, 2, 1)
    if esnap is not None:
        acc = 0
        i_ev = len(script) // 16 - 1
        while acc < FADE_OUT_F and i_ev >= 0:
            dur = struct.unpack_from(">H", script, i_ev*16 + 10)[0]
            if dur > 2:
                (s_bi, s_pb, s_doff, s_dcnt, _, s_sx,
                 s_sy) = struct.unpack(">HHIHHHH",
                                       script[i_ev*16:(i_ev+1)*16])
                pieces = [2] * (dur // 2)
                if dur % 2:
                    pieces[0] += 1
                repl = bytearray()
                for k, d in enumerate(pieces):
                    repl += struct.pack(">HHIHHHH", s_bi, s_pb,
                                        s_doff if k == 0 else 0,
                                        s_dcnt if k == 0 else 0x8000,
                                        d, s_sx, s_sy)
                script[i_ev*16:(i_ev+1)*16] = repl
            acc += dur
            i_ev -= 1
        n_ev = len(script) // 16
        for k, lvl in enumerate(OUT_LEVELS):
            stamp_fade(n_ev - len(OUT_LEVELS) + k, lvl)
        # the split ADDED reunion events -- everything downstream that
        # counts them (gc_first_evt!) must see the new total.  's
        # first build missed this: the walker armed 6 events early (a
        # second sprite Cody over the head's flattened one) and ran out 6
        # events early (both characters parked mid-fade -- "guy
        # disappears before the scene fades out", user)
        n_rev = n_ev

    # gap event: black the whole 64-column map (the scene paints all of it,
    # so the tail has to clear all of it too).  this event is now
    # actually APPENDED -- built the deltas but never wired them
    # to a record; the "gap duration" patch hijacked the last reunion event
    # instead, so the reunion art was never cleared under the head.  Fade
    # code 15 (level 0): the palette-block switch happens while black.
    gap = bytearray()
    for i in range(64 * ROWS):
        col, row = divmod(i, ROWS)
        gap += struct.pack(">3H", cell_off(COL0 + col, ROW0 + row),
                           black_code, 0)
    d0, dc = len(deltas), len(gap) // 6
    deltas += gap
    gap_dur = (SEAM_F - HEAD_F) if esnap is not None else GAP_F
    script += struct.pack(">HHIHHHH", 0xF000, n_rpb, d0, dc | 0x8000,
                          gap_dur, SX0, SY0)
    # ---- HEAD (user: "the guy/cody scene is missing the first
    # few frames that show cody looking at the camera as it fades in --
    # something only available from the VM").  The CD fades the two-shot
    # IN from black with Cody facing the camera, then two head-turn beats
    # settle into the pose the layered scene starts with.  Sourced from
    # the VM E-sweep esnap (native frames: VM f1740-1758 fade, beats
    # B0=1760 camera / B1=1788 turning / B2=1804 near-settled; esnap =
    # VM-136).  Budget: GAP_F=50 (the 26-frame trim goes to the reunion so
    # the fade-out runs AFTER Jessica's closing line), so the scene events
    # (and the anchor-owned captions) do not move.  The fade-in is
    # ENGINE-side (per-event fade codes over ONE bright palette block) at
    # 2f-per-level cadence -- ~6f plateaus read as steps: 2f gap black + B0
    # paint @level 0 (2f, cells land invisibly) + 8 fade steps x2f + B0
    # bright hold 12f + B1 9f + B2 9f = 50.  Cells use pal 0 (bgorder) where they match
    # the pano and pal 1 (a head-char palette) elsewhere.
    n_head_ev = 0
    head_pb = bytearray()
    if esnap is not None:
        B0, B1, B2 = 1624, 1652, 1668          # esnap frame numbers
        def head_img(fn):
            im = Image.open(esnap / f"f{fn:06d}.png").convert("RGB")
            return quant12(np.asarray(im).astype(int))
        # head palette: bgorder (pal 0) + a char palette (pal 1) from the
        # pixels of the bright beats that the pano does not explain
        beats = {f: head_img(f) for f in (B0, B1, B2)}
        # visible window under the scene's opening sx: the track's first
        # hscroll decides which pano columns show; the head frames are
        # SCREEN images, so paint screen cell c at map col (c + hsB0//16)
        hsB0 = track[frames[0]][1]
        assert hsB0 % CELL == 0, f"head needs cell-aligned first hscroll, got {hsB0}"
        col0 = hsB0 // CELL
        # ONE shared char palette could not cover both
        # characters AND the wall decoration -- measured 15-26 mean abs
        # error per channel on their cells (user: mismatched blocks,
        # fade or no fade).  The head block has 30 unused slots, so the
        # non-pano cells get THREE palettes: the walker's own per-
        # character palettes (the scene's approved quality) and a
        # leftovers palette built from whatever neither covers well.
        def mk_order(cols):
            cols = [0x000] + [c for c in cols if c != 0x000][:14]
            order = sorted(set(cols))
            return order, {c: n for n, c in enumerate(order)}
        p_cody = mk_order(cpal)
        p_guy = mk_order(gpal)
        cnt2 = {}
        for f, q in beats.items():
            band = q[32:32 + Hc*CELL]
            for r_, g_, b_ in band.reshape(-1, 3):
                k = (int(r_) << 8) | (int(g_) << 4) | int(b_)
                if k not in bglut and k not in p_cody[1] and k not in p_guy[1]:
                    cnt2[k] = cnt2.get(k, 0) + 1
        p_rest = mk_order(sorted(cnt2, key=lambda k: -cnt2[k]))

        def pal_err(cell, order):
            """squared quantization error of encoding cell against order"""
            arr = np.array(order)
            pr, pg, pb_ = arr >> 8 & 15, arr >> 4 & 15, arr & 15
            cr = cell[..., 0][..., None]
            cg = cell[..., 1][..., None]
            cb = cell[..., 2][..., None]
            d = (pr - cr) ** 2 + (pg - cg) ** 2 + (pb_ - cb) ** 2
            return float(d.min(axis=-1).sum())

        # (user: "a weird outline around guy").  MIXED cells --
        # background plus a sliver of character -- cannot be covered by
        # any single shared palette: whichever half loses the palette
        # pick renders through the other's colours, a halo tracing the
        # silhouettes.  But a 16x16 cell rarely holds >15 distinct
        # quantized colours, so cells the fixed palettes cover poorly
        # get DEDICATED palettes (greedily union-merged while they fit),
        # the same own-palette escape the farewell's patch sprites use.
        fixed = [sorted(bgpal), p_cody[0], p_guy[0], p_rest[0]]
        extras = []                    # mutable colour sets -> pals 4..
        ERR_OK = 256.0                 # <=1.0 sq err/px: quantizer noise
        alloc = {}                     # (beat, col, row) -> palette index
        beat_cells = {}
        for f, q in beats.items():
            for c in range(COLS):
                pano_j = (col0 + c) % Wc
                for r in range(Hc):
                    cell = q[32 + r*CELL:32 + (r+1)*CELL,
                             c*CELL:(c+1)*CELL]
                    pano_cell = qbg[r*CELL:(r+1)*CELL,
                                    pano_j*CELL:(pano_j+1)*CELL]
                    if np.array_equal(cell, pano_cell):
                        continue
                    beat_cells[(f, c, r)] = cell
                    e, i = min((pal_err(cell, o), i)
                               for i, o in enumerate(fixed))
                    if e <= ERR_OK:
                        alloc[(f, c, r)] = i
                        continue
                    hist = {}
                    for r_, g_, b_ in cell.reshape(-1, 3):
                        k2 = (int(r_) << 8) | (int(g_) << 4) | int(b_)
                        hist[k2] = hist.get(k2, 0) + 1
                    cols_ = set(sorted(hist, key=lambda k2: -hist[k2])[:15])
                    for j, ep in enumerate(extras):
                        if len(ep | cols_) <= 15:
                            ep |= cols_
                            alloc[(f, c, r)] = 4 + j
                            break
                    else:
                        extras.append(set(cols_))
                        alloc[(f, c, r)] = 4 + len(extras) - 1
        assert 4 + len(extras) <= 32, f"head wants {4+len(extras)} palettes"
        pals = fixed + [sorted(ep) for ep in extras]
        luts = [{c2: n for n, c2 in enumerate(o)} for o in pals]
        print(f"head palettes: 4 fixed + {len(extras)} dedicated; "
              f"{len(alloc)} non-pano cells")
        def head_cells(f, q):
            """art-window cells: (map_col, row, code, pal) -- exact pano
            matches reuse the pano tile at pal 0, everything else uses
            its allocated palette from the pass above."""
            out_cells = []
            for c in range(COLS):
                pano_j = (col0 + c) % Wc
                for r in range(Hc):
                    key = (f, c, r)
                    if key in alloc:
                        pi = alloc[key]
                        code = BASE + tile_id(
                            enc_cell16(beat_cells[key], pals[pi], luts[pi]))
                        pal = pi
                    else:
                        code, pal = bg_cells[(pano_j, r)], 0
                    out_cells.append(((COL0 + col0 + c) & 0x3F,
                                      ROW0 + ART_ROW0 + r, code, pal))
            return out_cells
        b0_cells = head_cells(B0, beats[B0])
        b1_cells = head_cells(B1, beats[B1])
        # B2 stays FLATTENED (chars in the cells): measured, the walker's
        # sprites only appear at the SCENE's first event however early it
        # is armed, so the head must carry the characters in scroll2 to
        # its last frame.  The scene's first event then PRESERVES these
        # char cells (see hold_cells below) and event 1 restores the pano
        # under the by-then-present sprites -- a <=2-frame overlay of
        # near-identical poses instead of a bg-only flash.
        b2_cells = head_cells(B2, beats[B2])
        head_hold = {(col, row) for col, row, code, pal in b2_cells
                     if pal != 0}
        # held cells reference their palettes ACROSS the block switch to
        # the scene's palette (whose non-zero slots were zeros -- they
        # rendered black for 2 frames, measured): mirror EVERY head
        # palette into the scene block's matching slot
        for n, o in enumerate(pals[1:], start=1):
            pb[n*32:(n+1)*32] = struct.pack(">16H", *pal_words(o))
        # ONE bright palette block: all palettes at full
        # value; every dim step is the engine fade, not a stored variant
        hblk = bytearray()
        for o in pals:
            hblk += struct.pack(">16H", *pal_words(o))
        hblk += b"\x00" * (1024 - len(hblk))
        head_pb += hblk
        HB = n_rpb + 1                     # head palblock index
        head_sx = SX0 - hsB0
        def head_event(cells, dur, level=15):
            nonlocal_deltas = bytearray()
            for col, row, code, pal in cells:
                nonlocal_deltas += struct.pack(">3H", cell_off(col, row),
                                               code, pal)
            d0_, dc_ = len(deltas), len(nonlocal_deltas) // 6
            deltas.extend(nonlocal_deltas)
            return struct.pack(">HHIHHHH", (15 - level) << 12, HB, d0_,
                               dc_ | 0x8000, dur, head_sx, SY0)
        # (the 2f gap event above holds black; cells land while level 0)
        # --head-nofade: debug knob -- play the head at full brightness
        # with no fade-in, to inspect the content in isolation
        nofade = HEAD_NOFADE
        script += head_event(b0_cells, 2, level=15 if nofade else 0)
        for lvl in (1, 2, 3, 5, 7, 9, 11, 13):         # fade-in, 16f @2f/level
            script += head_event([], 2, level=15 if nofade else lvl)
        script += head_event([], 12)                   # camera hold, bright
        script += head_event(b1_cells, 9)
        script += head_event(b2_cells, 9)
        n_head_ev = 12
        print(f"head: {n_head_ev} events, {len(head_pb)//1024} palblock, "
              f"tiles now {len(tiles)//128}")
    # first scene event paints the pano into the art rows and black into the
    # letterbox rows, across ALL 64 map columns.  The pano is exactly 32
    # cells wide and wraps, so two copies tile the whole scroll2 map and any
    # sx is then valid.  Painting only a window around COL0 left the columns
    # the leftward pan swings into unpainted, and the end of the scene
    # revealed darkness instead of more corridor.
    chunk = bytearray()
    restore = bytearray()
    hh = head_hold if esnap is not None else set()
    for j in range(64):
        col = (COL0 + j) & 0x3F
        for r in range(ROWS):
            if ART_ROW0 <= r < ART_ROW0 + Hc:
                code = bg_cells[(j % Wc, r - ART_ROW0)]
            else:
                code = black_code
            packed = struct.pack(">3H", cell_off(col, ROW0 + r), code, 0)
            if (col, ROW0 + r) in hh:
                restore += packed     # held for event 1 (sprites present)
            else:
                chunk += packed
    ev_rows = []
    f_prev = None
    sprevt = []
    for f in frames:
        if f_prev is not None and f - f_prev < FPS_STEP:
            continue
        f_prev = f
        ev_rows.append(f)
    for k, f in enumerate(ev_rows):
        hsA, hsB, cc, cx, cy, gc, gx, gy = track[f]
        dur = (ev_rows[k+1] - f) if k+1 < len(ev_rows) else 4
        sx = SX0 - hsB
        if k == 0:
            d0, dc = len(deltas), len(chunk) // 6
            deltas += chunk
        elif k == 3 and restore:
            # measured: the walker's sprites reach the screen ~2 events
            # after gc_first_evt; restore the pano under them only once
            # they are provably present (k=3 = ~6 frames; the pan moves
            # 0.5px/f, so the held flattened pose underneath is
            # imperceptible)
            d0, dc = len(deltas), len(restore) // 6
            deltas += restore
        else:
            d0, dc = len(deltas), 0
        script += struct.pack(">HHIHHHH", 0, n_rpb, d0,
                              dc | 0x8000, dur, sx, SY0)
        # sprite anchors, OBJ coordinate space
        def spr(tag, ci, ax, ay, twin, extra_dy):
            if ci < 0:
                return (0, 0, 0xFFFF)
            ci = twin.get(ci, ci)
            if (tag, ci) not in cel_key:
                return (0, 0, 0xFFFF)
            return ((ax + OBJ_X) & 0xFFFF, (ay + extra_dy + OBJ_Y) & 0xFFFF,
                    cel_key[(tag, ci)])
        sprevt.append(spr("cody", cc, cx, cy, ctwin, 0)
                      + spr("guy", gc, gx, gy, gtwin, SPR_DY))
    # Scene FADE-OUT: the CD fades the whole conversation to
    # black over ~16-20 frames at its end (jp_end sweep f2700-2716); the
    # shipped scene hard-cut the background off in 2 frames.  Stamp
    # descending levels over the last ~20 capture frames of scene events
    # (2f rows); the engine scales the walker's OBJ palettes by the same
    # code, so the characters fade WITH the background.
    SCENE_FADE_F = 20
    acc = 0
    i_ev = len(script) // 16 - 1
    while acc < SCENE_FADE_F and i_ev >= 0:
        dur = struct.unpack_from(">H", script, i_ev*16 + 10)[0]
        stamp_fade(i_ev, max(1, min(14, round(15 * (acc + dur/2)
                                              / SCENE_FADE_F))))
        acc += dur
        i_ev -= 1
    # trailing black to preserve the take-over's length; fade code 15
    # keeps the OBJ palettes dark too -- unscaled, the walker's characters
    # stayed lit over this black for its whole 133 frames
    # this MUST follow gap_dur.  It was hardcoded `2 + 48`, so
    # when widened the black gap to the disc's seam the budget did
    # not see it: used_f stayed put, the tail did not shrink, and the extra
    # 52 frames were appended to the take-over instead -- moving the handback
    # and dragging ENGINE3, the CODY! card and the farewell 52 frames later
    # against real-time audio (review: "voice/caption drift in the later
    # 'CODY!' and cody/jessica scene").  TOTAL is held; the trailing black
    # pays for the seam.
    pre_scene = (gap_dur + 48) if esnap is not None else GAP_F
    used_f = pre_scene + sum((ev_rows[k+1] - ev_rows[k])
                             for k in range(len(ev_rows)-1)) + 4
    # the take-over grows with the reunion window.  The raw tail follows the
    # modeled sweep's esnap, which runs longer than the e6 capture's
    # compressed pacing, so the engine handback frame stays fixed under the
    # anchors' tail pin (reunion_span -> 1818).  At a 1513-frame reunion the
    # expression is exactly TOTAL (2560).
    total_eff = TOTAL - 1513 + reunion_span
    total_scene = total_eff - reunion_span
    tail = total_scene - used_f
    d0 = len(deltas)
    gap2 = bytearray()
    for i in range(64 * ROWS):
        col, row = divmod(i, ROWS)
        gap2 += struct.pack(">3H", cell_off(COL0 + col, ROW0 + row),
                            black_code, 0)
    dc = len(gap2) // 6
    deltas += gap2
    script += struct.pack(">HHIHHHH", 0xF000, n_rpb, d0, dc | 0x8000,
                          max(1, tail), SX0, SY0)
    print(f"scene: {len(ev_rows)} events + gap + tail({tail}f)")

    # ---- write the merged conv
    (out / "tiles.bin").write_bytes(bytes(tiles))
    (out / "palblocks.bin").write_bytes(r_pal + bytes(pb) + bytes(head_pb))
    (out / "basemaps.bin").write_bytes(r_base)
    (out / "deltas.bin").write_bytes(bytes(deltas))
    (out / "script.bin").write_bytes(bytes(script))
    n_ev = len(script) // 16
    man = dict(rman)
    man.update(unique_tiles=len(tiles)//128, tile_bytes=len(tiles),
               events=n_ev, total_frames=total_eff,
               pal_blocks=n_rpb + 1 + len(head_pb) // 1024,
               gc_first_evt=n_rev + 1 + n_head_ev,
               gc_n_evt=len(ev_rows),
               gc_max_cells=maxc, gc_obj_pals=2)
    (out / "manifest.json").write_text(json.dumps(man, indent=2))
    with open(out / "script.tsv", "w") as f:
        f.write("# base_block\tpal_block\tdelta_off\tdcnt_flag\tframes\tsx\tsy\n")
        for i in range(0, len(script), 16):
            r = struct.unpack(">HHIHHHH", script[i:i+16])
            f.write("\t".join(str(v) for v in r) + "\n")

    # ---- gcspr.bin: CELOFF, CELTAB, SPREVT, OBJPALS (offsets in header)
    celtab = bytearray()
    celoff = []
    for ents in cel_entries:
        celoff.append(len(celtab))
        celtab += be16(len(ents))
        for dx, dy, code, opi in ents:
            celtab += be16(dx) + be16(dy) + be16(code) + be16(opi)
    sprblob = bytearray()
    for e in sprevt:
        for v in e:
            sprblob += be16(v)
    opals = bytearray()
    for pal in (cpal, gpal):
        ws = [0xF000]
        for c in pal:
            r, g, b = (c >> 8) & 15, (c >> 4) & 15, c & 15
            r, g, b = [0 if v == 1 else v for v in (r, g, b)]
            ws.append(0xF000 | (r << 8) | (g << 4) | b)
        ws += [0xF000] * (16 - len(ws))
        opals += struct.pack(">16H", *ws)
    hdr = struct.pack(">4H", 8, 8 + len(celoff)*2,
                      8 + len(celoff)*2 + len(celtab),
                      8 + len(celoff)*2 + len(celtab) + len(sprblob))
    blob = bytearray(hdr)
    for o in celoff:
        blob += be16(o)
    blob += celtab + sprblob + opals
    (out / "gcspr.bin").write_bytes(bytes(blob))
    print(f"gcspr.bin: {len(blob)} B "
          f"(celtab {len(celtab)}, sprevt {len(sprblob)}, pals {len(opals)})")
    print(json.dumps({k: man[k] for k in
                      ("unique_tiles", "tile_bytes", "events",
                       "gc_first_evt", "gc_n_evt", "gc_max_cells")}, indent=1))
    return 0


# ======== farewell ======================================================


def _farewell(a) -> int:
    f0, f1 = (int(v) for v in a.window.split("-"))
    a.outdir.mkdir(parents=True, exist_ok=True)
    megadrive.RAMP = megadrive.MAME_RAMP

    p = ScriptPlayer(region=a.region, bundle="E")
    last = None
    base = 0
    cam = []
    n = 0
    for f in range(f1 + 1):
        p.vblank()
        try:
            p.tick()
        except Exception as e:                            # noqa: BLE001
            print(f"f{f}: VM stopped: {e}")
            break
        if f < f0:
            # Before the window only the plane-B vscroll matters, and a
            # full snapshot unpacks 32768 VRAM words per frame to get it.
            # vsram[1] is the second big-endian word of the vsram bytes.
            vsr = p.bus.vdp.vsram
            v = megadrive.vs((vsr[2] << 8) | vsr[3])
        else:
            vram, cram, regs, vsram = p.snapshot()
            v = megadrive.vs(vsram[1])
        if last is not None:                              # unwrap the wrap
            d = v - last
            if d > 128:
                base -= 256
            elif d < -128:
                base += 256
        last = v
        if f < f0:
            continue
        cam.append((f, -(v + base)))
        megadrive.render_np(None, vscroll=(megadrive.vs(vsram[0]), v),
                           state=(vram, cram, regs, None)).save(
            a.outdir / f"f{f:06d}.png", compress_level=1)
        n += 1
    if not cam:
        raise SystemExit("no frames in the window -- did the VM stop early?")
    b0 = cam[0][1]
    with open(a.outdir / "camera.tsv", "w") as fh:
        fh.write("# frame\tcamera_y (plane-B vscroll, unwrapped; CD-SOURCED)\n")
        for f, s in cam:
            fh.write(f"{f}\t{s - b0}\n")
    print(f"{a.outdir}: {n} frames f{f0}-{f1}, camera "
          f"{cam[0][1]-b0}..{max(s for _f, s in cam)-b0} px")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="scene", required=True)
    p = sub.add_parser("esnaps",
                       help="slice the reunion out of the ending sweep")
    p.add_argument("--root", default=None,
                   help="intermediates root (default work/build/vmsweep)")
    p = sub.add_parser("layers", help="export the JP pillar-hall scene as layers")
    p.add_argument("outdir"); p.add_argument("region", nargs="?", default="jp", choices=("jp",))
    p.add_argument("window", nargs="*", type=int)
    p.add_argument("--dump", action="store_true")
    p.add_argument("--phase", type=float, default=None)
    p.add_argument("--frame-off", type=int, default=None)
    p = sub.add_parser("merge", help="fold the layer set into the reunion conv")
    p.add_argument("gdir", type=Path); p.add_argument("rconv", type=Path)
    p.add_argument("out", type=Path); p.add_argument("esnap", type=Path, nargs="?", default=None)
    p.add_argument("--head-nofade", action="store_true")
    p = sub.add_parser("farewell", help="render the CD-6 pan from the VM")
    p.add_argument("outdir", type=Path); p.add_argument("region", choices=("jp", "us"))
    p.add_argument("--window", required=True)
    a = ap.parse_args()
    return {"esnaps": _esnaps, "layers": _layers,
            "merge": _merge, "farewell": _farewell}[a.scene](a)


if __name__ == "__main__":
    raise SystemExit(main())
