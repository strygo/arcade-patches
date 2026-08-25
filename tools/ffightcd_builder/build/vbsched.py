"""Vblank scheduler for the cutscene engines' map deltas.

The engines commit every VRAM write inside vertical blanking (CPS1: 38
lines, ~24,300 68000 cycles, of which the stock ISR spends ~3,500).  A
conversion's delta for one event can be far bigger than that window (the
opening's post-scroll heads carry 600-1000 cells, the JP ending's scene
switch 896), so the deltas are re-planned here at ROM-build time:

* within an event, duplicate offsets keep the last write; a write that does
  not change the cell (against the simulated map state) is dropped -- same
  screen, fewer cycles;
* the cells of every event are assigned to vblanks under a per-vblank cycle
  budget.  A cell that is VISIBLE at the frame its event goes live must land
  in that vblank (the "now" list, atomic by construction); a cell that is
  not visible yet -- ring columns beyond the scroll window, rows outside the
  vertical window -- may land in a later vblank that has spare budget, as
  long as it lands before the first frame that shows it and before any later
  event rewrites it (a superseded write is simply dropped);
* deferred cells that cannot ride a later event's own list are stored as
  SPILL chunks behind the record's now-cells: chunk j applies on the event's
  j-th non-load vblank (frames-left counts them down), so the engine keeps
  no queue state at all.

The engine semantics this mirrors (engine.py, tick/ovr):
  ENGINE1 (opening): event N loads at the vblank starting frame start(N);
    "early" events (record bit13 clear) commit their now-list in that same
    vblank, "late" events (bit13 set -- the conversion's >=64-cell events,
    which the shipped build deferred by one vblank via the catch-up path,
    and whose timing the conversions were tuned around) commit it one vblank
    later.  Spill chunk j of the CURRENT event applies at load+j.
  ENGINE2/3 (ending, pan): everything commits at load; spills at load+j.

Blob format per event: count x (offset u16, code u16, attr u16) now-cells;
if flags bit14 is set, then spill chunks (n u16, n cells) for j = 1, 2, ...
terminated by n = 0xFFFF.  Record layout is otherwise unchanged
(>HHIHHHH: bi|fade, pal|cue, delta_off, count|flags, frames, sx, sy).
"""
from __future__ import annotations
import struct

CELL_BYTES = 6
REC = ">HHIHHHH"

# cycle model (68000 @ 10 MHz, 262 lines x 64 us) -- calibrated against the
# HBMAME raster probe (scratchpad a25isr, isr_gate.lua): the numbers are
# deliberately conservative so a build that passes here has margin on
# hardware
VBLANK_CYC = 38 * 640          # 24,320: the whole blanking interval
STOCK_ISR_CYC = 3_600          # the game's own vblank handler before us
CELL_CYC = 36                  # per now/spill cell (move.w + move.l + loop)
PAL_CYC = 4_800                # 512-word block copy (movem)
SAFETY_CYC = 1_500
# spill lists can make a rescheduled blob a little LARGER than the
# conversion's; rom.py reserves this much after every delta blob
SPILL_SLACK = 0x800
# deferred (not-yet-visible) cells only fill a vblank up to this fraction of
# its capacity: the typical vblank stays short, and only work that MUST land
# uses the whole window (margin for the core's slower VRAM path)
SOFT_FILL = 0.6


class SchedError(SystemExit):
    pass


def cell_rowcol(off):
    i = off >> 2
    return (i & 0xF) | (((i >> 10) & 3) << 4), (i >> 4) & 0x3F


def visible_at(off, sx, sy, margin_bars=False):
    """Is the scroll2 cell at byte offset `off` inside the 384x224 window at
    scroll (sx, sy)?  MAME's cps1 puts screen (0,0) at map (sx+64, sy+16).
    margin_bars: the reunion engine's opaque scroll1 bars cover screen
    x 0..31 / 352..383 for y 32..159, so cells fully under them are not
    visible."""
    row, col = cell_rowcol(off)
    c0 = (sx + 0x40) >> 4
    c1 = (sx + 0x40 + 383) >> 4
    r0 = (sy + 0x10) >> 4
    r1 = (sy + 0x10 + 223) >> 4
    vis = ((col - c0) & 0x3F) <= ((c1 - c0) & 0x3F) and \
          ((row - r0) & 0x3F) <= ((r1 - r0) & 0x3F)
    if vis and margin_bars:
        x = (col * 16 - (sx + 0x40)) & 0x3FF
        y = (row * 16 - (sy + 0x10)) & 0x3FF
        if (x + 16 <= 32 or x >= 352) and y >= 32 and y + 16 <= 160:
            vis = False
    return vis


def _rgb(word):
    bright = 0x0F + ((word >> 12) << 1)
    return tuple(((word >> sh) & 0xF) * 0x11 * bright // 0x2D for sh in (8, 4, 0))


def _tile_pens(tiles, base_code, code):
    i = code - base_code
    if i < 0 or (i + 1) * 128 > len(tiles):
        return None
    t = tiles[i * 128:(i + 1) * 128]
    pens = set()
    for byte in t:
        pens.add(byte >> 4)
        pens.add(byte & 0xF)
    pens.discard(15)          # scroll tile pen 15 is transparent
    return pens


def materialize_fades(script: bytes, palblocks: bytes, name: str,
                      verbose=True):
    """Bake every (block, fade code) pair the script uses into its own
    palette block -- the engine's tick then copies blocks verbatim (movem)
    and never scales 512 words per fade step at run time.  The record keeps
    its fade nibble (the OBJ-palette blocks in the overrides still scale by
    it); only the block index (word 1 low byte) is redirected.  Returns
    (script_out, palblocks_out, n_variants)."""
    n = len(script) // 16
    out = bytearray(script)
    blocks = bytearray(palblocks)
    nblk = len(palblocks) // 1024
    variants = {}
    for i in range(n):
        w0, w1 = struct.unpack(">HH", script[i * 16:i * 16 + 4])
        code = w0 >> 12
        if code == 0:
            continue
        pal = w1 & 0xFF
        key = (pal, code)
        if key not in variants:
            level = (15 - code) << 12
            src = palblocks[pal * 1024:(pal + 1) * 1024]
            assert len(src) == 1024, f"{name}: block {pal} missing"
            words = struct.unpack(">512H", src)
            blocks += struct.pack(">512H", *((w & 0x0FFF) | level for w in words))
            variants[key] = nblk
            nblk += 1
            assert nblk <= 256, f"{name}: more than 256 palette blocks"
        out[i * 16 + 2:i * 16 + 4] = struct.pack(">H", (w1 & 0xFF00) | variants[key])
    if verbose and variants:
        print(f"  vbsched {name}: {len(variants)} fade-step palette blocks "
              f"materialised ({nblk} blocks total)")
    return bytes(out), bytes(blocks), len(variants)


def schedule(script: bytes, deltas: bytes, *, clear_code: int, late_rule: bool,
             fixed_cyc: int, name: str, extra_cyc=None, dark_frames: int = 0,
             margin_bars: bool = False, palblocks: bytes = None,
             tiles: bytes = None, verbose=True):
    """Return (script_out, deltas_out, report).  extra_cyc(N, loading) ->
    cycles of other work the engine does in a vblank while event N is live
    (loading = the vblank that loads N);
    dark_frames = frames from event 0's load during which the engine holds
    the scroll layers off (nothing is visible, everything may defer);
    palblocks + tiles enable DEFERRED PALETTE copies (record bit 15): when a
    load vblank cannot fit its visible cells next to the block copy, and
    every visible cell renders identically under the old and new blocks,
    the copy moves to the next vblank."""
    n = len(script) // 16
    recs = [list(struct.unpack(REC, script[i * 16:(i + 1) * 16])) for i in range(n)]
    vis_at = lambda off, sx, sy: visible_at(off, sx, sy, margin_bars)
    start = []
    t = 0
    for r in recs:
        start.append(t)
        t += r[4]
    nframes = t
    late = [bool(late_rule and (r[3] & 0x1FFF) >= 64) for r in recs]
    land = [start[i] + (1 if late[i] else 0) for i in range(n)]

    # ---- pass 1: dedupe + drop no-ops against the logical map state
    clear = (clear_code, 0)
    state = {}
    real = []
    ndup = nnoop = 0
    for i, r in enumerate(recs):
        cnt, off = r[3] & 0x1FFF, r[2]
        seen = {}
        for k in range(cnt):
            o, c, a = struct.unpack(">3H", deltas[off + 6 * k:off + 6 * k + 6])
            if o in seen:
                ndup += 1
            seen[o] = (c, a)
        cells = []
        for o, v in seen.items():
            if state.get(o, clear) == v:
                nnoop += 1
                continue
            state[o] = v
            cells.append((o, v))
        real.append(cells)

    # ---- pass 2: schedule per vblank
    # frame -> live event
    live = [0] * (nframes + 2)
    for i in range(n):
        for f in range(start[i], start[i] + recs[i][4]):
            live[f] = i
    live[nframes] = live[nframes + 1] = n - 1

    def pal_key(i):
        return (recs[i][1] & 0xFF, recs[i][0] >> 12)

    def sinks(v):
        """events whose now-list lands at v, plus the spill (event, j) sink."""
        s = []
        for i in landers.get(v, ()):
            s.append(("now", i))
        if v < nframes:
            # (the vblank after the last frame runs FIN, not a spill)
            e = live[v]
            j = v - start[e]
            if j >= 1:
                s.append(("spill", e, j))
        return s

    landers = {}
    for i in range(n):
        landers.setdefault(land[i], []).append(i)
    # next vblank with a sink, for every v
    last_v = nframes + 1
    has_sink = [bool(sinks(v)) for v in range(last_v + 2)]
    next_sink = [0] * (last_v + 3)
    nxt = None
    for v in range(last_v + 1, -1, -1):
        next_sink[v] = nxt if nxt is not None else last_v + 2
        if has_sink[v]:
            nxt = v

    def deadline(off, v0):
        """first frame >= v0 whose live event shows the cell (inf if none)."""
        f = max(v0, dark_frames)
        while f <= nframes:
            e = live[min(f, nframes)]
            if vis_at(off, recs[e][5], recs[e][6]):
                return f
            f += 1
        return 10 ** 9

    # ---- deferred palette copies (bit 15).  Candidates: events whose load
    # vblank would not fit the palette copy next to the cells that must land
    # then.  Verified by rendering equivalence: every cell visible on the
    # load frame (after that vblank's writes) shows the same colours under
    # the previous block and the new one.
    defer_pal = [False] * n
    if palblocks is not None and tiles is not None and not late_rule:
        base_code = clear_code
        st = {}
        for i in range(n):
            for o, val in real[i]:
                st[o] = val
            if i == 0 or pal_key(i) == pal_key(i - 1) or start[i] < dark_frames:
                continue
            visible = [(o, st.get(o, clear)) for o in st
                       if vis_at(o, recs[i][5], recs[i][6])]
            need = sum(1 for o, _ in real[i]
                       if vis_at(o, recs[i][5], recs[i][6]))
            budget = VBLANK_CYC - STOCK_ISR_CYC - fixed_cyc - SAFETY_CYC - PAL_CYC
            if extra_cyc:
                budget -= extra_cyc(i, True)
            if need * CELL_CYC <= budget:
                continue
            oldb, newb = recs[i - 1][1] & 0xFF, recs[i][1] & 0xFF
            old = struct.unpack(">512H", palblocks[oldb * 1024:(oldb + 1) * 1024])
            new = struct.unpack(">512H", palblocks[newb * 1024:(newb + 1) * 1024])
            ok = True
            for o, (code, attr) in visible:
                pens = _tile_pens(tiles, base_code, code)
                if pens is None:
                    ok = False
                    break
                pi = (attr & 0x1F) * 16
                if any(_rgb(old[pi + p]) != _rgb(new[pi + p]) for p in pens):
                    ok = False
                    break
            defer_pal[i] = ok
            if verbose:
                print(f"  vbsched {name}: event {i} load needs {need} cells with a "
                      f"block change -- palette copy deferred one vblank: "
                      f"{'YES (renders identically)' if ok else 'NOT POSSIBLE'}")

    def capacity(v):
        cyc = VBLANK_CYC - STOCK_ISR_CYC - fixed_cyc - SAFETY_CYC
        e = live[min(v, nframes)]
        loading = v <= nframes and start[e] == v
        if loading and (e == 0 or pal_key(e) != pal_key(e - 1)) and not defer_pal[e]:
            cyc -= PAL_CYC
        if extra_cyc:
            cyc -= extra_cyc(e, loading)
        if v <= nframes and start[e] + 1 == v and defer_pal[e]:
            cyc -= PAL_CYC
        return max(0, cyc // CELL_CYC)

    pending = {}          # off -> [value, origin, earliest, deadline]
    placed = {}           # v -> list of (off, value)
    ndeferred = nsuper = 0
    maxcells = (0, -1)
    for v in range(0, last_v + 1):
        for i in landers.get(v, ()):
            for o, val in real[i]:
                if o in pending:
                    nsuper += 1
                pending[o] = [val, i, v, None]
        if not pending:
            continue
        sk = sinks(v)
        if not sk:
            continue
        nsv = next_sink[v]
        cap = capacity(v)
        # deadlines (lazy)
        for o, p in pending.items():
            if p[3] is None:
                p[3] = deadline(o, p[2])
        must = [o for o, p in pending.items() if p[3] < nsv]
        if len(must) > cap:
            worst = sorted(must, key=lambda o: pending[o][3])
            raise SchedError(
                f"VBSCHED {name}: vblank {v} needs {len(must)} cells "
                f"(cap {cap}) -- origins "
                f"{sorted(set(pending[o][1] for o in must))}, next sink {nsv}")
        rest = sorted((o for o in pending if pending[o][3] >= nsv),
                      key=lambda o: (pending[o][3], pending[o][2]))
        # deferred cells ride along only while the vblank is not already
        # heavy with mandatory work
        soft = max(0, int(cap * SOFT_FILL) - len(must)) if len(must) <= cap * 0.5 else 0
        take = must + rest[:soft]
        for o in take:
            p = pending.pop(o)
            placed.setdefault(v, []).append((o, p[0], p[1]))
            if v != p[2]:
                ndeferred += 1
        if len(take) > maxcells[0]:
            maxcells = (len(take), v)
    nlost = len(pending)     # never became visible before the script ended

    # ---- pass 3: rebuild blobs
    now = {i: [] for i in range(n)}
    spill = {}     # (i, j) -> cells
    for v, cells in placed.items():
        # top-down, left-to-right in SCREEN order at that vblank's scroll:
        # the CPS-A fetches each tilemap row band as the beam reaches it,
        # so a list written in this order stays ahead of the beam even if
        # its tail runs past the blanking
        e = live[min(v, nframes)]
        sx, sy = recs[e][5], recs[e][6]
        def skey(c):
            row, col = cell_rowcol(c[0])
            return ((row * 16 - (sy + 0x10)) & 0x3FF, (col * 16 - (sx + 0x40)) & 0x3FF)
        cells = sorted(cells, key=skey)
        sk = sinks(v)
        nows = [s[1] for s in sk if s[0] == "now"]
        sp = [s for s in sk if s[0] == "spill"]
        if nows:
            # own cells to their own list where possible, deferred ones to
            # the last-applied list (early(N) after late(N-1))
            tgt = nows[-1]
            for o, val, origin in cells:
                now[origin if origin in nows else tgt].append((o, val))
        else:
            _, e, j = sp[0]
            spill.setdefault((e, j), []).extend((o, val) for o, val, _ in cells)
    out = bytearray()
    outrecs = []
    nspill_ev = 0
    for i, r in enumerate(recs):
        r = list(r)
        r[2] = len(out)
        flags = r[3] & 0xE000
        flags &= ~0x6000
        if late[i]:
            flags |= 0x2000
        cells = now[i]
        assert len(cells) < 0x2000
        for o, (c, a) in cells:
            out += struct.pack(">3H", o, c, a)
        js = sorted(j for (e, j) in spill if e == i)
        if js:
            flags |= 0x4000
            nspill_ev += 1
            for j in range(1, js[-1] + 1):
                ch = spill.get((i, j), [])
                out += struct.pack(">H", len(ch))
                for o, (c, a) in ch:
                    out += struct.pack(">3H", o, c, a)
            out += struct.pack(">H", 0xFFFF)
        if defer_pal[i]:
            flags |= 0x8000
        else:
            flags &= 0x7FFF
        r[3] = len(cells) | flags
        outrecs.append(struct.pack(REC, *r))
    if len(out) > len(deltas) + SPILL_SLACK:
        raise SchedError(
            f"VBSCHED {name}: rescheduled deltas ({len(out)} B) exceed the "
            f"conversion's blob ({len(deltas)} B) plus the reserved slack "
            f"({SPILL_SLACK}); raise SPILL_SLACK")
    out += b"\xFF" * (len(deltas) + SPILL_SLACK - len(out))
    rep = dict(events=n, frames=nframes, dup=ndup, noop=nnoop,
               deferred=ndeferred, superseded=nsuper, lost=nlost,
               spill_events=nspill_ev, max_cells=maxcells[0],
               max_cells_vblank=maxcells[1], late_events=sum(late),
               deferred_palettes=sum(defer_pal))
    if verbose:
        print(f"  vbsched {name}: {n} events/{nframes} f, dropped {ndup} dup + "
              f"{nnoop} no-op, deferred {ndeferred} (superseded {nsuper}, "
              f"unshown {nlost}), spill lists on {nspill_ev} events, "
              f"peak {maxcells[0]} cells at vblank {maxcells[1]}")
    return b"".join(outrecs), bytes(out), rep
