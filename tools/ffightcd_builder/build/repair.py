"""Per-frame repairs applied to a rendered sweep before conversion.

Four passes, each confined to a measured frame window.  They exist because
the VM renders what the disc's code produces, and a few of those frames
carry artifacts the hardware would not show -- a cel peeking into a
letterbox band, a beam-race pixel, a stray flicker -- plus one genuine
content substitution (the US split-screen mouth animation, driven by the
bundled mouth data).  All windows are VM frame numbers.

    repair.py flicker|raster|letterbox <frames> <outdir> <jp|us>
    repair.py mouth <us-frames> <outdir>
"""
from __future__ import annotations
import argparse
from PIL import Image
from pathlib import Path
import numpy as np
import sys


FLICKER_PATCHES = {
    "us": ((972, 1389), (
        ((86, 315), (86, 316)),
        ((87, 315), (87, 316)),
    )),
    "jp": ((1345, 1941), (
        ((86, 315), (86, 316)),
        ((87, 315), (87, 316)),
    )),
}

def _flicker(a) -> int:
    sweep, outdir, region = a.sweep, a.outdir, a.region
    if region not in FLICKER_PATCHES:
        print(f"no patches defined for region {region!r}; nothing to do")
        return 0
    (f0, f1), pixels = FLICKER_PATCHES[region]
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in range(f0, f1):
        src = sweep / f"f{f:06d}.png"
        if not src.exists():
            continue
        im = np.asarray(Image.open(src).convert("RGB")).copy()
        changed = False
        for (y, x), (dy, dx) in pixels:
            if (im[y, x] != im[dy, dx]).any():
                im[y, x] = im[dy, dx]
                changed = True
        if changed:
            Image.fromarray(im).save(outdir / f"f{f:06d}.png")
            n += 1
    print(f"repaired {n} frames -> {outdir}")
    return 0


SPOTS = {
    (56, 312): (55, 311),
    (56, 313): (55, 314),
    (63, 312): (62, 312),
    (63, 313): (62, 313),
    (80, 260): (79, 260),
    (80, 261): (79, 261),
    (81, 260): (81, 261),
    (82, 260): (82, 261),
    (85, 260): (84, 260),
    (86, 260): (86, 259),
    (87, 260): (87, 259),
    (87, 261): (88, 261),
    (120, 152): (119, 152),
    (120, 153): (119, 155),
    (120, 154): (117, 153),
    (120, 155): (119, 157),
    (120, 156): (119, 157),
    (121, 152): (121, 151),
    (121, 153): (121, 151),
    (126, 152): (125, 152),
    (126, 153): (125, 153),
    (127, 152): (127, 151),
    (127, 153): (128, 153),
    (127, 154): (126, 154),
    (127, 155): (126, 155),
    (127, 156): (126, 156),
}
SPECK_ZONE = (46, 53, 219, 235)   # y0, y1, x0, x1
SPECK_DONOR_ROW = 44
RASTER_WINDOWS = {"jp": (138, 767), "us": (164, 904)}      # VM frames

def _raster(a) -> int:
    snaps, outdir, region = a.snaps, a.outdir, a.region
    lo, hi = RASTER_WINDOWS[region]
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in range(lo, hi + 1):
        p = snaps / f"f{f:06d}.png"
        if not p.exists():
            continue
        img = Image.open(p).convert("RGB")
        a = np.asarray(img).copy()
        changed = False
        for (y, x), (dy, dx) in SPOTS.items():
            if not np.array_equal(a[y, x], a[dy, dx]):
                a[y, x] = a[dy, dx]
                changed = True
        y0, y1, x0, x1 = SPECK_ZONE
        donor = a[SPECK_DONOR_ROW, x0:x1]
        zone = a[y0:y1, x0:x1]
        if not np.array_equal(zone, np.broadcast_to(donor, zone.shape)):
            a[y0:y1, x0:x1] = donor
            changed = True
        if changed:
            Image.fromarray(a).save(outdir / p.name)
            n += 1
    print(f"  raster-spot repair: {n} frames rewritten")
    return 0


LETTERBOX_WINDOWS = {"jp": ((6360, 7840), 32, 160)}           # VM frames

def _letterbox(a) -> int:
    snaps, outdir, region = a.snaps, a.outdir, a.region
    if region not in LETTERBOX_WINDOWS:
        print("  letterbox-band repair: no window for this region")
        return 0
    (lo, hi), top, bot = LETTERBOX_WINDOWS[region]
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in range(lo, hi + 1):
        p = snaps / f"f{f:06d}.png"
        if not p.exists():
            continue
        a = np.asarray(Image.open(p).convert("RGB")).copy()
        if a[:top].any() or a[bot:].any():
            a[:top] = 0
            a[bot:] = 0
            Image.fromarray(a).save(outdir / p.name)
            n += 1
    print(f"  letterbox-band repair: {n} frames rewritten")
    return 0


BOX = (90, 140, 188, 250)          # y0,y1,x0,x1 in disc coords
MOUTH_DATA = (
    "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"
)
US_REF = 5564                      # clenched reference (VM frame)
ONSETS = [5567, 5575, 5597, 5604, 5623, 5642, 5657, 5665, 5683, 5693]
WINDOW = (5566, 5737)              # never paste outside the line (VM frames)
OPEN_F, HALF_F = 4, 3
def crop(path: Path) -> np.ndarray:
    y0, y1, x0, x1 = BOX
    return np.asarray(Image.open(path).convert("RGB"))[y0:y1, x0:x1]

def _load_mouth_data():
    """-> (open, half) BOX-sized RGB cels from the packed mouth data."""
    import zlib, struct, base64
    buf = zlib.decompress(base64.b64decode(MOUTH_DATA))
    magic, h, w, n = struct.unpack(">4sHHH", buf[:10])
    assert magic == b"FFM1", "mouth_data.bin: bad magic"
    pal = [tuple(buf[10 + i*3:13 + i*3]) for i in range(n)]
    off = 10 + n*3
    def plane(o):
        return np.array([pal[b] for b in buf[o:o + h*w]],
                        dtype=np.uint8).reshape(h, w, 3)
    return plane(off), plane(off + h*w)


def _mouth(a) -> int:
    us_dir, outdir = a.us_dir, a.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    m_open, m_half = _load_mouth_data()
    us_ref = crop(us_dir / f"f{US_REF:06d}.png")
    # remap the mouth cels onto the US scene's OWN colours.  As raw
    # data they carry colours that pushed the scene's palette block
    # over the 32-palette cap; the converter's lossy merge then shared
    # the hand's palette with a PULSING one, and the TV glow's 4-step
    # palette cycle made the thumb shimmer ("the thumb jumps around").
    # The US talk cels contain the same mouth tones, so nearest-colour
    # remap is near-identity and adds ZERO new colours.
    us_pool = np.unique(
        np.concatenate([crop(us_dir / f"f{f:06d}.png").reshape(-1, 3)
                        for f in (US_REF, 5569, 5541, 5571)]), axis=0
    ).astype(np.int16)
    def remap(cel):
        px = cel.reshape(-1, 3).astype(np.int16)
        out = np.empty_like(px)
        for i in range(0, len(px), 4096):
            chunk = px[i:i + 4096]
            d = np.abs(chunk[:, None, :] - us_pool[None, :, :]).sum(axis=2)
            out[i:i + 4096] = us_pool[d.argmin(axis=1)]
        return out.reshape(cel.shape).astype(np.uint8)
    m_open = remap(m_open)
    m_half = remap(m_half)
    # mouth mask: union of the mouth-cel diffs AND every pixel
    # that varies across the window's own US frames -- the US clenched
    # mouth has its own phases (jaw shifts ~570 px); a mask built against
    # one phase only let the other phases' corners peek out beside the
    # paste ("part of the old mouth on the left").
    diff = ((np.abs(m_open.astype(np.int16) - us_ref.astype(np.int16))
             .sum(axis=2) > 18) |
            (np.abs(m_half.astype(np.int16) - us_ref.astype(np.int16))
             .sum(axis=2) > 18))
    for f in range(WINDOW[0], WINDOW[1]):
        q = us_dir / f"f{f:06d}.png"
        if q.exists():
            diff |= (np.abs(crop(q).astype(np.int16) -
                            us_ref.astype(np.int16)).sum(axis=2) > 18)
    m = diff.copy()
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            m |= np.roll(np.roll(diff, dy, 0), dx, 1)
    mask = m
    # clip every paste mask to FULL 16px cells (disc x192-240,
    # box-relative cols 4..52).  A mask edge mid-cell makes that cell's
    # content toggle between flap/clench states, and the converter
    # re-quantizes the cell's 15-colour palette differently per state --
    # the STATIC pixels sharing the cell (the thumb sliver at x250+)
    # shimmer on every flap ("the thumb jumps around").
    mask[:, :192 - BOX[2]] = False
    mask[:, 240 - BOX[2]:] = False
    print(f"mask: {int(mask.sum())} px of {mask.size}")

    state_for = {}
    for on in ONSETS:
        for k in range(OPEN_F):
            state_for[on + k] = "open"
        for k in range(OPEN_F, OPEN_F + HALF_F):
            state_for.setdefault(on + k, "half")
    # absorb sub-3-frame clenched gaps between flaps: 1-2
    # frame cels sit below the converter's hold threshold and blend-
    # merge into neighbors as clenched/open HYBRID cells (the teeth
    # band inside the open mouth); extend half across micro-gaps
    frames = sorted(state_for)
    for a, b in zip(frames, frames[1:]):
        if 1 <= b - a - 1 <= 2:
            for g in range(a + 1, b):
                state_for.setdefault(g, "half")
    # the US disc has its OWN talk cels in this scene -- a
    # right-biased opening (the "only the right part opens" defect the
    # user has reported ).  Pin every non-flap frame whose
    # mouth deviates from clenched back to the US_REF state across the
    # whole split-screen scene, so the ONLY mouth animation is our
    # own flaps.
    SCENE = (5434, 6764)          # VM frames
    NOSE = (55, 85, 225, 270)
    y0, y1, x0, x1 = BOX
    us_full_ref = np.asarray(Image.open(us_dir / f"f{US_REF:06d}.png")
                             .convert("RGB"))
    nose_ref = us_full_ref[NOSE[0]:NOSE[1], NOSE[2]:NOSE[3]].astype(np.int16)
    # per-STATE pinning.  The US talk cels (clustered from the
    # scene) each get their own template mask = pixels that cel changes
    # vs clench; a frame is pinned only if it MATCHES a talk cel, and
    # only that cel's own pixels are replaced -- the hand can never be
    # touched (pasting a wider mask ate thumb pixels when the receiver
    # crossed the mouth box: "the thumb jumps around").
    US_TALK = (5569,)   # VM frame; the ONLY real US talk cel -- the other three were contamination artifacts
    templates = []
    for tf in US_TALK:
        cel = crop(us_dir / f"f{tf:06d}.png").astype(np.int16)
        tm = (np.abs(cel - us_ref.astype(np.int16)).sum(axis=2) > 18)
        tmd = tm.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                tmd |= np.roll(np.roll(tm, dy, 0), dx, 1)
        tmd[:, :192 - BOX[2]] = False        # cell-aligned (see mask note)
        tmd[:, 240 - BOX[2]:] = False
        templates.append((cel, tmd))
    # the disc leaves a ~10-frame remnant of the OLD desk
    # scene (a knuckle-sized chunk at x252-268, y108-130) lingering
    # after the wipe completes.  The pre-mouth conversion happened to
    # hide it via its cel-merge structure (user-approved look); the
    # reconverted timeline shows it.  Erase it explicitly: settle the
    # zone to the reference frame's content once the face is revealed.
    CHUNK = (104, 134, 250, 272)
    chunk_ref = us_full_ref[CHUNK[0]:CHUNK[1], CHUNK[2]:CHUNK[3]]
    for f in range(5530, WINDOW[0]):
        src = us_dir / f"f{f:06d}.png"
        if not src.exists():
            continue
        prev = outdir / f"f{f:06d}.png"
        base = prev if prev.exists() else src
        full = np.asarray(Image.open(base).convert("RGB")).copy()
        zone = full[CHUNK[0]:CHUNK[1], CHUNK[2]:CHUNK[3]]
        dev = (np.abs(zone.astype(np.int16) - chunk_ref.astype(np.int16))
               .sum(axis=2) > 30)
        if dev.sum() < 8:
            continue
        # don't touch mid-wipe frames (black present around the zone)
        gz = full[CHUNK[0]:CHUNK[1] + 16, CHUNK[2] - 8:CHUNK[3] + 8]
        if float((gz.sum(axis=2) < 30).mean()) > 0.02:
            continue
        full[CHUNK[0]:CHUNK[1], CHUNK[2]:CHUNK[3]] = chunk_ref
        Image.fromarray(full).save(outdir / f"f{f:06d}.png")
    for f in range(SCENE[0], SCENE[1]):
        if f in state_for:
            continue
        src = us_dir / f"f{f:06d}.png"
        if not src.exists():
            continue
        prev = outdir / f"f{f:06d}.png"      # layer on the chunk pass
        full = np.asarray(Image.open(prev if prev.exists() else src)
                          .convert("RGB"))
        nose = full[NOSE[0]:NOSE[1], NOSE[2]:NOSE[3]].astype(np.int16)
        if float(np.abs(nose - nose_ref).mean()) > 3.0:
            continue                        # fade/wipe frame
        # The diagonal wipe reveals the face BEFORE the mouth area, so the
        # nose gate alone passes late-wipe frames and the pin would paint
        # clenched cells into not-yet-revealed regions (the floating chunk in
        # the user's video).  Skip any frame with unrevealed (black) content
        # in or around the box.
        guard = full[y0:min(y1 + 20, full.shape[0]),
                     max(0, x0 - 12):min(x1 + 12, full.shape[1])]
        if float((guard.sum(axis=2) < 30).mean()) > 0.02:
            continue
        region = full[y0:y1, x0:x1].astype(np.int16)
        d_clench = float(np.abs(region - us_ref.astype(np.int16)).mean())
        best = None
        for cel, tmd in templates:
            d = float(np.abs(region - cel).mean())
            if best is None or d < best[0]:
                best = (d, cel, tmd)
        if best[0] >= d_clench or best[0] > 6.0:
            continue                        # clenched already, or unknown
        im = full.copy()
        reg = im[y0:y1, x0:x1]
        reg[best[2]] = us_ref[best[2]]
        im[y0:y1, x0:x1] = reg
        Image.fromarray(im).save(outdir / f"f{f:06d}.png")
    n = 0
    for f in range(WINDOW[0], WINDOW[1]):
        st = state_for.get(f)
        if not st:
            continue
        src = us_dir / f"f{f:06d}.png"
        if not src.exists():
            continue
        im = np.asarray(Image.open(src).convert("RGB")).copy()
        y0, y1, x0, x1 = BOX
        cel = m_open if st == "open" else m_half
        region = im[y0:y1, x0:x1]
        region[mask] = cel[mask]
        im[y0:y1, x0:x1] = region
        Image.fromarray(im).save(outdir / f"f{f:06d}.png")
        n += 1
    print(f"composited {n} frames -> {outdir}")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="pass_", required=True)
    p = sub.add_parser("flicker", help="stray flickering pixels")
    p.add_argument("sweep", type=Path); p.add_argument("outdir", type=Path)
    p.add_argument("region", choices=("jp", "us"))
    for name, help_ in (("raster", "map-scene beam pixels"),
                        ("letterbox", "duo-shot letterbox bands")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("snaps", type=Path); p.add_argument("outdir", type=Path)
        p.add_argument("region", choices=("jp", "us"))
    p = sub.add_parser("mouth", help="US mouth animation from the mouth data")
    p.add_argument("us_dir", type=Path)
    p.add_argument("outdir", type=Path)
    a = ap.parse_args()
    return {"flicker": _flicker, "raster": _raster,
            "letterbox": _letterbox, "mouth": _mouth}[a.pass_](a)


if __name__ == "__main__":
    raise SystemExit(main())
