#!/usr/bin/env python3
"""Stage 3 converter: the full opening as one engine-ready dataset.

Consumes a 1-frame sweep of the whole opening plus the shot table, and
emits the engine data:

    tiles.bin      global dedup 16x16 chunky tiles (code base 0x4000+)
    palblocks.bin  N x 32-palette blocks (1 KB each) -- one per shot, plus
                   fade-step variants (<=4 steps per fade run)
    basemaps.bin   per shot: 280 x (code,attr) full map of the shot's anchor
    deltas.bin     per event: cells differing from the shot base map
    script.bin     12-byte event records:
                   u16 shot, u16 pal_block, u16 delta_off/6, u16 delta_count,
                   u16 duration, u16 flags
    manifest.json  stats; script.tsv human-readable

Stage 3 scope rules (PLAN.md):
  - pan shots (SHOT_HOLD) hold their anchor cel for the shot's full span --
    scroll-register panning is Stage 4;
  - fade frames (image ~= scalar * nearest held cel) become palette-step
    events (delta_count 0, dedicated pal block), collapsed to <= 4 steps per
    run; non-multiplicative transitions (wipes) stay real cels;
  - every event's delta is against the SHOT BASE map, so the engine can do
    a full ROM-driven redraw every tick (base + delta) and win every race.

Run under the repo venv:
    venv/bin/python convert.py <snapdir> <shots.tsv> <outdir>
        [--window 2340-9700] [--crop 320x224]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
from PIL import Image

CELL = 16
COLS, ROWS = 20, 14
BASE_CODE = 0x4000
ROW0, COL0 = 0x11, 0x26
# shots held as stills in Stage 3 (pan/scroll shots, FEASIBILITY table)
SHOT_HOLD_DEFAULT = "pan"


def quant12(img: np.ndarray) -> np.ndarray:
    q = np.rint(img.astype(np.float32) * 15.0 / 255.0).astype(np.int16)
    src = img.astype(np.int16)
    q = np.where((q == 1) & (src >= 26), 2, q)
    q = np.where(q == 1, 0, q)
    return q.astype(np.uint8)


def enc_cell(cell, pal_order, lut, pi):
    """Encode one quantised 16x16 cell with palette pi.

    Returns (chunky bytes, rendered rgb) so a caller can measure what the
    hardware would actually show -- the refresh pass needs the RENDERED
    error, not the source error, to decide whether a repaint helps.
    """
    pal = pal_order[pi]
    pens = np.zeros((CELL, CELL), dtype=np.uint8)
    rgb = np.zeros((CELL, CELL, 3), dtype=np.uint8)
    for y in range(CELL):
        for x in range(CELL):
            r, g, b = (int(v) for v in cell[y, x])
            c = (r << 8) | (g << 4) | b
            if c not in lut[pi]:
                c = min(pal, key=lambda pc: ((pc >> 8) - r) ** 2
                        + (((pc >> 4) & 15) - g) ** 2 + ((pc & 15) - b) ** 2)
            pens[y, x] = lut[pi][c]
            rgb[y, x] = (((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17,
                         (c & 15) * 17)
    ch = bytearray(128)
    for y in range(CELL):
        for kx in range(8):
            ch[y * 8 + kx] = ((int(pens[y, 2 * kx]) << 4)
                              | int(pens[y, 2 * kx + 1]))
    return bytes(ch), rgb


def cell_offsets() -> list[int]:
    offs = []
    for cy in range(ROWS):
        for cx in range(COLS):
            row, col = ROW0 + cy, COL0 + cx
            idx = (row & 0x0F) + ((col & 0x3F) << 4) + ((row & 0x30) << 6)
            offs.append(idx * 4)
    return offs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapdir", type=Path)
    ap.add_argument("shots_tsv", type=Path)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--window", default="2340-9700")
    ap.add_argument("--crop", default="320x224")
    ap.add_argument("--base-code", type=lambda v: int(v, 0), default=0x4000,
                    help="first tile code (default 0x4000; the ending art "
                         "converts at 0x4000 + opening tile count so both "
                         "can share the gfx region without rebasing)")
    ap.add_argument("--fade-steps", type=int, default=4)
    ap.add_argument("--refresh-shots", default="",
                    help="shot indices whose PANORAMA also gets a per-cell "
                         "refresh.  A panorama assumes the world is static "
                         "and only the camera moves; where characters also "
                         "move (the JP Guy/Cody scene: they swap sides and "
                         "Cody walks out) they would otherwise stay pinned "
                         "to the sliding background.  OFF by default -- "
                         "blanket refresh makes the opening's lineup cells "
                         "flip state as the scroll advances.")
    ap.add_argument("--black", default="",
                    help="frame ranges 'A-B[,C-D]' to replace with BLACK, "
                         "keeping their duration.  Use for inter-scene fades: "
                         "the CD's hue ramps (green in / yellow out) are not "
                         "scalar fades, so the fade-step collapse misses them "
                         "and each step lands as a screenful of fresh tiles -- "
                         "1695 tiles instead of 178 on the JP reunion alone.")
    ap.add_argument("--hold-shots", default="",
                    help="comma-separated shot numbers held as stills (pans)")
    ap.add_argument("--cue", action="append", default=[],
                    help="T:HEX -- emit sound cue HEX when the event covering"
                         " script-frame T loads (packed in pal high byte)")
    args = ap.parse_args()
    global BASE_CODE
    BASE_CODE = args.base_code
    hold_shots = {int(v) for v in args.hold_shots.split(",") if v.strip()}
    refresh_shots = {int(v) for v in args.refresh_shots.split(",") if v.strip()}
    f0, f1 = (int(v) for v in args.window.split("-"))
    w, h = (int(v) for v in args.crop.split("x"))

    # ---- shots: [(f_start, f_end, kind)]
    shots = []
    for line in args.shots_tsv.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        n, fs, fe = int(parts[0]), int(parts[1]), int(parts[2])
        shots.append([fs, fe, "pan" if n in hold_shots else ""])
    shots.sort()

    def shot_of(frame: int) -> int:
        # a shot owns everything from its start to the next shot's start,
        # so transition frames living in the 10-frame-sweep gaps attach to
        # the preceding shot instead of pooling into a fake shot
        best = 0
        for i, (fs, _fe, _) in enumerate(shots):
            if fs <= frame:
                best = i
        return best

    # ---- pass 1: cel runs over the window
    runs = []          # [md5, f_start, f_end]
    prev = None
    imgs: dict[str, np.ndarray] = {}
    blackout = []
    for spec in (r for r in args.black.split(",") if r.strip()):
        a, b = (int(v) for v in spec.split("-"))
        blackout.append((a, b))
    for fr in range(f0, f1 + 1):
        p = args.snapdir / f"f{fr:06d}.png"
        if not p.exists():
            continue
        if any(a <= fr <= b for a, b in blackout):
            img = np.zeros((h, w, 3), np.uint8)
        else:
            img = np.asarray(Image.open(p).convert("RGB"))[:h, :w]
        hsh = hashlib.md5(img.tobytes()).hexdigest()[:12]
        if hsh != prev:
            runs.append([hsh, fr, fr])
            if hsh not in imgs:
                imgs[hsh] = img
            prev = hsh
        else:
            runs[-1][2] = fr

    # ---- classify: held cels, fades, wipes; apply Stage 3 shot rules
    held = {}          # md5 -> True for runs >= 3 frames
    for md5, a, b in runs:
        if b - a + 1 >= 3:
            held[md5] = True

    def palette_variant(img, ref, force=False):
        """Colour bijection ref->img if img is ref with remapped colours.

        Covers fades AND hue pulses (the map scene oscillates red/green,
        which is not a scalar fade).  Returns {ref_rgb12: img_rgb12} or
        None.  Requires >=99.5% of pixels to obey the majority mapping
        (both directions), unless force=True, which returns the majority
        mapping unconditionally -- used for degenerate cels whose delta
        quantized to zero cells, where palette is the only lever left.
        """
        r = quant12(ref).reshape(-1, 3)
        i = quant12(img).reshape(-1, 3)
        rk = (r[:, 0].astype(np.int32) << 8) | (r[:, 1] << 4) | r[:, 2]
        ik = (i[:, 0].astype(np.int32) << 8) | (i[:, 1] << 4) | i[:, 2]
        mapping = {}
        ok = 0
        order = np.argsort(rk, kind="stable")
        rs, is_ = rk[order], ik[order]
        bounds = np.flatnonzero(np.diff(rs)) + 1
        for grp in np.split(np.arange(len(rs)), bounds):
            vals, counts = np.unique(is_[grp], return_counts=True)
            m = vals[counts.argmax()]
            mapping[int(rs[grp[0]])] = int(m)
            ok += int(counts.max())
        if not force and ok / len(rk) < 0.995:
            return None
        if force:
            return mapping
        # inverse consistency: a wipe frame (many ref colours collapsing
        # onto few) passes the forward test but not this one
        inv_ok = 0
        order2 = np.argsort(ik, kind="stable")
        is2, rs2 = ik[order2], rk[order2]
        bounds2 = np.flatnonzero(np.diff(is2)) + 1
        for grp in np.split(np.arange(len(is2)), bounds2):
            vals, counts = np.unique(rs2[grp], return_counts=True)
            inv_ok += int(counts.max())
        if inv_ok / len(ik) < 0.995:
            return None
        # coherent-content guard: pixels defying the mapping must be
        # scattered noise (dither), not a solid object.  A 200 px sliver
        # sliding out against black is 99.7% "consistent with black" by
        # fraction, but its outliers are spatially clustered -- refusing
        # those keeps real content out of the erase path.
        mapped = np.array([mapping[c] for c in rk], dtype=np.int32)
        out2d = (ik != mapped).reshape(ref.shape[0], ref.shape[1])
        # adjacency in BOTH directions: a mouth flap's outliers are thin
        # HORIZONTAL lip strips (few vertical pairs) -- vertical-only
        # clustering let it pass as a palette variant (frozen mouth)
        clustered = int((out2d[1:, :] & out2d[:-1, :]).sum()) \
            + int((out2d[:, 1:] & out2d[:, :-1]).sum())
        # threshold 8: thin-line mouth cels produce as few as ~15 adjacent
        # pairs; true dither noise is isolated pixels (near zero pairs)
        if clustered > 8:
            return None
        return mapping

    MOTION_STILL = 30   # non-scroll motion over this many frames holds a still

    def shift_of(a, b, maxdx=8):
        """Per-frame translation a->b (content moving left = camera right)."""
        best = None
        aa, bb = a.astype(np.int16), b.astype(np.int16)
        for dx in range(0, maxdx + 1):
            d = (np.abs(aa[:, dx:] - bb[:, :aa.shape[1] - dx]).mean()
                 if dx else np.abs(aa - bb).mean())
            if best is None or d < best[0]:
                best = (d, dx)
        return best  # (err, dx)

    events = []        # dicts: kind in cel/palvar/scroll, plus fields
    pending = 0
    pending_runs = []  # [(md5, frames)]

    def is_blend(f, pimg, nimg):
        """frame ~= a*prev + (1-a)*next -- true for dissolves/crossfades.

        Error is evaluated over the CONTENT region (pixels differing from
        the next cel) only: a 20 px sliver sliding out against black
        otherwise dilutes to nothing against a whole-frame mean and gets
        merged away.
        """
        ff = f.astype(np.float32)
        pp = pimg.astype(np.float32)
        nn = nimg.astype(np.float32)
        mask = np.abs(ff - nn).sum(-1) > 40
        if not mask.any():
            return True                     # identical to next: merge freely
        d = (pp - nn)[mask]
        fv = (ff - nn)[mask]
        den = float((d * d).sum())
        a = 0.5 if den < 1e-3 else float((fv * d).sum() / den)
        a = min(1.0, max(0.0, a))
        err = float(np.abs(fv - a * d).mean())
        return err < 12.0

    def flush_motion(sh, next_md5=None):
        nonlocal pending, pending_runs
        if not pending:
            return
        # translation run?  measure consecutive shifts across the cluster
        if len(pending_runs) >= 8:
            shifts = []
            ok = True
            for k in range(len(pending_runs) - 1):
                err, dx = shift_of(imgs[pending_runs[k][0]],
                                   imgs[pending_runs[k + 1][0]])
                shifts.append((err, dx, pending_runs[k][1]))
            good = [s for s in shifts if s[0] < 2.0 and s[1] > 0]
            if len(good) >= 0.8 * len(shifts):
                events.append(dict(shot=sh, kind="scroll",
                                   runs=list(pending_runs)))
                pending = 0
                pending_runs = []
                return
        hold_shot = 0 <= sh < len(shots) and shots[sh][2] == SHOT_HOLD_DEFAULT
        if pending >= MOTION_STILL and hold_shot:
            # representative = the most content-rich frame: the map's pan
            # cluster ENDS in its fade-to-black, so last-frame reps held a
            # near-black screen for the whole span (user: "map is messed up")
            rep = max(pending_runs,
                      key=lambda r: float((imgs[r[0]].sum(-1) > 30).mean()))[0]
            events.append(dict(shot=sh, kind="cel", md5=rep, frames=pending))
        elif pending >= MOTION_STILL:
            burst = list(pending_runs)
            if events and events[-1].get("kind") == "scroll":
                # No-fade LINEUP exit: drop dim tail frames;
                # hold the last bright sliver for their duration, cut.
                b0 = float(imgs[burst[0][0]].astype(np.float32).mean())
                kept, dropped = [], 0
                for md5b, frb in burst:
                    v = float(imgs[md5b].astype(np.float32).mean())
                    if v >= 0.85 * b0 or not kept:
                        kept.append([md5b, frb])
                    else:
                        dropped += frb
                if dropped and kept:
                    kept[-1][1] += dropped
                burst = kept
            for md5b, frb in burst:
                events.append(dict(shot=sh, kind="cel", md5=md5b, frames=frb))
        elif events and pending_runs:
            # short cluster: merge ONLY if it is a genuine blend between the
            # neighbouring cels (dissolve); content-bearing tails (e.g. the
            # lineup's last 20 px sliding out against black) must play
            prev_md5 = next((e.get("md5") for e in reversed(events)
                             if e.get("md5")), None)
            samples = pending_runs[::max(1, len(pending_runs) // 3)][:3]
            blend = (prev_md5 is not None and next_md5 is not None
                     and all(is_blend(imgs[m], imgs[prev_md5],
                                      imgs[next_md5]) for m, _ in samples))
            if blend and pending <= 6:
                # imperceptible splice; anything longer is an AUTHORED fade
                # or dissolve and must play (the lineup's fade-out was being
                # swallowed here -- user )
                events[-1]["frames"] += pending
            else:
                for md5b, frb in pending_runs:
                    events.append(dict(shot=sh, kind="cel", md5=md5b,
                                       frames=frb))
        elif events:
            events[-1]["frames"] += pending
        pending = 0
        pending_runs = []

    anchor = {}        # shot -> md5 of the current structural anchor
    i = 0
    while i < len(runs):
        md5, a, b = runs[i]
        sh = shot_of(a)
        frames = b - a + 1
        if md5 in held:
            flush_motion(sh, md5)
            # held cels can be palette variants too (e.g. the gang scene's
            # neon pulse holds each step 6-8 frames)
            ref = anchor.get(sh)
            mapping = palette_variant(imgs[md5], imgs[ref]) if ref else None
            if mapping is not None:
                events.append(dict(shot=sh, kind="palvar", md5=ref,
                                   mapping=mapping, frames=frames))
            else:
                anchor[sh] = md5
                events.append(dict(shot=sh, kind="cel", md5=md5,
                                   frames=frames))
            i += 1
            continue
        ref = None
        for j in range(i + 1, min(i + 60, len(runs))):
            if runs[j][0] in held:
                ref = runs[j][0]
                break
        if ref is None:
            for j in range(i - 1, max(i - 60, -1), -1):
                if runs[j][0] in held:
                    ref = runs[j][0]
                    break
        mapping = palette_variant(imgs[md5], imgs[ref]) if ref else None
        if mapping is not None:
            flush_motion(sh)
            events.append(dict(shot=sh, kind="palvar", md5=ref,
                               mapping=mapping, frames=frames))
        else:
            pending += frames
            pending_runs.append((md5, frames))
        i += 1
    flush_motion(shot_of(runs[-1][1]) if runs else 0)

    # ---- pass 2+3: sequential palette BLOCKS, tiles, base maps, deltas
    # A block is <=32 palettes serving a run of consecutive cels; a new block
    # starts when the next cel's cells no longer fit (the CD swaps CRAM the
    # same way).  Each block's first cel is its base map; deltas are vs it.
    used_imgs = {}
    for ev in events:
        if ev["kind"] in ("cel", "hold"):
            used_imgs.setdefault(ev["md5"], quant12(imgs[ev["md5"]]))

    def cellsets_arr(q, cols_n):
        out = []
        for cy in range(ROWS):
            for cx in range(cols_n):
                cell = q[cy*CELL:(cy+1)*CELL, cx*CELL:(cx+1)*CELL]
                cols = {(int(r) << 8) | (int(g) << 4) | int(b)
                        for r, g, b in cell.reshape(-1, 3)}
                out.append(cols)
        return out

    def cellsets(md5):
        return cellsets_arr(used_imgs[md5], COLS)

    over15 = 0

    def reduce15(cols):
        nonlocal over15
        if len(cols) <= 15:
            return cols
        over15 += 1
        cl = sorted(cols)
        while len(cl) > 15:
            best = None
            for x in range(len(cl)):
                for y in range(x+1, len(cl)):
                    a2, b2 = cl[x], cl[y]
                    dd = (((a2 >> 8)-(b2 >> 8))**2
                          + (((a2 >> 4) & 15)-((b2 >> 4) & 15))**2
                          + ((a2 & 15)-(b2 & 15))**2)
                    if best is None or dd < best[0]:
                        best = (dd, x, y)
            cl.pop(best[2])
        return set(cl)

    blocks = []        # list of dict(palettes=[set..], images={md5: cell_pal})
    img_block = {}     # md5 -> block index (first assignment wins)

    def try_fit(block, sets):
        pals = [set(p2) for p2 in block["palettes"]]
        assign = []
        for cs in sets:
            for pi, pal in enumerate(pals):
                if len(pal | cs) <= 15:
                    pal |= cs
                    assign.append(pi)
                    break
            else:
                if len(pals) >= 32:
                    return None
                pals.append(set(cs))
                assign.append(len(pals) - 1)
        block["palettes"] = pals
        return assign

    panos = {}         # id(ev) -> dict(pano, cum, width_cells)
    for ev in events:
        if ev["kind"] == "scroll":
            rs = ev["runs"]
            cum = [0]
            for k in range(len(rs) - 1):
                _, dx = shift_of(imgs[rs[k][0]], imgs[rs[k + 1][0]])
                cum.append(cum[-1] + dx)
            W = 320 + cum[-1]
            Wc = (W + 15) // 16
            # Source each column from the frame in which the streaming
            # schedule paints it (entry frame), NOT last/first-writer
            # composites: content that animates during the scroll (the
            # leftmost member fades in on the CD) otherwise samples mixed
            # animation phases into adjacent 2px strips -- visible as
            # vertical banding.
            # latest-writer stitch: palettes get fitted on the SETTLED
            # appearance (content that animates during the scroll ends
            # here); the per-frame refresh below renders the journey
            # Brightest-writer stitch: latest-writer baked
            # the dim deceleration-tail frames into the right-side
            # columns -- a content-level fade palettes cannot remove.
            # Center-anchored stitch: brightest-writer chose
            # each column's source frame INDEPENDENTLY, so adjacent columns
            # came from different instants -- members idle-animate, and the
            # seams between mismatched instants read as palette shifts
            # sweeping through figures as they scroll past (palette RAM
            # measured constant).  Anchoring each column to the frame where
            # it sits nearest SCREEN CENTER makes source moments progress
            # smoothly across columns (adjacent columns <- adjacent frames)
            # and centers are fully bright for every member.
            pano = np.zeros((224, Wc * 16, 3), np.uint8)
            for j in range(Wc):
                target = j * 16 - 152
                best, bestd = None, None
                for k in range(len(rs)):
                    x0 = j * 16 - cum[k]
                    if x0 < 0 or x0 > 304:
                        continue
                    d2 = abs(cum[k] - target)
                    if bestd is None or d2 < bestd:
                        bestd, best = d2, (k, x0)
                if best is not None:
                    k2, x2 = best
                    pano[:, j * 16:(j + 1) * 16] = \
                        imgs[rs[k2][0]][:, x2:x2 + 16]
            panos[id(ev)] = dict(pano=pano, cum=cum, wc=Wc)
            continue
        if ev["kind"] == "palvar" or ev["md5"] in img_block:
            continue
        sets = [reduce15(cs) for cs in cellsets(ev["md5"])]
        if blocks:
            assign = try_fit(blocks[-1], sets)
            if assign is not None:
                blocks[-1]["images"][ev["md5"]] = assign
                img_block[ev["md5"]] = len(blocks) - 1
                continue
        blocks.append(dict(palettes=[], images={}))
        assign = try_fit(blocks[-1], sets)
        assert assign is not None, f"single image needs >32 palettes"
        blocks[-1]["images"][ev["md5"]] = assign
        img_block[ev["md5"]] = len(blocks) - 1

    scroll_data = {}   # id(ev) -> dict(block, cells[col][row]=(code,pal))
    for ev in events:
        if ev["kind"] != "scroll":
            continue
        pd = panos[id(ev)]
        q = quant12(pd["pano"])
        Wc = pd["wc"]
        sets = [reduce15(cs) for cs in cellsets_arr(q, Wc)]
        # panorama fitter: first-fit-decreasing, then lossy merge to <=32
        order = sorted(range(len(sets)), key=lambda i2: -len(sets[i2]))
        pals = []
        for i2 in order:
            for pal in pals:
                if len(pal | sets[i2]) <= 15:
                    pal |= sets[i2]
                    break
            else:
                pals.append(set(sets[i2]))
        def pal_dist(a, b):
            return min(((x >> 8) - (y >> 8)) ** 2
                       + (((x >> 4) & 15) - ((y >> 4) & 15)) ** 2
                       + ((x & 15) - (y & 15)) ** 2
                       for x in a for y in b)
        while len(pals) > 32:
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
        # best-palette per cell = fewest out-of-palette colours
        assign = []
        for cs in sets:
            assign.append(max(range(len(pals)),
                              key=lambda pi: len(cs & pals[pi])
                              - 0.01 * len(pals[pi])))
        blocks.append(dict(palettes=pals, images={}))
        bi = len(blocks) - 1
        scroll_data[id(ev)] = dict(block=bi, assign=assign, q=q, wc=Wc)

    for b in blocks:
        b["order"] = [sorted(p2) for p2 in b["palettes"]]

    # tiles + per-image maps (pen assignment depends on the block)
    tiles: dict[bytes, int] = {b"\xff" * 128: 0}   # code BASE+0: transparent
    offs = cell_offsets()
    img_maps = {}

    def map_of(md5):
        bi = img_block[md5]
        block = blocks[bi]
        pal_order = block["order"]
        lut = [{c: i for i, c in enumerate(pl)} for pl in pal_order]
        assign = block["images"][md5]
        q = used_imgs[md5]
        m = np.zeros((ROWS * COLS, 2), dtype=np.uint16)
        for cy in range(ROWS):
            for cx in range(COLS):
                ci = cy*COLS + cx
                pi = assign[ci]
                pal = pal_order[pi]
                cell = q[cy*CELL:(cy+1)*CELL, cx*CELL:(cx+1)*CELL]
                pens = np.zeros((CELL, CELL), dtype=np.uint8)
                for y in range(CELL):
                    for x in range(CELL):
                        r, g, b = (int(v) for v in cell[y, x])
                        c = (r << 8) | (g << 4) | b
                        if c not in lut[pi]:
                            c = min(pal, key=lambda pc: ((pc >> 8)-r)**2
                                    + (((pc >> 4) & 15)-g)**2
                                    + ((pc & 15)-b)**2)
                        pens[y, x] = lut[pi][c]
                ch = bytearray(128)
                for y in range(CELL):
                    for kx in range(8):
                        ch[y*8+kx] = (int(pens[y, 2*kx]) << 4) | int(pens[y, 2*kx+1])
                ch = bytes(ch)
                if ch not in tiles:
                    tiles[ch] = len(tiles)
                m[ci] = (BASE_CODE + tiles[ch], pi)
        return m

    for md5 in img_block:
        img_maps[md5] = map_of(md5)

    # tile the scroll panoramas: cells[col][row] -> (code, pal)
    for key, sd in scroll_data.items():
        bi, assign, q, Wc = sd["block"], sd["assign"], sd["q"], sd["wc"]
        pal_order = blocks[bi]["order"]
        lut = [{c: i for i, c in enumerate(pl)} for pl in pal_order]
        cells = [[None] * ROWS for _ in range(Wc)]
        for cy in range(ROWS):
            for cx in range(Wc):
                pi = assign[cy * Wc + cx]
                pal = pal_order[pi]
                cell = q[cy*CELL:(cy+1)*CELL, cx*CELL:(cx+1)*CELL]
                pens = np.zeros((CELL, CELL), dtype=np.uint8)
                for y in range(CELL):
                    for x in range(CELL):
                        r, g, b = (int(v) for v in cell[y, x])
                        c = (r << 8) | (g << 4) | b
                        if c not in lut[pi]:
                            c = min(pal, key=lambda pc: ((pc >> 8)-r)**2
                                    + (((pc >> 4) & 15)-g)**2 + ((pc & 15)-b)**2)
                        pens[y, x] = lut[pi][c]
                ch = bytearray(128)
                for y in range(CELL):
                    for kx in range(8):
                        ch[y*8+kx] = (int(pens[y, 2*kx]) << 4) | int(pens[y, 2*kx+1])
                ch = bytes(ch)
                if ch not in tiles:
                    tiles[ch] = len(tiles)
                cells[cx][cy] = (BASE_CODE + tiles[ch], pi)
        sd["cells"] = cells

    # base map per block = its first image
    block_base = {}
    for md5, bi in img_block.items():
        block_base.setdefault(bi, md5)

    # ---- degenerate-cel repair: a cel whose pen assignment collapsed onto
    # the block base (delta would be 0 cells) carries colour changes the
    # 15-colour merge ate -- typically positionally-selective palette
    # animation (the CD pulses one CRAM line while another line holds the
    # same colours elsewhere).  Give the differing cells dedicated palettes
    # (block cap 32) and re-tile just those cells.
    repaired = 0
    for ev in events:
        if ev["kind"] in ("palvar", "scroll"):
            continue
        md5 = ev.get("md5")
        if md5 is None or md5 not in img_block:
            continue
        bi = img_block[md5]
        if md5 == block_base.get(bi):
            continue
        if not (img_maps[md5] == img_maps[block_base[bi]]).all():
            continue
        qi = used_imgs[md5]
        qb = used_imgs[block_base[bi]]
        pal_order = blocks[bi]["order"]
        changed = False
        for ci in range(ROWS * COLS):
            cy, cx = divmod(ci, COLS)
            ic = qi[cy*CELL:(cy+1)*CELL, cx*CELL:(cx+1)*CELL]
            bc = qb[cy*CELL:(cy+1)*CELL, cx*CELL:(cx+1)*CELL]
            if (ic == bc).all():
                continue
            cols = reduce15({(int(r) << 8) | (int(g) << 4) | int(b)
                             for r, g, b in ic.reshape(-1, 3)})
            pi = None
            for k2, pl in enumerate(pal_order):
                if cols <= set(pl):
                    pi = k2
                    break
            if pi is None:
                for k2, pl in enumerate(pal_order):
                    if len(set(pl) | cols) <= 15:
                        pal_order[k2] = sorted(set(pl) | cols)
                        pi = k2
                        break
            if pi is None and len(pal_order) < 32:
                pal_order.append(sorted(cols))
                pi = len(pal_order) - 1
            if pi is None:
                continue
            pl = pal_order[pi]
            lut = {c: i2 for i2, c in enumerate(pl)}
            pens = np.zeros((CELL, CELL), dtype=np.uint8)
            for y in range(CELL):
                for x in range(CELL):
                    r, g, b = (int(v) for v in ic[y, x])
                    c = (r << 8) | (g << 4) | b
                    if c not in lut:
                        c = min(pl, key=lambda pc: ((pc >> 8)-r)**2
                                + (((pc >> 4) & 15)-g)**2 + ((pc & 15)-b)**2)
                    pens[y, x] = lut[c]
            ch = bytearray(128)
            for y in range(CELL):
                for kx in range(8):
                    ch[y*8+kx] = (int(pens[y, 2*kx]) << 4) | int(pens[y, 2*kx+1])
            ch = bytes(ch)
            if ch not in tiles:
                tiles[ch] = len(tiles)
            img_maps[md5] = img_maps[md5].copy()
            img_maps[md5][ci] = (BASE_CODE + tiles[ch], pi)
            changed = True
        if changed:
            repaired += 1
    if repaired:
        print(f"degenerate cels repaired: {repaired}")


    def pal_words(pals, k=1.0):
        out = b""
        for pl in pals:
            words = []
            for c in pl:
                r = min(15, int(round(((c >> 8) & 15) * k)))
                g = min(15, int(round(((c >> 4) & 15) * k)))
                b2 = min(15, int(round((c & 15) * k)))
                r, g, b2 = [0 if v == 1 else v for v in (r, g, b2)]
                words.append(0xF000 | (r << 8) | (g << 4) | b2)
            words += [0xF000] * (16 - len(words))
            out += struct.pack(">16H", *words)
        out += b"\x00" * (32 * 32 - len(out))
        return out

    palblocks = []
    pb_content = {}

    def emit_block(words_bytes):
        if words_bytes not in pb_content:
            pb_content[words_bytes] = len(palblocks)
            palblocks.append(words_bytes)
        return pb_content[words_bytes]

    pb_of = {}
    for bi, b in enumerate(blocks):
        pb_of[bi] = emit_block(pal_words(b["order"], 1.0))

    def variant_block(bi, mapping):
        out = b""
        for pl in blocks[bi]["order"]:
            words = []
            for c in pl:
                m = mapping.get(c, c)
                r, g, b2 = (m >> 8) & 15, (m >> 4) & 15, m & 15
                r, g, b2 = [0 if v == 1 else v for v in (r, g, b2)]
                words.append(0xF000 | (r << 8) | (g << 4) | b2)
            words += [0xF000] * (16 - len(words))
            out += struct.pack(">16H", *words)
        out += b"\x00" * (32 * 32 - len(out))
        return out

    deltas = b""
    delta_cache = {}
    script = []

    def delta_of(md5, bi):
        nonlocal deltas
        key = (md5, bi)
        if key in delta_cache:
            return delta_cache[key]
        m = img_maps[md5]
        bm = img_maps[block_base[bi]]
        diff = [(offs[i], int(m[i, 0]), int(m[i, 1]))
                for i in range(ROWS * COLS) if (m[i] != bm[i]).any()]
        doff = len(deltas)
        for o, c, a2 in diff:
            deltas += struct.pack(">3H", o, c, a2)
        delta_cache[key] = (doff, len(diff))
        return delta_cache[key]

    SX0, SY0 = 0x200, 0x100
    ROW_OFFS = []      # vram byte offset of (ring col, row) for any col
    def cell_off(col, row):
        col &= 0x3F
        return ((row & 0x0F) + (col << 4) + ((row & 0x30) << 6)) * 4

    margin_clear = None    # pending transparent-cell repaint after a scroll
    prev_map = None        # previous event's 280-cell content (chain deltas)
    steal_frames = 0       # scroll exit-extension frames owed by later events
    for ev in events:
        if ev["kind"] == "scroll":
            sd = scroll_data[id(ev)]
            bi = sd["block"]
            pb = pb_of[bi]
            cells = sd["cells"]
            cum = panos[id(ev)]["cum"]
            Wc = sd["wc"]
            # Scroll rendering model (-- matches the CD's actual
            # composition): the panorama is painted BRIGHT always; the
            # fade-in/out is a GLOBAL palette ramp (variant blocks on the
            # scroll events -- CD measurement: 0->settled over ~50 frames at
            # scroll start, later entrants arrive already bright).  Content
            # refresh (vs the pano) only runs after the ramp settles, for
            # genuine animation like the last member's arm gesture.
            # Per-cell content-swapped fading rendered smooth
            # fades as blocky patchwork and left stale mid-fade cells at the
            # view edges.
            lut = [{c: i2 for i2, c in enumerate(pl)}
                   for pl in blocks[bi]["order"]]
            pal_order2 = blocks[bi]["order"]
            qpano = q
            runs = ev["runs"]
            qcache = {}
            # global brightness ramp: scale per run vs the pano
            def view_mean(img_arr, c0px):
                return float(img_arr[:, :320].astype(np.float32).mean())
            settled = float(np.median([
                np.asarray(imgs[m]).astype(np.float32).mean()
                for m, _ in runs[len(runs)//3:2*len(runs)//3]]))
            scales = []
            for k, (md5r, fr) in enumerate(runs):
                pm = qpano[:, cum[k]:cum[k]+320]
                pano_mean = float((pm.astype(np.float32) * 17.0).mean())
                fm = float(np.asarray(imgs[md5r]).astype(np.float32).mean())
                a2 = 1.0 if pano_mean < 1 else min(1.0, fm / max(pano_mean, 1))
                scales.append(round(a2 * 16) / 16)
            ramp_end = 0
            for k, s in enumerate(scales):
                if s >= 0.9375:
                    ramp_end = k
                    break
            # no-fade LINEUP: truncate the scroll at the last
            # bright frame -- the CD's deceleration tail is intrinsically
            # dim and reads as a fade; hold the last bright view for the
            # dropped duration instead, then the burst/black cut follows.
            last_bright = max((k for k, s in enumerate(scales)
                               if s >= 0.9375), default=len(runs) - 1)
            dropped_tail = sum(fr for _, fr in runs[last_bright + 1:])
            runs = runs[:last_bright + 1]
            if dropped_tail and runs:
                runs[-1] = (runs[-1][0], runs[-1][1] + dropped_tail)
            first_bright = next((k for k, s in enumerate(scales)
                                 if s >= 0.9375), 0)
            dropped_head = sum(fr for _, fr in runs[:first_bright])
            if first_bright:
                runs = runs[first_bright:]
                cum = cum[first_bright:]
                scales = scales[first_bright:]
                if runs:
                    runs[0] = (runs[0][0], runs[0][1] + dropped_head)
            srchash = {}
            painted = set()
            sd = scroll_data[id(ev)]
            pal_order2 = blocks[sd["block"]]["order"]
            lut2 = [{c: i for i, c in enumerate(pl)} for pl in pal_order2]
            shown_cell = {}
            doff0 = len(deltas)
            deltas_local = b""
            # EXIT EXTENSION: the CD's own pan ends the moment
            # its 320px window is empty, but the CPS1 shows 384px -- the
            # 32px margins either side of the CD window still hold the
            # trailing member when the cut lands (user: "disappears before
            # the right most edge of the image has left the screen",
            # measured: 34px of art on screen at the cut).  When the pan
            # ends with the art already leaving (its right edge within
            # 64px of the window), keep sliding at the pan rate until the
            # art clears the WIDE window (art_right <= cum - 32), entering
            # columns painted black, and steal the extra frames from the
            # following black-gap event so the timeline is unchanged.
            pano_arr = panos[id(ev)]["pano"]
            _colsum = pano_arr.astype(np.int64).sum(axis=(0, 2))
            _lit = np.nonzero(_colsum > 900)[0]
            art_right = int(_lit.max()) + 1 if len(_lit) else 0
            n_ext = 0
            if runs and art_right and art_right <= cum[-1] + 64:
                n_ext = min(60, max(0, -(-(art_right + 32 - cum[-1]) // 2)))
            for k, (md5r, fr) in enumerate(runs):
                sx = SX0 + cum[k]
                left = max(0, (cum[k] - 32) // 16)
                right = min(Wc - 1, (cum[k] + 415) // 16)
                chunk = b""
                for j in range(left, right + 1):
                    # entering columns: paint BRIGHT pano cells once
                    if j not in painted:
                        painted.add(j)
                        for cy in range(ROWS):
                            code, pi = cells[j][cy]
                            chunk += struct.pack(
                                ">3H", cell_off(0x26 + j, 0x11 + cy),
                                code, pi)
                            if refresh_shots:
                                # record what is ACTUALLY on screen -- the
                                # quantised tile, not the raw panorama, or
                                # the refresh gate compares against a
                                # better-than-reality baseline
                                shown_cell[(j, cy)] = enc_cell(
                                    sd["q"][cy*CELL:(cy+1)*CELL,
                                            j*CELL:(j+1)*CELL],
                                    pal_order2, lut2, sd["assign"][j*ROWS + cy]
                                    if len(sd["assign"]) > j*ROWS + cy else 0)[1]
                        continue
                    # Refresh is OPT-IN per shot (disabled it
                    # globally: repainting wherever the live frame differed
                    # made the lineup's cells flip state as the scroll
                    # advanced).  Scenes where the CONTENT moves as well as
                    # the camera need it, or the characters ride the
                    # background instead of walking.
                    if ev.get("shot") not in refresh_shots:
                        continue
                    sxpix = j * 16 - cum[k]
                    if sxpix < 0 or sxpix + CELL > 320:
                        continue
                    live = quant12(imgs[md5r])[cy*CELL:(cy+1)*CELL,
                                               sxpix:sxpix+CELL]
                    cur = shown_cell.get((j, cy))
                    if cur is None:
                        continue
                    if float(np.abs(live.astype(int)
                                    - cur.astype(int)).mean()) <= 0.8:
                        continue
                    lcols = {(int(r) << 8) | (int(g) << 4) | int(b)
                             for r, g, b in live.reshape(-1, 3)}
                    short = sorted(range(len(pal_order2)),
                                   key=lambda q2: -(len(lcols & set(pal_order2[q2]))
                                                    - 0.01 * len(pal_order2[q2])))[:6]
                    best = None
                    for q2 in short:
                        ch2, rgb2 = enc_cell(live, pal_order2, lut2, q2)
                        e2 = float(np.abs(rgb2.astype(int)
                                          - live.astype(int)).mean())
                        if best is None or e2 < best[0]:
                            best = (e2, q2, ch2, rgb2)
                    err_cur = float(np.abs(cur.astype(int)
                                           - live.astype(int)).mean())
                    if best[0] >= err_cur * 0.95:
                        continue
                    if best[2] not in tiles:
                        tiles[best[2]] = len(tiles)
                    chunk += struct.pack(">3H", cell_off(0x26 + j, 0x11 + cy),
                                         BASE_CODE + tiles[best[2]], best[1])
                    shown_cell[(j, cy)] = best[3]
                    continue
                if k == len(runs) - 1 and n_ext == 0:
                    # Final scroll event also clears every ring column
                    # outside the 20-col window: sx snaps to
                    # 0x200 on the next tick while the post-scroll head's
                    # clears land one vblank later -- stale wrapped pano
                    # (El Gado's arm) showed in the margins for exactly
                    # one frame.  This delta is >=64 cells, so the
                    # catch-up applies it at the vblank STARTING the snap
                    # frame: cleared before the first render at 0x200.
                    # (With an exit extension the clear rides the LAST
                    # extension event instead: attached here it would
                    # apply at the vblank starting extension frame 1 and
                    # wipe the still-visible sliver mid-slide.)
                    for col in range(64):
                        if 0x26 <= col <= 0x39:
                            continue
                        for cy in range(ROWS):
                            chunk += struct.pack(
                                ">3H", cell_off(col, 0x11 + cy),
                                BASE_CODE, 0)
                d0 = doff0 + len(deltas_local)
                dc = len(chunk) // 6
                deltas_local += chunk
                # No fade on the LINEUP (user decision, ): the ramp
                # approximations consistently read worse than a clean bright
                # entry.  Members enter and exit at full brightness; the CD's
                # own dim exit frames still play as content in the
                # post-scroll burst.
                script.append((bi, pb_of[bi], d0, dc | 0xC000, fr, sx, SY0))
            for e2 in range(1, n_ext + 1):
                # exit extension (note above): continue the
                # slide past the CD's endpoint until the art clears the
                # CPS1's wider window
                cumx = cum[-1] + 2 * e2
                chunk = b""
                for j in range(max(0, (cumx - 32) // 16),
                               (cumx + 415) // 16 + 1):
                    if j in painted:
                        continue
                    painted.add(j)
                    for cy in range(ROWS):
                        code, pi = (cells[j][cy] if j < Wc
                                    else (BASE_CODE, 0))
                        chunk += struct.pack(
                            ">3H", cell_off(0x26 + j, 0x11 + cy), code, pi)
                if e2 == n_ext:
                    # the earlier ring clear, relocated from the last
                    # real run: lands at the vblank starting the (black)
                    # gap event -- invisible there
                    for col in range(64):
                        if 0x26 <= col <= 0x39:
                            continue
                        for cy in range(ROWS):
                            chunk += struct.pack(
                                ">3H", cell_off(col, 0x11 + cy),
                                BASE_CODE, 0)
                d0 = doff0 + len(deltas_local)
                dc = len(chunk) // 6
                deltas_local += chunk
                script.append((bi, pb_of[bi], d0, dc | 0xC000, 1,
                               SX0 + cumx, SY0))
            steal_frames += n_ext
            # BLACK BRIDGE: the post-scroll head's big write
            # lands mid-frame while the screen still shows the scroll's
            # final position; freshly written window columns sit inside
            # that old view on the right -- a 1-frame arm flash.  Insert a
            # 1-frame event that blanks the old view first, so the head's
            # write happens against black.
            if runs:
                # steal the bridge frame from the (tail-extended) last event
                bi2, pb2b, d02, dc2, fr2, sx2, sy2 = script[-1]
                if fr2 > 1:
                    script[-1] = (bi2, pb2b, d02, dc2, fr2 - 1, sx2, sy2)
                    blank = b""
                    lcol = max(0, (cum[-1] - 48) // 16)
                    rcol = min(Wc + 3, (cum[-1] + 415) // 16)
                    for j in range(lcol, rcol + 1):
                        for cy in range(ROWS):
                            blank += struct.pack(
                                ">3H", cell_off(0x26 + j, 0x11 + cy),
                                BASE_CODE, 0)
                    d0b = len(deltas_local) + 0
                    nb = len(blank) // 6
                    script.append((bi, pb_of[bi], doff0 + len(deltas_local),
                                   nb | 0xC000, 1, sx2, SY0))
                    deltas_local += blank
            deltas += deltas_local
            margin_clear = [(0x26 + j) & 0x3F for j in range(-2, Wc + 2)
                            if not (0 <= j < 20)]
            prev_map = None    # ring state diverged from the 20-col window
            continue
        md5 = ev["md5"]
        if md5 not in img_block:
            continue
        bi = img_block[md5]
        if ev["kind"] == "palvar":
            pb = emit_block(variant_block(bi, ev["mapping"]))
        else:
            pb = pb_of[bi]

        # CHAIN DELTAS: every event carries only the cells that
        # differ from the PREVIOUS event's content and sets count bit 15
        # (the scroll path's skip-base flag), so the engine writes each
        # cell exactly once per load.  A base+delta two-pass would paint the
        # block anchor first and be caught mid-write by the renderer as
        # single-frame flashes of foreign content (knife/Haggar scenes).
        # Chain heads (first event, post-scroll) carry the full map.
        m = img_maps[md5]
        extra = b""
        if margin_clear is not None:
            for col in margin_clear:
                for cy in range(ROWS):
                    extra += struct.pack(">3H", cell_off(col, 0x11 + cy),
                                         BASE_CODE, 0)
            margin_clear = None
        # bit13 (force-tick) ONLY for post-scroll heads: prev_map is None is
        # also true for the FIRST event, and force-ticking the map's head
        # reintroduced the one-shot ordering race at story start with the
        # vblank recovery disabled -- the "totally botched" map.
        head_after_scroll = margin_clear is not None
        if prev_map is None:
            cells = [(offs[i2], int(m[i2, 0]), int(m[i2, 1]))
                     for i2 in range(ROWS * COLS)]
        else:
            cells = [(offs[i2], int(m[i2, 0]), int(m[i2, 1]))
                     for i2 in range(ROWS * COLS)
                     if (m[i2] != prev_map[i2]).any()]
        prev_map = m.copy()
        body = b"".join(struct.pack(">3H", *c) for c in cells)
        d0 = len(deltas)
        deltas += extra + body
        dcnt = (len(extra) + len(body)) // 6
        # post-scroll heads go back to the VBLANK path (catch-up
        # applies skipped 1-frame heads in order at the frame edge --
        # atomic, ).  The bit13 force-tick write landed
        # MID-FRAME on the transition tick while the screen still rendered
        # at the scroll's final position, flashing the next scene's columns
        # (an arm) on the right for one frame.
        flags = 0x8000
        # bit13 = force-tick: post-scroll heads are 1-frame events whose
        # vblank application has proven unreliable; their content is
        # near-black so a synchronous write cannot flash visibly
        fr_ev = ev["frames"]
        if steal_frames and fr_ev > 1:
            # repay the scroll exit-extension out of this event's span
            # (the post-scroll black gap) so the timeline is unchanged
            take = min(steal_frames, fr_ev - 1)
            fr_ev -= take
            steal_frames -= take
        script.append((bi, pb, d0, dcnt | flags, fr_ev, SX0, SY0))

    # cue annotations -> pal high byte of the covering event
    cues = []
    for spec in args.cue:
        tstr, hexv = spec.split(":")
        cues.append((int(tstr), int(hexv, 16)))
    for tcue, val in cues:
        t = 0
        for k in range(len(script)):
            if t + script[k][4] > tcue:
                r = list(script[k])
                assert r[1] < 256 and val < 256
                r[1] |= val << 8
                script[k] = tuple(r)
                break
            t += script[k][4]

    shot_ids = list(range(len(blocks)))
    base_maps = {bi: img_maps[block_base[bi]] for bi in block_base}
    # panorama blocks have no held image; their base map is never drawn
    # (scroll events carry the base-skip flag) -- emit transparent filler
    for bi in shot_ids:
        if bi not in base_maps:
            filler = np.zeros((ROWS * COLS, 2), dtype=np.uint16)
            filler[:, 0] = BASE_CODE
            base_maps[bi] = filler

    # ---- emit
    out = args.outdir
    out.mkdir(parents=True, exist_ok=True)
    (out / "tiles.bin").write_bytes(b"".join(
        t for t, _ in sorted(tiles.items(), key=lambda kv: kv[1])))
    (out / "palblocks.bin").write_bytes(b"".join(palblocks))
    (out / "basemaps.bin").write_bytes(b"".join(
        base_maps[sh].astype(">u2").tobytes() for sh in shot_ids))
    (out / "deltas.bin").write_bytes(deltas)
    with open(out / "script.bin", "wb") as f:
        for rec in script:
            f.write(struct.pack(">HHIHHHH", *rec))
    with open(out / "script.tsv", "w") as f:
        f.write("# base_block\tpal_block\tdelta_off\tdcnt_flag\tframes\tsx\tsy\n")
        for rec in script:
            f.write("\t".join(str(v) for v in rec) + "\n")
    manifest = dict(
        base_code=BASE_CODE,
        events=len(script), shots=len(shot_ids),
        unique_tiles=len(tiles), tile_bytes=len(tiles) * 128,
        pal_blocks=len(palblocks), palblock_bytes=len(palblocks) * 1024,
        basemap_bytes=len(shot_ids) * 1120, delta_bytes=len(deltas),
        script_bytes=len(script) * 12,
        cells_over_15=over15,
        total_frames=sum(r[4] for r in script),
        table_bytes=(len(palblocks) * 1024 + len(shot_ids) * 1120
                     + len(deltas) + len(script) * 12),
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
