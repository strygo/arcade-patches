"""Restructure the Z80 opening song (cue 0x52) to end at the engine's fade.

The stock FM opening is shorter than the CD-timed opening cutscene, so the
68K schedule would have to re-fire it and the song restarts mid-scene.  The
CD masters solve the same problem with repeat points in the middle of the
track; this gives the FM song the same shape.

Score analysis of cue 0x52: intro (768 ticks) | A A' (bars 4-11) | a repeat
construct | 2nd ending (bars 19-21) | cadence.  The repeat construct is
literal in the bytes: each melody channel carries an op0E slot-0 count=1 at
the tick-3072 boundary jumping back to the 3-bar phrase P = [2304,2880),
with an op12 at the 2880 boundary exiting to the 2nd ending on the last
pass; bar 15 is the 1st-ending bar (1E).  ch8/9 (OKI) hold the same
768-tick section as a twice-played op0E block; ch5/6 ride 1-bar rest-figure
loops through it; ch7 echoes ch4 at +18 ticks.

The edit raises the score's own repeat count so the MIDDLE of the song
loops, then plays the exit material and cadence once:

    intro  A A'  (P 1E) x c  P x (b+1)  2E  cadence

Extra P's beyond the loop's own last pass are fragment clones placed in
the m1's free 0x8000+ space (banked; sequence-fetch address == file
offset), entered from the retargeted op12 -- which still fires exactly
once, on the last pass.  A trailing op04 in each fragment re-applies the
op12's own flags merge (handler $0AB4: flags = (flags & $97) | arg), so
the 2nd ending enters in exactly the stock state.  Every seam the
listener hears is the score's own transition except P->P, which mirrors
the score's bar11->bar12 move.  The two knobs exist for timing
granularity: extra ticks = 768*(c-1) + 576*b, a 192-tick (1.8 s) grid at
the STOCK tempo (0x1B6; 0.5585 frames/tick measured live).  The fit
target is the TITLE ANIMATE-IN -- the wall-rise/logo-slam that follows
the story's fade to black, measured visually per region (fire+8006 f US,
fire+7673 f JP; the sequel events differ, US inserts a drugs screen
before the demo, but the title sequence itself is frame-identical) -- so
the cadence's final chord lands on the logo slam and rings over the
settled card, and the attract is silent from there to the demo's own BGM:

    US  c=12 b=1   final note-on 0.30 s after the title settles
    JP  c=12 b=0   final note-on 0.52 s after the title settles

With b=0 no fragments are needed at all: the melody op12 keeps its stock
2nd-ending target and ch8/9 simply play their section once more.

The 68K schedule fires 0x76 once (rom.py cues=); the restarts are gone.
Only cue 0x52's own data and dead 0xFF space are touched: every other
cue's channel walk visits none of the patched bytes, and every patched
byte is asserted against its stock value below, so a drifted m1 refuses
to build rather than shipping a half-applied song.
"""
from __future__ import annotations

# Argument byte counts for the driver's sequence ops (< 0x20); note/rest
# bytes (>= 0x20) are single.  Needed to walk whole events when cloning.
ARGLEN = {0x00: 0, 0x01: 0, 0x02: 0, 0x03: 0, 0x04: 1, 0x05: 2, 0x06: 1,
          0x07: 1, 0x08: 1, 0x09: 1, 0x0A: 1, 0x0B: 1, 0x0C: 1, 0x0D: 1,
          0x0E: 3, 0x0F: 3, 0x10: 3, 0x11: 3, 0x12: 3, 0x13: 3, 0x14: 3,
          0x15: 3, 0x16: 2, 0x17: 0, 0x18: 1, 0x19: 1}

FRAG_BASE = 0x8000

# Melody channels: (op12 last-pass-exit site, op0E repeat-count site).
MEL = {0: (0x52C5, 0x52D4), 1: (0x5354, 0x5363), 2: (0x53F8, 0x5408),
       3: (0x549C, 0x54AB), 4: (0x5567, 0x5579)}
# OKI channels: their twice-played-section op0E site.
OKI = {8: 0x5742, 9: 0x57C8}
# Rest-figure loops that span the repeat section: (op0E site, stock count).
# Each figure is 192 ticks, so the count grows by extra/192.
FIG = {5: (0x55D6, 0x0F), 6: (0x5648, 0x08)}
# ch8/9 section split at the tick-2880 walk boundary: [S..split) plays
# every round, [split..site) only on the exit pass.
SPLIT = {8: 0x5729, 9: 0x57BD}
# ch8's clone must renormalize the instrument to the replay-entry voice
# each pass (the section changes it mid-way); ch9's does not.
CLONE_FIX = {8: bytes([0x08, 0x71]), 9: b""}


def _event_run(rom: bytes, addr: int, min_bytes: int) -> tuple[bytes, int]:
    """Whole events from addr covering >= min_bytes; no control flow."""
    out = bytearray()
    a = addr
    while len(out) < min_bytes:
        b = rom[a]
        n = 1 + ARGLEN.get(b, 0) if b < 0x20 else 1
        assert not (0x0E <= b <= 0x17), f"ctrl op {b:02x} at {a:04x} in reloc"
        out += rom[a:a + n]
        a += n
    return bytes(out), a


def _clone_range(rom: bytes, lo: int, hi: int) -> bytes:
    """Verbatim stock bytes [lo,hi): whole events, no loop/jump/end/tempo."""
    a = lo
    while a < hi:
        b = rom[a]
        n = 1 + ARGLEN.get(b, 0) if b < 0x20 else 1
        assert not (0x0E <= b <= 0x17) and b != 0x05, \
            f"op{b:02x}@{a:04x} not clonable"
        a += n
    assert a == hi, f"range {lo:04x}..{hi:04x} splits an event"
    return bytes(rom[lo:hi])


def apply_opening_loop(m1: bytes, c: int, tail_b: int) -> tuple[bytes, str]:
    """Return (patched m1, one-line summary).

    c = melody repeat count byte; tail_b = extra tail P plays beyond the
    loop's own last pass, so the tail is P x (tail_b+1) before the 2nd
    ending.  tail_b >= 2 puts an op0F count=tail_b-1 inside the melody
    fragment; tail_b == 1 omits it (a count byte of 0 would re-arm
    forever -- the handler treats slot==0 as first arrival); tail_b == 0
    needs no fragments at all: the melody op12 keeps its stock 2nd-ending
    target and ch8/9 simply play their section once more (their section
    tail IS the exit groove, so the stock fall-through already lines up).
    """
    rom = bytearray(m1)
    stock = bytes(m1)
    assert 0 <= tail_b <= 200
    extra = 768 * (c - 1) + 576 * tail_b
    frags = bytearray()
    writes: dict[int, bytes] = {}

    for ch, (op12, op0e) in MEL.items():
        assert stock[op12] == 0x12 and stock[op0e] == 0x0E, f"ch{ch} sites"
        assert stock[op0e + 1] == 0x01, f"ch{ch} stock repeat count"
        p_start = (stock[op0e + 2] << 8) | stock[op0e + 3]
        e2 = (stock[op12 + 2] << 8) | stock[op12 + 3]
        arg12 = stock[op12 + 1]
        writes[op0e + 1] = bytes([c])
        if tail_b == 0:
            continue
        pclone = _clone_range(stock, p_start, op12)
        assert stock[p_start] == 0x04, f"ch{ch}: P does not open with op04"
        # F = [P-clone][0F tail_b-1 -> F][04 arg12][16 -> 2E]: the tail P
        # plays, then the 2nd ending entered with the op12's own flags
        # merge applied.
        fa = FRAG_BASE + len(frags)
        loop = (bytes([0x0F, tail_b - 1, fa >> 8, fa & 0xFF])
                if tail_b >= 2 else b"")
        frags += pclone + loop + bytes([0x04, arg12,
                                        0x16, e2 >> 8, e2 & 0xFF])
        writes[op12 + 2] = bytes([fa >> 8, fa & 0xFF])

    for ch, site in OKI.items():
        assert stock[site] == 0x0E and stock[site + 1] == 0x01, f"ch{ch}"
        g = (stock[site + 2] << 8) | stock[site + 3]
        if tail_b == 0:
            writes[site + 1] = bytes([c])   # S x (c+1), stock fall-through
            continue
        split = SPLIT[ch]
        p8 = _clone_range(stock, g, split)
        e8 = _clone_range(stock, split, site)
        assert stock[g] == 0x04, f"ch{ch}: section does not open with op04"
        reloc, resume = _event_run(stock, site + 4, 3)
        # G = [fix][P8][0F tail_b -> G][E8][reloc][16 -> resume]: the
        # redirect at site+4 displaces whole events, relocated into the
        # fragment.
        ga = FRAG_BASE + len(frags)
        frags += (CLONE_FIX[ch] + p8
                  + bytes([0x0F, tail_b, ga >> 8, ga & 0xFF]) + e8
                  + reloc + bytes([0x16, resume >> 8, resume & 0xFF]))
        writes[site + 1] = bytes([c - 1])
        writes[site + 4] = bytes([0x16, ga >> 8, ga & 0xFF])

    for ch, (site, cnt0) in FIG.items():
        assert stock[site] == 0x0E and stock[site + 1] == cnt0, f"ch{ch}"
        newcnt = cnt0 + extra // 192
        assert newcnt <= 0xFF, f"ch{ch} figure count overflow"
        writes[site + 1] = bytes([newcnt])

    for a, v in writes.items():
        rom[a:a + len(v)] = v
    assert all(v == 0xFF for v in stock[FRAG_BASE:FRAG_BASE + len(frags)]), \
        "fragment space not free"
    rom[FRAG_BASE:FRAG_BASE + len(frags)] = frags
    assert len(rom) == len(m1)
    note = (f"opening loop c={c} b={tail_b}: {len(writes)} sites "
            f"{sum(len(v) for v in writes.values())} B in place, "
            f"{len(frags)} B fragments @{FRAG_BASE:04X}")
    return bytes(rom), note
