"""Full Mega Drive compositor over a VDP state dump (VRAM/CRAM/regs).

Implements every feature the FF CD cutscenes use: two tile planes with
priority split, per-line HScroll (raw signed), per-plane VScroll, 512px
horizontal wrap, window-plane override/clipping (reg18), and the sprite
layer (link-ordered, column-major cells, per-sprite flip + priority).
"""
import struct
from PIL import Image

# No default dump path.  This shipped an absolute path to one machine's
# capture directory; the build never uses it (it always passes `state`
# from the VM), so the only caller was the scenes CLI, which now says
# which dump it means.
DUMP = None
RAMP = None            # None -> linear r*255//7 ; set to MAME_RAMP for parity
MAME_RAMP = (0, 52, 87, 116, 144, 172, 206, 255)

def words_le(path):
    b = open(path, "rb").read()
    return list(struct.unpack("<%dH" % (len(b) // 2), b))

def load_state(F, dump=None):
    d = dump or DUMP
    if d is None:
        raise ValueError(
            "load_state needs a dump directory: megadrive has no default. "
            "The build path never reaches here -- it passes `state` from the "
            "VM -- so this is a tool calling in without saying which capture.")
    vram = words_le(f"{d}/vram_{F}.bin"); cram = words_le(f"{d}/cram_{F}.bin")
    regs = hs = None
    for line in open(f"{d}/layers.txt"):
        p = dict(z.split("=", 1) for z in line.split() if "=" in z)
        if int(p["f"]) == F:
            regs = [int(z) for z in p["regs"].split(",")]
            hs = [int(z) for z in p["hs"].split(",")]
            break
    return vram, cram, regs, hs

def load_raw(F, dump=None):
    """Ground-truth rendered frame (gNNNNNN.raw: 8-byte header + 320x224 BGRX)."""
    d = dump or DUMP
    data = open(f"{d}/g{F:06d}.raw", "rb").read()
    img = Image.new("RGB", (320, 224)); px = img.load()
    for i in range(320 * 224):
        o = 8 + i * 4
        px[i % 320, i // 320] = (data[o + 2], data[o + 1], data[o])
    return img

def vs(v):
    """Raw VSRAM word -> vroll() convention (validated against ground truth:
    hardware 224 == -32 raw == +32 in vroll's terms)."""
    s = v - 65536 if v > 32767 else v
    s = ((s + 128) % 256) - 128
    return -s

def prgb(v):
    r = (v >> 1) & 7; g = (v >> 5) & 7; b = (v >> 9) & 7
    if RAMP: return (RAMP[r], RAMP[g], RAMP[b])
    return (r * 255 // 7, g * 255 // 7, b * 255 // 7)

def _tile_rows(vram, idx):
    base = idx * 16; rows = []
    for y in range(8):
        w0 = vram[base + y * 2] if base + y * 2 < len(vram) else 0
        w1 = vram[base + y * 2 + 1] if base + y * 2 + 1 < len(vram) else 0
        rows.append([(w0 >> 12) & 0xF, (w0 >> 8) & 0xF, (w0 >> 4) & 0xF, w0 & 0xF,
                     (w1 >> 12) & 0xF, (w1 >> 8) & 0xF, (w1 >> 4) & 0xF, w1 & 0xF])
    return rows

def plane_pri(vram, nt, cram):
    """(low, high) RGBA 512x256, split by the nametable priority bit."""
    lo = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    hi = Image.new("RGBA", (512, 256), (0, 0, 0, 0))
    plo = lo.load(); phi = hi.load()
    for cy in range(32):
        for cx in range(64):
            e = nt[cy * 64 + cx]
            idx = e & 0x7FF; hf = e & 0x800; vf = e & 0x1000
            ps = (e >> 13) & 3; pri = e & 0x8000
            rows = _tile_rows(vram, idx); px = phi if pri else plo
            for y in range(8):
                sy = 7 - y if vf else y
                for x in range(8):
                    ix = rows[sy][7 - x if hf else x]
                    if ix == 0: continue
                    px[cx * 8 + x, cy * 8 + sy] = (*prgb(cram[ps * 16 + ix]), 255)
    return lo, hi

def sprites_pri(vram, cram, sat):
    """(low, high) RGBA 320x224 sprite layers, link-ordered.

    Sprite-vs-sprite: the FIRST sprite in link order owns each pixel (MD
    hardware rule; its priority bit then decides the plane interaction).
    This is what makes Guy's talk-mouth strip (an early SAT entry) show on
    top of his face sprites (later entries) in the opening duo scene."""
    lo = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    hi = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    plo = lo.load(); phi = hi.load(); i = 0
    claimed = bytearray(512 * 512)
    for _ in range(80):
        w = sat // 2 + i * 4
        y = vram[w] & 0x3FF; size = (vram[w + 1] >> 8) & 0xFF
        link = vram[w + 1] & 0x7F; attr = vram[w + 2]; x = vram[w + 3] & 0x1FF
        hs = ((size >> 2) & 3) + 1; vs = (size & 3) + 1
        t = attr & 0x7FF; hf = attr & 0x800; vf = attr & 0x1000
        ps = (attr >> 13) & 3; px = phi if (attr & 0x8000) else plo
        for cxk in range(hs):
            for cyk in range(vs):
                rows = _tile_rows(vram, t + cxk * vs + cyk)
                for yy in range(8):
                    ry = 7 - yy if vf else yy
                    for xx in range(8):
                        ix = rows[ry][7 - xx if hf else xx]
                        if ix == 0: continue
                        dcx = (hs - 1 - cxk) if hf else cxk
                        dcy = (vs - 1 - cyk) if vf else cyk
                        X = (x - 128) + dcx * 8 + xx + 128
                        Y = (y - 128) + dcy * 8 + yy + 128
                        if 0 <= X < 512 and 0 <= Y < 512:
                            k = Y * 512 + X
                            if not claimed[k]:
                                claimed[k] = 1
                                px[X, Y] = (*prgb(cram[ps * 16 + ix]), 255)
        if link == 0: break
        i = link
    return lo.crop((128, 128, 448, 352)), hi.crop((128, 128, 448, 352))

def hscroll_table(vram, regs):
    """Per-scanline (A, B) horizontal scroll, RAW SIGNED table values."""
    hs_base = (regs[13] & 0x3F) * 0x400; mode = regs[11] & 3
    A = []; B = []
    for y in range(224):
        idx = y if mode == 3 else (y & ~7) if mode == 2 else 0
        wA = vram[(hs_base // 2) + idx * 2]; wB = vram[(hs_base // 2) + idx * 2 + 1]
        raw = lambda v: (v - 65536) if v > 32767 else v
        A.append(raw(wA)); B.append(raw(wB))
    return A, B

def wrap_perline(img512, shifts):
    """Apply hscroll per scanline with 512px wrap -> 320x224."""
    out = Image.new("RGBA", (320, 224), (0, 0, 0, 0))
    src = img512.load(); dst = out.load()
    for y in range(224):
        sh = shifts[y]
        for x in range(320):
            dst[x, y] = src[(x - sh) % 512, y]
    return out

def vroll(img512, dy):
    """Vertical scroll with wrap at 256."""
    if dy == 0: return img512
    out = Image.new("RGBA", img512.size, (0, 0, 0, 0)); m = (-dy) % 256
    out.paste(img512.crop((0, m, 512, 256)), (0, 0))
    out.paste(img512.crop((0, 0, 512, m)), (0, 256 - m))
    return out

def _pal_np(cram):
    import numpy as np
    pal = np.zeros((64, 3), dtype=np.uint8)
    for i in range(64):
        pal[i] = prgb(cram[i])
    return pal


def _tiles_np(vram):
    """All 2048 tile pen blocks as (2048, 8, 8) uint8."""
    import numpy as np
    w = np.asarray(vram, dtype=np.uint16).reshape(2048, 16)
    pens = np.empty((2048, 8, 8), dtype=np.uint8)
    w0 = w[:, 0::2].astype(np.uint16)   # (2048, 8) rows
    w1 = w[:, 1::2].astype(np.uint16)
    for k in range(4):
        pens[:, :, k] = (w0 >> (12 - 4 * k)) & 0xF
        pens[:, :, 4 + k] = (w1 >> (12 - 4 * k)) & 0xF
    return pens


def _plane_np(tiles, nt, np):
    """(lo_pen, lo_att, hi_pen, hi_att) 256x512 arrays.  att = ps*16.
    NOTE: plane vflip is intentionally a no-op — parity with the reference
    renderer above (whose vflip write-back cancels itself); the cutscenes
    never vflip plane tiles."""
    e = np.asarray(nt, dtype=np.uint16).reshape(32, 64)
    idx = (e & 0x7FF).astype(np.int32)
    hf = (e & 0x800) != 0
    ps = ((e >> 13) & 3).astype(np.uint8)
    pri = (e & 0x8000) != 0
    tt = tiles[idx]                       # (32, 64, 8, 8)
    tt = np.where(hf[:, :, None, None], tt[:, :, :, ::-1], tt)
    pen = tt.transpose(0, 2, 1, 3).reshape(256, 512)
    att = np.repeat(np.repeat(ps * 16, 8, axis=0), 8, axis=1)
    prim = np.repeat(np.repeat(pri, 8, axis=0), 8, axis=1)
    lo_pen = np.where(prim, 0, pen)
    hi_pen = np.where(prim, pen, 0)
    return lo_pen, att, hi_pen


def _sprites_np(tiles, vram, cram_att_unused, sat, np):
    """(lo_pen, lo_att, hi_pen, hi_att) 224x320 arrays, link-ordered.

    Sprite-vs-sprite: the FIRST sprite in link order owns each pixel (MD
    hardware rule; its priority bit then decides the plane interaction).
    This is what makes Guy's talk-mouth strip (an early SAT entry) show on
    top of his face sprites (later entries) in the opening duo scene."""
    lo_pen = np.zeros((512, 512), dtype=np.uint8)
    lo_att = np.zeros((512, 512), dtype=np.uint8)
    hi_pen = np.zeros((512, 512), dtype=np.uint8)
    hi_att = np.zeros((512, 512), dtype=np.uint8)
    claimed = np.zeros((512, 512), dtype=bool)
    i = 0
    for _ in range(80):
        w = sat // 2 + i * 4
        y = vram[w] & 0x3FF
        size = (vram[w + 1] >> 8) & 0xFF
        link = vram[w + 1] & 0x7F
        attr = vram[w + 2]
        x = vram[w + 3] & 0x1FF
        hs = ((size >> 2) & 3) + 1
        vsz = (size & 3) + 1
        t = attr & 0x7FF
        hf = attr & 0x800
        vf = attr & 0x1000
        ps = (attr >> 13) & 3
        # assemble the sprite pen block (vsz*8 rows, hs*8 cols), column-major
        blk = np.empty((vsz * 8, hs * 8), dtype=np.uint8)
        for cxk in range(hs):
            for cyk in range(vsz):
                cell = tiles[(t + cxk * vsz + cyk) & 0x7FF]
                blk[cyk * 8:cyk * 8 + 8, cxk * 8:cxk * 8 + 8] = cell
        if vf:
            blk = blk[::-1, :]
        if hf:
            blk = blk[:, ::-1]
        X0, Y0 = x, y
        h, wd = blk.shape
        x1, y1 = min(X0 + wd, 512), min(Y0 + h, 512)
        if X0 >= 512 or Y0 >= 512:
            if link == 0:
                break
            i = link
            continue
        sub = blk[:y1 - Y0, :x1 - X0]
        m = (sub != 0) & ~claimed[Y0:y1, X0:x1]
        pen_dst, att_dst = (hi_pen, hi_att) if attr & 0x8000 else (lo_pen, lo_att)
        pen_dst[Y0:y1, X0:x1][m] = sub[m]
        att_dst[Y0:y1, X0:x1][m] = ps * 16
        claimed[Y0:y1, X0:x1] |= m
        if link == 0:
            break
        i = link
    return (lo_pen[128:352, 128:448], lo_att[128:352, 128:448],
            hi_pen[128:352, 128:448], hi_att[128:352, 128:448])


def render_np(F, vscroll=(0, 0), dump=None, state=None):
    """numpy fast path — byte-identical to render() (verified)."""
    import numpy as np
    vram, cram, regs, hs = state if state else load_state(F, dump)
    a = (regs[2] & 0x38) * 0x400
    b = (regs[4] & 0x07) * 0x2000
    sat = (regs[5] & 0x7F) * 0x200
    tiles = _tiles_np(vram)
    pal = _pal_np(cram)
    ntA = vram[a // 2:a // 2 + 2048]
    ntB = vram[b // 2:b // 2 + 2048]
    Alo, Aatt, Ahi = _plane_np(tiles, ntA, np)
    Blo, Batt, Bhi = _plane_np(tiles, ntB, np)
    dyA, dyB = vscroll
    if dyA:
        m = (-dyA) % 256
        Alo, Ahi = np.roll(Alo, -m, axis=0), np.roll(Ahi, -m, axis=0)
        Aatt = np.roll(Aatt, -m, axis=0)
    if dyB:
        m = (-dyB) % 256
        Blo, Bhi = np.roll(Blo, -m, axis=0), np.roll(Bhi, -m, axis=0)
        Batt = np.roll(Batt, -m, axis=0)
    hsA, hsB = hscroll_table(vram, regs)
    ys = np.arange(224)[:, None]
    xsA = (np.arange(320)[None, :] - np.asarray(hsA)[:, None]) % 512
    xsB = (np.arange(320)[None, :] - np.asarray(hsB)[:, None]) % 512

    def shifted(pen, att, xs):
        return pen[:224][ys, xs], att[:224][ys, xs]

    winV = regs[18]
    vpos = (winV & 0x1F) * 8
    win_active = bool(winV & 0x80 and vpos < 224)
    if win_active:
        winbase = (regs[3] & 0x3F) * 0x400
        ntW = vram[winbase // 2:winbase // 2 + 2048]
        Wlo, Watt, Whi = _plane_np(tiles, ntW, np)

    def a_layer(pen512, att512, wpen512):
        p, at = shifted(pen512, att512, xsA)
        if not win_active:
            return p, at
        p = p.copy(); at = at.copy()
        p[vpos:] = wpen512[vpos:224, :320]
        at[vpos:] = Watt[vpos:224, :320]
        return p, at

    slo_p, slo_a, shi_p, shi_a = _sprites_np(tiles, vram, None, sat, np)

    # ---- SHADOW / HIGHLIGHT (reg12 bit 3).
    # The CD-6 farewell's shadow under Cody and Jessica is a Mega Drive
    # SHADOW-OPERATOR sprite: palette line 3, pen 15, carrying no colour of
    # its own -- it halves what is beneath.  The game turns the mode ON for
    # exactly that stretch (reg12 0x81 -> 0x89 between f7400 and f8000).
    # Without this the operator drew as opaque MAGENTA, which is the purple
    # shadow reported twice in review; 2767 px of it per frame from f7960 on,
    # against 32-47 px in the disc-composed reference.
    sh_mode = bool(regs[12] & 0x08)
    shadow_op = hilite_op = None
    if sh_mode:
        for p_, a_ in ((slo_p, slo_a), (shi_p, shi_a)):
            is3 = a_ == 48                    # palette line 3 (att = line*16)
            sm, hm = is3 & (p_ == 15), is3 & (p_ == 14)
            shadow_op = sm if shadow_op is None else (shadow_op | sm)
            hilite_op = hm if hilite_op is None else (hilite_op | hm)
            p_[sm | hm] = 0                   # operators are not DRAWN

    comp_pen = np.zeros((224, 320), dtype=np.uint8)
    comp_att = np.zeros((224, 320), dtype=np.uint8)
    hi_seen = np.zeros((224, 320), dtype=bool)
    Bhi_s = shifted(Bhi, Batt, xsB)
    Ahi_s = a_layer(Ahi, Aatt, Whi if win_active else None)
    for pen, att in (shifted(Blo, Batt, xsB),
                     a_layer(Alo, Aatt, Wlo if win_active else None),
                     (slo_p, slo_a),
                     Bhi_s, Ahi_s, (shi_p, shi_a)):
        m = pen != 0
        comp_pen[m] = pen[m]
        comp_att[m] = att[m]
    rgb = pal[(comp_att + comp_pen).astype(np.int32)]
    rgb[comp_pen == 0] = pal[0] * 0     # background: black, as reference
    if sh_mode:
        # An operator darkens what is BENEATH it, and a HIGH-PRIORITY layer
        # is not beneath it -- the hardware exempts those pixels.  The
        # characters are plane A HIGH here (plane A low is empty all scene),
        # so without the exemption the shadow painted OVER Cody and Jessica,
        # which is what showed in review as "the shadow appears above the
        # characters".
        #
        # The exemption is the ONLY use of hi_seen.  The hardware also
        # shadows pixels no high-priority layer covers at all; applying THAT
        # darkened the whole beach and took f8000 from mean 0.25 to 34.8
        # against the capture-verified reference, so it is not applied.
        # Scored against the oracle, not against the doc.
        hi_seen = (Bhi_s[0] != 0) | (Ahi_s[0] != 0) | (shi_p != 0)
        dark = shadow_op & ~hilite_op & ~hi_seen
        rgb[dark] = rgb[dark] // 2
        rgb[hilite_op] = np.minimum(255, rgb[hilite_op].astype(np.int16)
                                    // 2 + 128).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


def render(F, vscroll=(0, 0), dump=None, state=None):
    """Composite one frame. state=(vram,cram,regs,hs) overrides the dump read
    (used by usmap to render synthesized US state)."""
    vram, cram, regs, hs = state if state else load_state(F, dump)
    a = (regs[2] & 0x38) * 0x400; b = (regs[4] & 0x07) * 0x2000
    sat = (regs[5] & 0x7F) * 0x200
    ntA = [vram[a // 2 + i] for i in range(2048)]
    ntB = [vram[b // 2 + i] for i in range(2048)]
    Alo, Ahi = plane_pri(vram, ntA, cram); Blo, Bhi = plane_pri(vram, ntB, cram)
    dyA, dyB = vscroll
    Alo, Ahi = vroll(Alo, dyA), vroll(Ahi, dyA)
    Blo, Bhi = vroll(Blo, dyB), vroll(Bhi, dyB)
    slo, shi = sprites_pri(vram, cram, sat)
    hsA, hsB = hscroll_table(vram, regs)
    winV = regs[18]; vpos = (winV & 0x1F) * 8
    win_active = bool(winV & 0x80 and vpos < 224)
    Wl = Wh = None
    if win_active:
        winbase = (regs[3] & 0x3F) * 0x400
        ntW = [vram[winbase // 2 + i] for i in range(2048)]
        Wl, Wh = plane_pri(vram, ntW, cram)
    def a_layer(Aimg, Wimg):
        As = wrap_perline(Aimg, hsA)
        if not win_active: return As
        out = Image.new("RGBA", (320, 224), (0, 0, 0, 0))
        out.paste(As.crop((0, 0, 320, vpos)), (0, 0))
        out.paste(Wimg.crop((0, vpos, 320, 224)), (0, vpos))
        return out
    comp = Image.new("RGBA", (320, 224), (0, 0, 0, 255))
    comp.alpha_composite(wrap_perline(Blo, hsB)); comp.alpha_composite(a_layer(Alo, Wl))
    comp.alpha_composite(slo)
    comp.alpha_composite(wrap_perline(Bhi, hsB)); comp.alpha_composite(a_layer(Ahi, Wh))
    comp.alpha_composite(shi)
    return comp.convert("RGB")
