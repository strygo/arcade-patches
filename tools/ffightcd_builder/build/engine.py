"""The cutscene engine: 68K code emitted into the ROM's free program space.

Everything here is OUR code, not the game's.  It is assembled instruction by
instruction so the addresses can be resolved against whatever the conv
stages produced -- script, palette blocks and deltas all move with the
content, and the engine has to be told where they landed.

The ROM address map below is shared with rom.py, which places these blobs.
"""
from __future__ import annotations
import argparse
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ffcd.cps1 import chunky_to_planar  # noqa: E402
import title_ex

def be16(v): return struct.pack(">H", v & 0xFFFF)
def be32(v): return struct.pack(">I", v & 0xFFFFFFFF)


ENGINE = 0x0D6000

DATA = 0x100000

OFFTAB = DATA

MARGTAB = DATA + 0x240

CAPTAB = DATA + 0x2C0    # caption schedule: (t u16, text-id u16)*, FFFF end

CAPPAL = DATA + 0x380    # caption palettes 6,14,15 at full fade (48 words)

SCRIPT = DATA + 0x0400

# ---- p7 DATA-ROM LAYOUT.  THE ONE PLACE IT IS DEFINED.
# Every consumer derives from these: `place()` writes here, build_engine's
# script/palblocks/deltas bases default to them (so the engine's own
# `lea $xxxxxx.l` immediates are EMITTED from these values, not typed
# twice), and the ending/pan blob chains are computed off them.
#
# The regions are sized for room to move, not to fit.  A tight PALBLOCKS
# window of 0xD000 = 52 blocks has no headroom -- the capture-free JP
# opening needs 52 for itself plus 3 for the ending -- and two more cliffs
# sit a few hundred bytes away, neither failing loudly:
# * BASEMAPS at 0x4000 uses 15,680 in BOTH regions -- 704 bytes of margin,
# and its overflow escape (TAIL_BASEMAP, then the literal 0x180000) points
# INSIDE the opening's own delta span, so taking it silently corrupts deltas.
# * the ending+pan blob chain that trails DELTAS ends ~850 bytes below a
# script escape at 0x1A0000, and the capture-free JP conv pushes it PAST --
# a silent overwrite of the ending's event script.
# So: PALBLOCKS 0x18000 (96 blocks, ~1.85x the largest current use),
# BASEMAPS 0x8000 (2x), and the tail escapes + credits are lifted clear of
# the delta chain.  The p7
# is 1 MB and everything above CREDITS is still free.
PALBLOCKS = DATA + 0x9000       # .. DATA+0x21000  (0x18000 = 96 blocks)

BASEMAPS = DATA + 0x21000       # .. DATA+0x29000  (0x8000)

DELTAS = DATA + 0x29000         # grows into the tail; chained blobs follow

# Position-independent blobs that do not fit their fixed window escape to
# the free tail.  These MUST stay clear of the delta chain above.
TAIL_SCRIPT = DATA + 0xC0000    # oversized ending script (JP: 440 events)

TAIL_BASEMAP = DATA + 0xC8000   # oversized ending basemaps

STATE = 0x00FFFFF0

# label maps of every engine built in this process, in build order (a probe
# aid: rom.py writes them beside engine.lst so a Lua sampler can classify PCs)
LABELS = []


def build_engine(nevents: int, init_cue: int = 0,
                 lead_in: int = 1,
                 sprite_slide: bool = False,
                 jp_title: bool = False,
                 script_base: int = None,
                 palblocks_base: int = None,
                 deltas_base: int = None,
                 magic: int = 0xCAFE,
                 arm_restore: tuple = (0x3B7C, 0xFFFF, 0x92B0),
                 ending_mode: bool = False,
                 ecaps_base: int = None,
                 letterbox: bool = False,
                 lb_tile: int = None,
                 lb_open_at: int = None,
                 patch_base: int = None,
                 objpal_base: int = None, objpal_words: int = 12 * 16,
                 handback_outer: int = 0x0008, handback_layer: int = 0x12C0,
                 gcspr: dict = None,
                 clear_cell: int = 0x40000000,
                 blk_skip: int = 0x54,
                 ovr_chain=None) -> tuple[bytes, int, int]:
    """The cutscene engine: PREPARE IN TASK, COMMIT IN VBLANK.

    Single map buffer (0x90C000, base register CONSTANT -- HBMAME marks the
    whole tilemap dirty on any base change, so a double-buffer flip would
    force a 4096-tile re-decode per event and drop the host below speed, an
    audible audio stutter).

    Every VRAM / palette / CPS-A write the engines make happens INSIDE
    VERTICAL BLANKING, sized to the window.  This is required, not tidiness:
    jtcps1 samples the CPS-A registers per scanline, DMAs scroll2 rows per
    16-line band and OBJ one entry per line, so a write that lands during
    active display shows as a seam, tearing or a one-frame-stale row on
    hardware (MAME renders from end-of-frame state and cannot show it).  A
    tick that runs in TASK context, or a vblank handler that overruns into
    the next frame's active lines, produces exactly those artifacts.

    Structure:
    * tick0 (engine base, the dispatcher's entry, TASK context) is only a
      heartbeat: it clears STATE+8 and, when the script has run out
      (frames-left == 0), runs FIN -- the handback and the title-slide arm.
      It writes no VRAM.
    * `tick` (ISR context) is the per-frame body: countdown/advance, cue,
      palette block copy (movem, only when the block INDEX changes -- fade
      steps are materialised as blocks by rom.py), scroll registers +
      shadows, and the record's map cells: an "early" record's cells now, a
      "late" record's (bit 13, the conversion's >=64-cell events) one vblank
      later via the catch-up (the frame timing the conversions are tuned
      around); then the record's SPILL chunk for this frame (see vbsched.py:
      cells that are not yet visible are spread over later vblanks at build
      time, so the engine keeps no queue).
    * the opening's vblank override runs catch-up + tick itself; the ending
      overrides do too.  Nothing redundant runs per vblank: the 0x900000
      sprite clear is dropped (the live OBJ page is 0x920000 -- the opening
      parks 0x900000 ONCE on its first tick), palette copies run only when
      the index changes, OBJ palette uploads only when the fade level
      changes, captions clear their own cells at their end (no band wipe),
      the side-margin bars draw once at the first vblank, the walker parks
      once on its scene transition, and the letterbox park loop is a movem.
      The title slide's idempotent restores run AFTER its sprite/scroll
      updates and the scroll2 restore rotates 128 cells per vblank, not
      1024.

    * the CPS-A DMAs: the palette copy VRAM -> colour RAM starts on a
      write to the palette base register and the OBJ table copy on a write
      to the OBJ base (both once per frame in the stock handler at $584 /
      $594; jtcps1 stalls the 68000 ~12 lines during the palette copy).
      palhook skips the stock palette-base write while an engine renders
      and the overrides start the copy themselves, at the vblank after
      they changed a palette (the same frame the stock write would publish
      it, so nothing moves by a frame); the OBJ base is written by the $584
      hook only (objhook), the engines drive its shadow.  The vblank hook
      moved from the handler's epilogue ($5E2) to its `jsr $984` at $5A0
      so the override runs before the sound/input calls (~13 lines on
      jtcps1).  Ending-engine INIT leaves the frame counter at the $8000
      sentinel so the caption/letterbox clocks keep the phase the
      subtitle schedules were tuned against (see eovr).

    STATE: F0 magic, F2 idx, F4 frames-left, F6 last-applied (catch-up)
    (nothing beyond F7: the attract manager READS those bytes; F8 is the
    heartbeat, FA+ is game-owned)."""
    # Data bases are parameters so a SECOND engine instance can be built
    # for the ending against its own art (see build_ending_parts.py).
    # Defaults reproduce the opening engine byte-for-byte.
    script_base = SCRIPT if script_base is None else script_base
    # How many entry ticks run with the scrolls forced dark (see the
    # EVENT FADE / layer block below).  Named because the PATCH WALKER has
    # to gate on the same number.
    dark_ticks = 3 if letterbox else 2
    palblocks_base = PALBLOCKS if palblocks_base is None else palblocks_base
    deltas_base = DELTAS if deltas_base is None else deltas_base
    b = bytearray()

    def emit(*bs):
        for x in bs:
            b.extend(x if isinstance(x, (bytes, bytearray)) else be16(x))

    fix = {}

    def bxx(op, name):
        fix.setdefault(name, []).append(len(b))
        emit(op, 0x0000)

    def lea(addr, an):
        emit(0x41F9 | (an << 9), be32(addr))

    def record_a1(dreg=0):
        """a1 = script record for the WORD index in dreg (idx*16 as a LONG:
        adda.w of a sign-extended idx>=2048 gave SCRIPT-32768, an address
        error)."""
        emit(0x48C0 | dreg)                  # ext.l dN
        emit(0xE988 | dreg)                  # lsl.l #4,dN
        lea(script_base, 1)
        emit(0xD3C0 | dreg)                  # adda.l dN,a1

    def emit_paldma(tag):
        """PALETTE DMA START, at the top of the override, BEFORE tick_a.

        On the CPS-A a write to the palette base register starts the copy
        VRAM -> colour RAM (jtcps1: mmr.v pre_copy / dma.v pal_busy; MAME
        cps1_cps_a_w: cps1_build_palette on that write and only then).  The
        stock ISR does that write every vblank at $594, BEFORE our code, so
        a block the engine copies in vblank V is on screen from frame V+1.
        While an engine renders the $594 write is skipped (palhook: on
        jtcps1 the copy stalls the 68000 ~12 lines and it would otherwise
        run on EVERY frame) and the engine writes the base itself: at the
        vblank AFTER a palette changed, i.e. exactly when the stock write
        publishes it, and before this vblank's own block copy so the copy is
        not published a frame early.  Conditions: the record loaded last vblank changed the
        block (or was event 0; a deferred copy -- bit 15 -- one vblank
        later still), or the engine's constant palettes were first written
        last vblank (counter == 1)."""
        emit(0x3028, 0x0002)                 # move.w 2(a0),d0: idx
        emit(0x0C40, 0xFFFF)
        bxx(0x6700, f"pdskip{tag}")                # nothing loaded yet
        emit(0x0C79, 0x0001, be32(0xFF12AE)) # counter == 1: first palettes
        bxx(0x6700, f"pdfire{tag}")                #   were written last vblank
        record_a1(0)
        emit(0x3229, 0x000A)                 # move.w $A(a1),d1: duration
        emit(0x0829, 0x0007, 0x0008)         # btst #7,8(a1): deferred copy?
        bxx(0x6700, f"pdnorm{tag}")
        emit(0x5341)                         # subq.w #1,d1: copied at j == 1
        lab[f"pdnorm{tag}"] = len(b)
        emit(0xB268, 0x0004)                 # cmp.w 4(a0),d1: frames-left
        bxx(0x6600, f"pdskip{tag}")                # not loaded/copied last vblank
        emit(0x4A68, 0x0002)                 # tst.w 2(a0): event 0
        bxx(0x6700, f"pdfire{tag}")
        emit(0x3029, 0x0002)                 # block index vs the previous
        emit(0x3229, 0xFFF2)
        emit(0xB340)                         # eor.w d1,d0
        emit(0x0240, 0x00FF)
        bxx(0x6700, f"pdskip{tag}")                # same block: no copy happened
        lab[f"pdfire{tag}"] = len(b)
        emit(0x33FC, 0x9140, be32(0x80010A)) # start the palette DMA
        lab[f"pdskip{tag}"] = len(b)

    lab = {}
    lab["tick0"] = 0                         # engine base = dispatcher entry
    BUF = 0x90C000
    # ---- DISPATCHER STUB (task context; the outer scene table jumps here
    # every attract frame).  Heartbeat only -- the ovr's liveness watchdog
    # counts STATE+8 up and tears down when the dispatcher goes quiet (coin
    # / test switch).  When the ISR tick has consumed the last event it
    # parks with frames-left == 0 instead of running FIN, and the stub runs
    # FIN here, in TASK context: the handback (outer -> title) is then seen
    # by the dispatcher one frame later, so the title's timeline is
    # unchanged.
    lea(STATE, 0)
    emit(0x0C50, magic)                      # cmpi.w #magic,(a0)
    bxx(0x6600, "tail")
    emit(0x4268, 0x0008)                     # clr.w 8(a0): heartbeat
    if not ending_mode:
        emit(0x4A68, 0x0004)                 # tst.w 4(a0): frames-left
        bxx(0x6700, "fin")                   # 0 -> script over: FIN
    lab["tail"] = len(b)
    emit(0x4E75)                             # rts

    # ---- TICK (ISR context).  a0 = STATE, a5 = $FF8000.  Clobbers d0-d6,
    # a1-a4; RETURNS d7 = flags: bit 0 fresh event load, bit 1 the scroll2
    # palette block index changed (also set on the first event) -- the
    # override blocks gate their own palette uploads on it.  ----------
    lab["tick"] = len(b)
    emit(0x7E00)                             # moveq #0,d7
    lea(0x800140, 1)                         # layer control
    if ending_mode:
        # FORCE the scrolls dark through the first two ticks -- skipping
        # the write is not enough: the stock vblank handler re-copies
        # its own layer shadow (scene value, scrolls ON) before our
        # detour runs, so only an active later write wins the frame.
        # Two dark ticks cover the palette copy's one-frame lag behind
        # the tilemap (presented-frame AVI evidence).  The eovr counter
        # (0xFF12AE, incremented after the tick) reads 0/1 here.
        # THREE dark ticks when the scene wears the letterbox: the OBJ list
        # is fetched a frame behind the tilemap, so releasing the layers at
        # tick 2 showed one frame of the art UNMASKED -- full height, before
        # the bars arrived.  That was the flicker at the scene's start.
        emit(0x0C79, dark_ticks, be32(0xFF12AE))
        bxx(0x6400, "lyon")                   # bhs: settled, normal value
        emit(0x337C, 0x12C0, 0x002E)          # settling: scrolls off
        bxx(0x6000, "noly")
        lab["lyon"] = len(b)
    emit(0x337C, 0x12C6, 0x002E)          # scroll1 ON (captions), scroll3 off
    if ending_mode:
        lab["noly"] = len(b)
    else:
        # ---- OBJ PAGE PARK, ONCE.  The attract's live OBJ page is 0x900000
        # and the phase before the story leaves its sprites there.  Parking
        # it once avoids clearing all 1024 words EVERY tick (18k cycles, in
        # active display).  Measured over every frame of both openings:
        # nothing else writes that page during the story, so park it once, on
        # the first tick after INIT (idx == FFFF and frames-left still ==
        # lead_in), in vblank.  movem: 32 bytes = 4 entries per store.
        emit(0x0C68, 0xFFFF, 0x0002)         # cmpi.w #$FFFF,2(a0)
        bxx(0x6600, "nopark")
        emit(0x0C68, lead_in, 0x0004)        # cmpi.w #lead_in,4(a0)
        bxx(0x6600, "nopark")
        emit(0x203C, be32(0x01F001F0))       # move.l #$01F001F0,d0
        emit(0x2200)                         # move.l d0,d1
        emit(0x2400)                         # d2
        emit(0x2600)                         # d3
        emit(0x2800)                         # d4
        emit(0x2A00)                         # d5
        emit(0x2C00)                         # d6
        emit(0x2E00)                         # d7 (flags: reset below)
        lea(0x900000, 1)
        lea(0x900800, 2)                     # end: 1024 words
        lab["oprk"] = len(b)
        emit(0x48D1, 0x00FF)                 # movem.l d0-d7,(a1)
        emit(0x43E9, 0x0020)                 # lea 32(a1),a1
        emit(0xB3CA)                         # cmpa.l a2,a1
        bxx(0x6500, "oprk")                  # blo
        emit(0x7E00)                         # moveq #0,d7
        lab["nopark"] = len(b)
    emit(0x7800)                             # moveq #0,d4
    emit(0x3028, 0x0002)                     # idx
    emit(0x0C40, 0xFFFF)
    bxx(0x6700, "count")
    record_a1(0)
    lab["count"] = len(b)
    emit(0x3028, 0x0004)                     # move.w 4(a0),d0: frames-left
    if not ending_mode:
        # 0 = the ISR tick has consumed the whole script and parked (see
        # the stub): hold the last record, no countdown
        bxx(0x6700, "vis")
    emit(0x5340)                             # subq.w #1,d0
    emit(0x3140, 0x0004)                     # move.w d0,4(a0)
    bxx(0x6600, "vis")
    emit(0x3028, 0x0002)                     # move.w 2(a0),d0
    emit(0x5240)                             # addq.w #1,d0: next event
    emit(0x0C40, nevents)
    bxx(0x6C00, "fin" if ending_mode else "vis")   # past the end
    emit(0x3140, 0x0002)                     # advance
    record_a1(0)
    emit(0x3169, 0x000A, 0x0004)             # duration
    emit(0x3229, 0x0002)                     # cue
    emit(0xE049)
    emit(0x670A)                             # beq.s past the call
    emit(0x3001)
    emit(0x2F08)                             # move.l a0,-(sp): $9d0 clobbers a0
    emit(0x4EB8, 0x09D0)
    emit(0x205F)                             # movea.l (sp)+,a0
    emit(0x7801)                             # moveq #1,d4
    lab["vis"] = len(b)
    emit(0x0C68, 0xFFFF, 0x0002)             # pre-first?
    bxx(0x6700, "trts")
    # ---- PALETTE BLOCK.  Copied ONLY when a fresh load changes the block
    # index (or on event 0): identical-value rewrites cost the whole
    # vblank for nothing.  Copying 512 words on every fresh load would be
    # 11k cycles a frame, since every 1-frame pan event IS a fresh load.
    # EVENT FADE (record word-0 top nibble, code c = level 15-c) is
    # materialised by rom.py as its own palette block (vbsched), so a fade
    # step is an index change here; the OBJ-palette blocks in the overrides
    # still scale by the record's nibble at run time.
    # If something outside this engine ever clobbers the staging area at
    # $914800, the symptom is wrong colours that persist rather than
    # self-heal -- visible immediately, not silent.
    # DEFERRED COPY (record bit 15, set by rom.py only when it has verified
    # that every cell visible on the load frame renders identically under
    # the old and the new block -- the JP ending's black-out cuts): the copy
    # moves to the record's first non-load vblank so the load vblank has
    # the whole budget for the map.
    emit(0x4A84)                             # tst.l d4
    bxx(0x6700, "palnf")                     # not a fresh load
    emit(0x7E01)                             # moveq #1,d7: fresh load
    emit(0x0829, 0x0007, 0x0008)             # btst #7,8(a1): word bit 15
    bxx(0x6600, "paldn")                     # deferred: not now
    emit(0x4A68, 0x0002)                     # tst.w 2(a0): idx 0?
    bxx(0x6700, "palgo")
    emit(0x3029, 0x0002)                     # move.w 2(a1),d0   pal|cue
    emit(0x3229, 0xFFF2)                     # move.w -14(a1),d1 (prev record)
    emit(0xB340)                             # eor.w d1,d0
    emit(0x0240, 0x00FF)                     # andi.w #$FF,d0
    bxx(0x6700, "paldn")                     # same block: nothing to copy
    bxx(0x6000, "palgo")
    lab["palnf"] = len(b)
    emit(0x0829, 0x0007, 0x0008)             # btst #7,8(a1): deferred copy?
    bxx(0x6700, "paldn")
    emit(0x3029, 0x000A)                     # move.w $A(a1),d0: duration
    emit(0x9068, 0x0004)                     # sub.w 4(a0),d0 -> j
    emit(0x0C40, 0x0001)                     # first non-load vblank?
    bxx(0x6600, "paldn")
    lab["palgo"] = len(b)
    emit(0x7E03)                             # moveq #3,d7: fresh + palette
    emit(0x7200)
    emit(0x3229, 0x0002)
    emit(0x0241, 0x00FF)
    emit(0xE189)
    emit(0xE589)                             # d1 = block*1024
    lea(palblocks_base, 2)
    emit(0xD5C1)                             # adda.l d1,a2
    lea(0x914800, 3)
    # 1024 bytes as 36 x movem.l of 7 longs (1008) + 4 longs: 5.0k cycles
    # instead of the move.w loop's 11.3k.  (The loop bound is an address
    # register: the movem loads d0-d6, so no data register survives it.)
    emit(0x49EB, 0x03F0)                     # lea 1008(a3),a4: end
    lab["pal"] = len(b)
    emit(0x4CDA, 0x007F)                     # movem.l (a2)+,d0-d6
    emit(0x48D3, 0x007F)                     # movem.l d0-d6,(a3)
    emit(0x47EB, 0x001C)                     # lea 28(a3),a3
    emit(0xB7CC)                             # cmpa.l a4,a3
    bxx(0x6500, "pal")                       # blo
    for _ in range(4):
        emit(0x26DA)                         # move.l (a2)+,(a3)+
    # (the palette becomes visible when the palette DMA is started -- see
    # the PALETTE DMA block at the top of the overrides: one vblank later,
    # as before)
    lab["paldn"] = len(b)
    emit(0x4E75)                             # rts (end of tick_a)
    # ---- TICK B (ISR context): scroll registers + shadows, then the map
    # cells.  a0 = STATE; the record and the fresh-load flag are recomputed
    # here (the overrides do their OBJ work between the two halves).
    lab["tickb"] = len(b)
    emit(0x0C68, 0xFFFF, 0x0002)             # pre-first: nothing to do
    bxx(0x6700, "trts")
    # a1 = current record; d4 = fresh load (frames-left still == duration:
    # tick_a set it at the load, every later tick has counted it down)
    emit(0x3028, 0x0002)
    record_a1(0)
    emit(0x7800)                             # moveq #0,d4
    emit(0x3029, 0x000A)                     # move.w $A(a1),d0
    emit(0xB068, 0x0004)                     # cmp.w 4(a0),d0
    emit(0x6602)                             # bne.s +2
    emit(0x7801)                             # moveq #1,d4
    # scroll x/y + constant base pin
    lea(0x800100, 2)
    emit(0x3569, 0x000C, 0x0010)
    emit(0x3569, 0x000E, 0x0012)
    emit(0x357C, BUF >> 8, 0x0004)           # move.w #$90C0,4(a2)
    emit(0x357C, 0xFFF0, 0x000E)             # scroll1 y: captions 16px down
    emit(0x357C, 0x0000, 0x000C)             # scroll1 x: center captions (-64px)
    # ---- AND THE SHADOWS.  The four writes above set the CPS-A registers
    # directly, but the stock vblank handler re-derives those same four
    # registers from work-RAM shadows every frame ($584 -> $5e8):
    #
    #   $80010C <- $26(a5)              scroll1 x
    #   $80010E <- $28(a5)              scroll1 y
    #   $800110 <- $2e(a5) + $FFC0      scroll2 x   (-64)
    #   $800112 <- $300 - $30(a5)       scroll2 y   (inverted)
    #
    # and then promotes a pending pair over it ($22->$26, $24->$28,
    # $2a->$2e, $2c->$30).  Writing only the registers therefore races the
    # handler every frame: when its recopy lands last the scroll snaps back
    # to the parked shadow, and when it lands mid-frame only the lines below
    # it move -- a horizontal band that alternates between the right place
    # and one frame behind, plus tearing across the slide.
    #
    # So drive the shadows too, pre-transformed so the handler reproduces
    # exactly what we just wrote, and set the pending pair to match so the
    # promote is a no-op either side of it.  This is the same both-places
    # rule the OBJ base ($800100 + $FF809E) and layer control ($2E + $6E)
    # already follow; scroll was the one left raw.  Absolute addresses: the
    # tick is shared by every override.
    # The shadows are written ONE RECORD AHEAD (the record that will be
    # LIVE next frame -- the patch walker's pwsy peek, same conditions):
    # the $59C handler write then publishes next frame's scroll at the TOP
    # of its vblank (core vdump ~241, fixed cost), before jtcps1 fetches
    # the frame's first scroll2 row bands in the TAIL of blanking
    # (jtcps1_dma.v: tile_ok = vrender1<240 || vrender1>257, so the top
    # bands' row fetches -- which latch coarse hpos2[10:4] and the map row
    # -- run at vdump ~256..13, and this tick's own register write below
    # lands AFTER some of them whenever the vblank runs long on the core's
    # slower SDRAM path: the value change then straddles the fetches and
    # the pans stutter on hardware while MAME's end-of-frame render shows
    # nothing).  The direct register writes above still publish THIS
    # frame's values -- by the time they land the registers already hold
    # the same values from the handler's early write, so they only matter
    # on the engine's first frame (inside the dark/settle window).
    for off in (0x0022, 0x0026):             # scroll1 x = 0
        emit(0x33FC, 0x0000, be32(0xFF8000 + off))
    for off in (0x0024, 0x0028):             # scroll1 y = $FFF0
        emit(0x33FC, 0xFFF0, be32(0xFF8000 + off))
    emit(0x3228, 0x0002)                     # move.w 2(a0),d1: idx
    emit(0x0C68, 0x0001, 0x0004)             # frames-left == 1: this is the
    bxx(0x6600, "sxcur")                     #   record's last frame?
    emit(0x0C41, nevents - 1)                # already the last event?
    bxx(0x6700, "sxcur")                     #   (hold: tick_a will not advance)
    emit(0x5241)                             # addq.w #1,d1: the NEXT record
    lab["sxcur"] = len(b)
    emit(0x48C1)                             # ext.l d1
    emit(0xE989)                             # lsl.l #4,d1
    lea(script_base, 3)
    emit(0xD7C1)                             # adda.l d1,a3 = next-live record
    emit(0x302B, 0x000C)                     # move.w $0C(a3),d0   (sx)
    emit(0x0640, 0x0040)                     # addi.w #$40,d0      undo the -64
    for off in (0x002A, 0x002E):
        emit(0x33C0, be32(0xFF8000 + off))   # move.w d0,(shadow).l
    emit(0x303C, 0x0300)                     # move.w #$300,d0
    emit(0x906B, 0x000E)                     # sub.w $0E(a3),d0    undo 300-y
    for off in (0x002C, 0x0030):
        emit(0x33C0, be32(0xFF8000 + off))
    # ---- MAP CELLS.  Fresh load: an EARLY record's now-cells land in this
    # vblank; a LATE record's (bit 13) land in the next one through the
    # override's catch-up (opening only -- the ending overrides call the
    # tick every vblank and commit at load; rom.py never sets bit 13 for
    # them).  Then, on the record's later frames, its spill chunk.
    emit(0x4A84)                             # tst.l d4
    bxx(0x6700, "spill")
    if not ending_mode:
        # (btst on a memory operand tests bit n of the BYTE: word bit 13 =
        # bit 5 of the count word's high byte at 8(a1); bit 14 = bit 6)
        emit(0x0829, 0x0005, 0x0008)         # btst #5,8(a1): late record
        bxx(0x6600, "trts")                  # -> catch-up owns it
    lea(BUF, 4)
    bxx(0x6100, "delta")                     # bsr.w delta (now-cells)
    bxx(0x6000, "trts")
    lab["spill"] = len(b)
    # spill chunk j = duration - frames-left (>= 1 here: a fresh load has
    # j == 0 and took the branch above); the list follows the now-cells
    # when bit 14 is set: (n u16, n cells)* terminated by n = $FFFF
    emit(0x0829, 0x0006, 0x0008)             # btst #6,8(a1): word bit 14
    bxx(0x6700, "trts")
    emit(0x3029, 0x000A)                     # move.w $A(a1),d0: duration
    emit(0x9068, 0x0004)                     # sub.w 4(a0),d0 -> j
    bxx(0x6F00, "trts")                      # ble: j <= 0 (paranoia)
    emit(0x2229, 0x0004)                     # move.l 4(a1),d1
    lea(deltas_base, 2)
    emit(0xD5C1)                             # adda.l d1,a2
    emit(0x3429, 0x0008)                     # move.w 8(a1),d2: count|flags
    emit(0x0242, 0x1FFF)                     # andi.w #$1FFF,d2
    emit(0x48C2)                             # ext.l d2
    emit(0x2602)                             # move.l d2,d3
    emit(0xE38B)                             # lsl.l #1,d3    (x2)
    emit(0xE58A)                             # lsl.l #2,d2    (x4)
    emit(0xD483)                             # add.l d3,d2    (x6)
    emit(0xD5C2)                             # adda.l d2,a2   -> spill list
    emit(0x5340)                             # subq.w #1,d0: chunks to skip
    bxx(0x6000, "spskt")
    lab["spsk"] = len(b)
    emit(0x341A)                             # move.w (a2)+,d2  n
    emit(0x0C42, 0xFFFF)
    bxx(0x6700, "trts")                      # end of list
    emit(0x48C2)                             # ext.l d2
    emit(0x2602)
    emit(0xE38B)
    emit(0xE58A)
    emit(0xD483)                             # d2 = n*6
    emit(0xD5C2)                             # adda.l d2,a2
    lab["spskt"] = len(b)
    bxx(0x51C8, "spsk")                      # dbf d0
    emit(0x341A)                             # move.w (a2)+,d2  this chunk's n
    emit(0x0C42, 0xFFFF)
    bxx(0x6700, "trts")
    emit(0x4A42)                             # tst.w d2
    bxx(0x6700, "trts")                      # empty chunk
    lea(BUF, 4)
    bxx(0x6100, "dl")                        # bsr.w dl (d2 cells at a2)
    lab["trts"] = len(b)
    emit(0x4E75)
    # ---- delta subroutine: apply record (a1) now-cells to (a4) ---------
    # count = 8(a1) & $1FFF; cells at deltas_base + 4(a1).  Then `dl`:
    # d2 = count (>= 1), a2 = cells, a4 = BUF.  Each cell is (offset,
    # code, attr): move.w + one move.l for the code/attr pair (26 vs 2 x
    # 18), in an 8-way unrolled loop entered Duff-style so the dbf costs
    # 1.25 cycles a cell instead of 10: ~35 cycles a cell against 54.
    lab["delta"] = len(b)
    emit(0x3429, 0x0008)
    emit(0x0242, 0x1FFF)
    bxx(0x6700, "drts")
    emit(0x2229, 0x0004)
    lea(deltas_base, 2)
    emit(0xD5C1)
    lab["dl"] = len(b)
    emit(0x5342)                             # subq.w #1,d2   (n-1)
    emit(0x3602)                             # move.w d2,d3
    emit(0xE64A)                             # lsr.w #3,d2    passes-1
    emit(0x0243, 0x0007)                     # andi.w #7,d3   (n-1)&7
    emit(0x0A43, 0x0007)                     # eori.w #7,d3   units to skip
    emit(0xD643)                             # add.w d3,d3    x2
    emit(0x3803)                             # move.w d3,d4
    emit(0xD643)                             # x4
    emit(0xD644)                             # add.w d4,d3    x6 = bytes
    emit(0x4EFB, 0x3002)                     # jmp 2(pc,d3.w)
    lab["dl8"] = len(b)
    for _ in range(8):
        emit(0x301A)                         # move.w (a2)+,d0
        emit(0x299A, 0x0000)                 # move.l (a2)+,0(a4,d0.w)
    bxx(0x51CA, "dl8")                       # dbf d2
    lab["drts"] = len(b)
    emit(0x4E75)
    lab["fin"] = len(b)
    if ending_mode:
        # script done: hand the ending back to the stock sequencer at the
        # segment boundary this engine replaced (CP-2.5, and the phase map in
        # work/capture/arcade_ending_phase_map.tsv), restore that segment's
        # entry layer control, and die.  a5 = 0xFF8000 (set by the ending ovr
        # before it calls the tick).
        # The value is PER ENGINE: the reunion (seg A) hands to outer 0x08 =
        # the credits, the farewell (seg G) to outer 0x0E = the gag cards.
        # A farewell that handed to 0x08 would return to the credits, which
        # loop round to the farewell again -- an endless loop after the END
        # card.
        emit(0x3B7C, handback_outer, 0x9288)   # outer
        emit(0x426D, 0x928A)                 # clr.w inner
        emit(0x422D, 0x008D)                 # clr.b sequence flag
        emit(0x3B7C, handback_layer, 0x006E)   # layer shadow -> seg entry
        lea(0x800140, 1)
        emit(0x337C, handback_layer, 0x002E)   # layer control now
        # obj base back to the ending context's stock value (0x9040,
        # probe-verified; blk parked it).  SHADOW ONLY: the stock handler
        # pushes it at $584 next vblank -- on jtcps1 every $800100 write is
        # an OBJ-DMA restart, and a second one in a frame puts the
        # half-copied bank on screen (RTL read).
        emit(0x33FC, 0x9040, be32(0xFF809E))
        # scroll1 Y back to stock.  The tick holds it at $FFF0 (captions
        # 16 px down) every vblank, and the stock code inherits the shadow:
        # every stock text screen drawn on scroll1 after the handback -- the
        # JP paragraph after the reunion, the US name entry after the
        # farewell -- lands 16 px low until something else writes it.  Both
        # the pending and current halves, so the $5E8 promote cannot bring
        # $FFF0 back a frame later.  (x is already 0 = stock.)
        emit(0x426D, 0x0024)                 # clr.w $24(a5)  s1 y pending
        emit(0x426D, 0x0028)                 # clr.w $28(a5)  s1 y current
        if not letterbox:
            # The side-margin bars ride scroll1 VRAM and their black palette
            # stops being asserted at the handback -- the stale cells render
            # as coloured side blocks through the CREDITS (user).  Wipe them
            # on the way out.
            for gi, cbase in enumerate((8, 52)):
                lea(0x908000 + cbase * 0x80 + 4 * 4, 4)
                emit(0x323C, 0x0003)         # d1 = 4 columns - 1
                lab[f"mgx{gi}"] = len(b)
                emit(0x740F)                 # moveq #15,d2 (16 rows)
                lab[f"mgy{gi}"] = len(b)
                emit(0x429C)                 # clr.l (a4)+
                bxx(0x51CA, f"mgy{gi}")
                emit(0xD8FC, 0x0080 - 16 * 4)
                bxx(0x51C9, f"mgx{gi}")
        emit(0x30BC, 0xDEAD)                 # engine off
        bxx(0x6000, "trts")
    else:
        # (task context, from the stub: a5 = $FF8000, the dispatcher's)
        emit(0x3B7C, 0x000A, 0x9288)
        emit(0x426D, 0x928A)
        if sprite_slide:
            # Arm the sprite slide instead of dying.  The stock
            # title phase stamps its ~120 sprites within 1-2 frames; the
            # vblank slide block snapshots their resting Y at n==2 (title
            # still <15% through the $81F fade = invisible), then glides
            # the ARCADE sprites in from off-screen: logo down from the
            # top, wall/rubble up from the bottom, absolute writes from
            # the snapshot (the OBJ list is measured-static, so nothing
            # accumulates).  scroll1 is held OFF (0x12CC) until landing so
            # INSERT COIN/(c) appear only at the very end.  STATE: +A =
            # slide frame counter, +C = logo travel, +E = wall travel
            # (+8 stays the heartbeat).
            # final home for slide state: STATE's own F2/F4/F6
            # cells, which are FREE once the script ends (idx/frames/last).
            # magic 0x51DE marks slide mode (a third state next to CAFE and
            # DEAD).  Nothing else works: FFFFFA+ is game-owned (writing it
            # reached the test menu), and BUF rows 32+ get rewritten by the
            # stock full-page clears (armed the gate with garbage).
            emit(0x30BC, 0x51DE)                 # magic = slide mode
            emit(0x317C, 0x0000, 0x0004)         # F4: frame counter = 0
            emit(0x317C, 0x00A8, 0x0002)         # F2: wall travel = 168
            emit(0x317C, 0x00D0, 0x0006)         # F6: logo travel = 208 (52
                                                 # steps at 4px/frame -- lands
                                                 # well before the title-anim
                                                 # hook dies; logo bottom is at
                                                 # map y 192 so >=192 still
                                                 # starts fully off-screen)
            # build a fully-parked sprite list in the unused VRAM page at
            # $920000 and point the OBJ base (reg $800100 + its shadow
            # $9E(a5), recopied every vblank by the handler at $584) at it.
            # The title init stamps the real list at $900000 on its own
            # clock inside the settle window, and with the render's +2
            # frame latency our epilogue writes can never blank that frame
            # -- swapping the base is the only latch-proof hide.  a5 is
            # NOT $FF8000 in tick context, so the shadow is written by
            # absolute address ($FF8000+$9E).  (Task context, active
            # display: the page is not displayed until the base switch, and
            # both pages are blank at this point -- nothing to tear.)
            lea(0x920000, 2)
            # ALL 256 entries: parking only 128 leaves entries 128-255
            # holding stale in-range codes (0x3205/0x3C01/0x3C02) that flash
            # as a 256x48 strip for one frame at the post-game
            # opening->title handoff, under every mapper.
            emit(0x363C, 0x00FF)                 # d3 = 255
            lab["fprk"] = len(b)
            emit(0x34BC, 0x01F0)                 # x = parked
            emit(0x426A, 0x0002)
            emit(0x426A, 0x0004)
            emit(0x426A, 0x0006)
            emit(0x508A)
            bxx(0x51CB, "fprk")
            emit(0x33FC, 0x9200, be32(0xFF809E))  # obj base SHADOW -> parked
                                                  # page (the register is the
                                                  # $584 hook's, next vblank:
                                                  # one OBJ pulse per frame)
            emit(0x317C, 0xFFFF, 0x0008)          # F8 = liveness sentinel
                                                  # for the blink gate
        else:
            lea(0x800140, 1)
            emit(0x337C, 0x12CE, 0x002E)
            emit(0x30BC, 0xDEAD)
        emit(0x4E75)
    # ---- ENDING OVERRIDE (ending_mode instances only) -------------------
    # The ending has no per-frame dispatcher (the attract tick never runs),
    # so the whole engine is driven from the vblank override: engine1's
    # ovr chain jumps here on magic CAFD.  Registers are still on the
    # interrupt frame (ovrx restores them), so everything is free to use.
    lab["eovr"] = len(b)
    if ending_mode:
        lea(STATE, 0)
        emit(0x0C50, magic)
        bxx(0x6600, "ovrx")                  # not ours -> epilogue
        # ---- FRAME COUNTER PHASE.  INIT ends by clearing the counter.  The
        # subtitle schedules are tuned to a phase where the caption /
        # letterbox / dark-tick clocks start ONE vblank after INIT's first
        # frame -- the phase a heavy INIT (16 KB map clear + 16 KB scroll1
        # clear + OBJ park in task context) produces when its clear straddles
        # into the second frame.  This override is short and INIT ends in its
        # first frame, so to reproduce that phase INIT leaves the SENTINEL
        # $8000 and this vblank turns it into a 0 without counting -- the same
        # values on the same frames.
        emit(0x387C, 0x0000)                 # movea.w #0,a4: count this vblank
        emit(0x0C79, 0x8000, be32(0xFF12AE)) # cmpi.w #$8000,counter
        emit(0x660A)                         # bne.s +10
        emit(0x4279, be32(0xFF12AE))         # clr.w counter
        emit(0x387C, 0x0001)                 # movea.w #1,a4: skip the count
        emit(0x2C4C)                         # movea.l a4,a6 (a4 is scratch below)
        emit_paldma("e")                     # publish last vblank's palettes
        lea(0xFF8000, 5)                     # a5: fin + cue paths need it
        bxx(0x6100, "tick")                  # bsr.w: advance / cue / palette
        # (tick_b -- scroll registers + map cells -- runs after the OBJ
        # work below, see the note there)
        # The tick may have just run FIN (handback + DEAD) --
        # skip the tail on that vblank, or its bar/palette stamps rewrite
        # the very cells fin's wipe cleared and the credits inherit them
        # (user: "a visual defect on the sides in the credit sequence")
        emit(0x0C50, 0xDEAD)                 # cmpi.w #$DEAD,(a0)
        bxx(0x6700, "ovrx")                  # beq: engine just died
        # keep seg A parked under us: its beat code clears the busy flag at
        # the beat's own end; re-assert every vblank while we own the
        # screen.  After fin the magic is DEAD and this path is skipped.
        emit(0x13FC, 0x0001, be32(0xFF808D))
        # Frame counter: 0xFF12AE, cleared by INIT, +1 every vblank -- the
        # same cell the opening pump uses.  Counted up HERE (nothing between
        # here and the palette blocks reads it) so the once-only blocks below
        # can test t == 1 = the first vblank after INIT.  d6 = t for the rest
        # of the override.
        lea(0xFF12AE, 3)                     # lea counter,a3
        emit(0x300E)                         # move.w a6,d0: sentinel vblank?
        emit(0x6602)                         # bne.s +2: yes -> no count
        emit(0x5253)                         # addq.w #1,(a3)
        emit(0x3C13)                         # move.w (a3),d6
        # re-assert the caption palettes (6, 14, 15) at full fade every
        # vblank, exactly like engine1's cpal block: the stock scene build
        # (which this engine skips) runs the $81F fade-in that restores them,
        # so without this the subtitles render in the half-faded palettes
        # left by the bonus-screen fade-out -- the "darkened font".
        lea(CAPPAL, 4)                       # lea CAPPAL,a4
        lea(0x9144C0, 3)                     # pal 6
        for _ in range(8):
            emit(0x26DC)                     # move.l (a4)+,(a3)+
        lea(0x9145C0, 3)                     # pals 14+15 (contiguous)
        for _ in range(16):
            emit(0x26DC)
        if not letterbox:
            # side-margin bar palette (scroll1 palette 1, all black) -- see
            # the SIDE MARGIN BARS block below for the bars themselves
            lea(0x914420, 4)
            emit(0x203C, be32(0xF000F000))
            for _ in range(8):
                emit(0x28C0)                 # move.l d0,(a4)+  black
        if gcspr:
            # character OBJ palettes (Cody=1, Guy=2), re-asserted alongside
            # the caption palettes for the same reason.  the copy is
            # SCALED by the current event's fade code so the walker fades
            # with the scene -- unscaled, the characters stayed lit over
            # the blacked background through the whole 133-frame tail
            # event ("Guy standing alone on black", user).
            # Re-asserted on the engine's first vblank, at the scene's first
            # event, and then only when a fresh load inside the scene changed
            # the FADE code (the level is all the copy depends on; block
            # changes at the same level -- the scene cuts, the heaviest
            # vblanks -- skip it, and so does everything before the scene,
            # where the page is parked).  Not every vblank.
            emit(0x0C46, 0x0001)             # cmpi.w #1,d6: first vblank
            bxx(0x6700, "gcpgo")
            emit(0x0807, 0x0000)             # btst #0,d7: fresh load?
            bxx(0x6700, "gcpx")
            emit(0x3039, be32(STATE + 2))    # move.w idx,d0
            emit(0x0C40, gcspr["first_evt"])
            bxx(0x6D00, "gcpx")              # blt: before the scene
            bxx(0x6700, "gcpgo")             # the scene's first event
            emit(0x0C40, gcspr["first_evt"] + gcspr["n_evt"])
            bxx(0x6C00, "gcpx")              # bge: past the scene
            record_a1(0)
            emit(0x3029, 0x0000)             # move.w 0(a1),d0
            emit(0x3229, 0xFFF0)             # move.w -16(a1),d1  (prev)
            emit(0xB340)                     # eor.w d1,d0
            emit(0x0240, 0xF000)             # andi.w #$F000,d0: fade nibble
            bxx(0x6700, "gcpx")              # same level: keep the palettes
            lab["gcpgo"] = len(b)
            emit(0x7600)                     # moveq #0,d3
            emit(0x3039, be32(STATE + 2))    # move.w idx,d0
            emit(0x0C40, 0xFFFF)
            bxx(0x6700, "gcpf")              # pre-first: full bright
            record_a1(0)                     # a1 = current record
            emit(0x3629, 0x0000)             # move.w 0(a1),d3
            emit(0xE04B)                     # lsr.w #8,d3
            emit(0xE84B)                     # lsr.w #4,d3 -> fade code
            lab["gcpf"] = len(b)
            emit(0x0A43, 0x000F)             # eori.w #$F,d3: level
            emit(0xE14B)                     # lsl.w #8,d3
            emit(0xE94B)                     # lsl.w #4,d3 -> level<<12
            lea(gcspr["opals"], 4)
            lea(0x914020, 3)                 # OBJ pal 1
            emit(0x743F)                     # moveq #63,d2  (2 pals x 32B)
            lab["gcp1"] = len(b)
            emit(0x301C)                     # move.w (a4)+,d0
            emit(0x0240, 0x0FFF)             # andi.w #$0FFF,d0
            emit(0x8043)                     # or.w d3,d0
            emit(0x36C0)                     # move.w d0,(a3)+
            bxx(0x51CA, "gcp1")
            lab["gcpx"] = len(b)
        # ---- CD-timed subtitle channel.  The stock ending typewriter is
        # silenced at its enqueue sites (0x186C0 / 0x1871A patches in
        # main); the engine draws the dialogue itself as subtitles under
        # the CD art.  Probe facts: the ending types straight into page
        # 0x908000 at the string table's own cells, tile = 0x4400 + ASCII;
        # screen = map (col-8, row-2).  (The real ending glyphs are 0x44xx,
        # not the 0x49xx attract-font leftovers; no cols-72..111 wipe is
        # needed.)
        lea(0xFF12AE, 3)                     # a3 = counter (ecaps read it)
        if letterbox:
            # LETTERBOX (scrolling-art instances).  A scrolling layer cannot
            # hold a fixed mask -- the art moves across the whole screen --
            # so the CD's 128 px window is restored with SPRITES, which sit
            # above every scroll layer and do not scroll with scroll2.
            # scroll1 was tried first and abandoned: this phase's font bank
            # has no fully opaque 8x8 tile (0x40C0 is opaque during the
            # reunion, transparent here; 0x4600 is a dither).
            # CPS1 multi-tile sprites (attr bits 8-11 = extra tiles X,
            # 12-15 = extra Y) cover both bars in FOUR entries.  OBJ
            # coordinates measured in-phase: screen_y = sprite_y - 16,
            # screen_x = sprite_x.  OBJ transparency is pen 15, so the
            # letterbox tile must be a single-pen tile with pen != 15
            # (computed from the conversion and passed in as lb_tile);
            # OBJ palette 0 is forced black so whichever pen it uses reads
            # black.  INIT already parked the OBJ page at 0x920000, which
            # is the live OBJ base for the whole scene.
            lea(0x914000, 4)                 # OBJ palette 0
            emit(0x203C, be32(0xF000F000))
            for _ in range(8):
                emit(0x28C0)                 # move.l d0,(a4)+
            if objpal_base:
                # OBJ palettes 1.. carry the patch sprites' own colours
                # (palette 0 stays the letterbox black).
                # the patch palettes take the event fade level
                # too, so a fade-out dims the figures with the background
                # instead of leaving them lit over black.  This is the
                # take-over walker's own sequence (gcpf, ~line 429) applied
                # to the pan's OBJ block -- note it computes the code into d3
                # and falls THROUGH to `eori #$F`, so code 0 becomes level 15
                # naturally.  Branching PAST the eori with d3 still holding
                # the code would upload every colour at level 0: characters at
                # 0.535 of the VM's brightness against a background at 0.993.
                # Uploaded on block changes only (d7 bit 1; event 0 included),
                # and a level-15 upload is a plain movem copy -- rom.py stores
                # the blob pre-masked to level 15.  Scaling 480 words word by
                # word would be 18k cycles EVERY vblank.
                emit(0x0807, 0x0001)             # btst #1,d7
                bxx(0x6700, "opalx")
                emit(0x7600)                     # moveq #0,d3
                emit(0x3039, be32(0xFFFFF2))     # move.w idx,d0
                emit(0x0C40, 0xFFFF)
                bxx(0x6700, "opalf")             # pre-first: full bright
                record_a1(0)                     # a1 = current record
                emit(0x3629, 0x0000)             # move.w 0(a1),d3
                emit(0xE04B)
                emit(0xE84B)                     # d3 = fade code
                lab["opalf"] = len(b)
                lea(0x914020, 4)                 # OBJ pal 1
                lea(objpal_base, 5)
                emit(0x4A43)                     # tst.w d3: code 0?
                bxx(0x6600, "opals")             # scaled path
                nlong = objpal_words // 2
                # 6 longs a movem (d6 = t and d7 = flags stay intact), bound
                # in a6 (the movem clobbers every data register it loads)
                emit(0x4DEC, (nlong // 6) * 24)  # lea end(a4),a6
                lab["opalm"] = len(b)
                emit(0x4CDD, 0x003F)             # movem.l (a5)+,d0-d5
                emit(0x48D4, 0x003F)             # movem.l d0-d5,(a4)
                emit(0x49EC, 0x0018)             # lea 24(a4),a4
                emit(0xB9CE)                     # cmpa.l a6,a4
                bxx(0x6500, "opalm")             # blo
                for _ in range(nlong % 6):
                    emit(0x28DD)                 # move.l (a5)+,(a4)+
                bxx(0x6000, "opald")
                lab["opals"] = len(b)
                emit(0x0A43, 0x000F)             # eori #$F: level = 15-code
                emit(0xE14B)
                emit(0xE94B)                     # level << 12
                emit(0x343C, objpal_words - 1)
                lab["opal2"] = len(b)
                emit(0x301D)                     # move.w (a5)+,d0
                emit(0x0240, 0x0FFF)             # strip its own level nibble
                emit(0x8043)                     # or.w d3,d0
                emit(0x38C0)                     # move.w d0,(a4)+
                bxx(0x51CA, "opal2")
                lab["opald"] = len(b)
                lea(0xFF8000, 5)                 # a5 back: fin/cue need it
                lab["opalx"] = len(b)
            lea(0x920000, 4)                     # OBJ page = 4 bar entries
            # bars built from SMALL sprites: a tall multi-sprite gets its
            # tile run truncated (a 16x4 bottom bar left a lit strip while
            # the identical 16x2 top bar was clean), so every entry here is
            # one tile tall.
            # OBJ origin measured in-phase: screen_x = sprite_x - 64,
            # screen_y = sprite_y - 16.  The -64 matters: side-margin sprites
            # at sprite_x 352 land on screen 288..319, so a wrong origin masks
            # 64 px of real art and reads as the scene cropped narrower than
            # the CD.
            # Only the top/bottom bars are needed -- the art spans screen
            # x 32..351 and the map outside it is the transparent clear.
            # The CD OPENS its window near the end (measured: art rows
            # 32..159 through f001063, then 32..215 from f001064 -- the
            # farewell's feet/shadow animation lives in the newly revealed
            # strip).  So the bottom bar retracts at lb_open_at: keeping it
            # shut hid exactly the animation this scene is about.
            top_bars, bot_bars, open_bars = [], [], []
            for y in (16, 32):                       # top: screen 0..31
                top_bars += [(64, y, 15, 0), (320, y, 7, 0)]
            for y in (176, 192, 208, 224):           # bottom: screen 160..223
                bot_bars += [(64, y, 15, 0), (320, y, 7, 0)]
            open_bars = [(64, 232, 15, 0), (320, 232, 7, 0)]   # screen 216..
            def emit_bars(lst, pad_to=None):
                for x, y, nx, ny in lst:
                    emit(0x28FC, be32((x << 16) | y))              # move.l #x:y,(a4)+
                    emit(0x28FC, be32((lb_tile << 16) | (ny << 12) | (nx << 8)))
                for _ in range(len(lst), pad_to or len(lst)):
                    emit(0x28FC, be32(0x01F00000))   # parked x
                    emit(0x28FC, be32(0x00000000))
            emit_bars(top_bars)
            if lb_open_at:
                emit(0x0C46, lb_open_at)                   # cmpi.w #t,d6
                bxx(0x6400, "lbopen")                      # bhs -> window open
            emit_bars(bot_bars)
            if lb_open_at:
                bxx(0x6000, "lbdone")
                lab["lbopen"] = len(b)
                emit_bars(open_bars, pad_to=len(bot_bars))
                lab["lbdone"] = len(b)
            # DYNAMIC ENTRIES FROM SLOT 40.  The CPS-A copies the OBJ table
            # one entry per scanline from the frame's OBJ-base write (jtcps1
            # dma.v: entry k at line ~241+k), i.e. entries 0..~37 are read
            # BEFORE a vblank override running to line 38 can update them
            # and would show one frame stale.  Slots 12..39 stay parked
            # (INIT parked the page; nothing writes them), the patch sprites
            # start at 40 (max 88 live: the table holds fewer records).
            lea(0x920000 + 40 * 8, 4)
            if patch_base:
                # NOTHING ON THE OBJ PAGE UNTIL THE PLANE IS UP.
                # The entry holds the scrolls dark for `dark_ticks` frames
                # (the letterbox rule above), but the OBJ layer is NOT part
                # of that shadow -- so the walker happily populated the page
                # through the dark ticks and the patch sprites drew on black
                # with no picture behind them.  Review caught it as the
                # scene's first few frames being "just the mouths": two
                # patch cells, Jessica's lips and Cody's, alone on an empty
                # screen.  It is the scene-END flash (108b) mirrored.
                # Gate on EXACTLY the layer condition, not one tick early.
                # dark_ticks-1 was the tempting choice (the OBJ list is
                # fetched a frame behind the tilemap, so the page written on
                # the last dark tick would land on the plane's first visible
                # frame) and it still leaked: measured, the entry counter
                # does NOT advance once per frame here, so "one tick early"
                # is not one frame early.  Matching the layer test exactly
                # costs at most a frame of patches at the open, which no one
                # can see, and guarantees the sprites never precede the
                # picture.
                emit(0x0C46, dark_ticks)     # cmpi.w #dark_ticks,d6
                bxx(0x6500, "pwdn")          # blo -> plane not up yet, park
                # PATCH SPRITES: cells whose single-palette fit was poor are
                # redrawn as sprites carrying their OWN palette -- the same
                # escape hatch the Mega CD uses by compositing its figures
                # over the plane.  Table entries are (band, col, code,
                # objpal) words, FFFF-terminated.  A sprite's screen row
                # follows the live scroll: band i sits at screen
                # y = i*16 - (sy - SY0), so sprite y = that + 16 (OBJ
                # origin), and sprite x = col*16 + 32 + 64.
                # Derive the live scroll from the CURRENT script record --
                # 0xFF12B2 looked free but the game zeroes it every frame,
                # which parked every patch.  STATE+2 is the event index and
                # the record's sy sits at +14.
                emit(0x3039, be32(0xFFFFF2))         # move.w idx,d0
                emit(0x0C40, 0xFFFF)
                bxx(0x6700, "pwdn")                  # pre-first event
                # PARK THE PAGE ON THE LAST DRAWING FRAME.
                # Review: "the background is hidden one frame before the obj
                # tiles" -- captured in game as f7367, the whole scene black
                # with a dozen patch cells (grass, jeans, her shoe) scattered
                # over it.  The cause is the lag this engine already knows
                # about at the scene's START ("the OBJ list is fetched a
                # frame behind the tilemap", the three-dark-tick rule): the
                # sprites on screen at frame N are the page as it stood at
                # N-1.  `fin` restores the layer control and dies on the same
                # frame, so the tilemap goes instantly and the previous
                # frame's sprites are still displayed.
                # Parking AT fin cannot work -- it is already too late by one
                # frame (built, run, still flashed).  Gating the walker on
                # the last EVENT is worse: events run 3-116 frames here, so
                # it drops the patches long before the end.  Park on the last
                # drawing FRAME instead: the display that frame still shows
                # the page from N-1, and the fin frame then shows nothing.
                emit(0x0C40, nevents - 1)            # last event?
                bxx(0x6600, "pwgo")
                emit(0x0C79, 0x0001, be32(0xFFFFF4))  # ...on its last frame?
                bxx(0x6700, "pwdn")                  # -> park the whole page
                lab["pwgo"] = len(b)
                emit(0x3400)                         # move.w d0,d2 (keep idx)
                emit(0x48C2)                         # ext.l d2
                emit(0xE98A)                         # lsl.l #4,d2 (x16, LONG)
                lea(script_base, 1)
                emit(0xD3C2)                         # adda.l d2,a1 = record
                # PLACE THE SPRITES FOR THE FRAME THEY LAND ON.
                # Same one-frame OBJ lag as the scene end, now measured
                # during the PAN: at in-game frame N the plane shows the
                # disc's frame F while the sprites show F-1 (four samples
                # across the pan, every one of them exactly one frame).
                # The pan moves 1 px per frame and each pan event is one
                # frame long, so that lag IS a 1 px vertical offset -- and
                # because a patch carries its OWN palette, the misregistered
                # cell also reads as a palette change against its
                # neighbours.  Both halves of the review report ("jessica's
                # mouth shifts down by 1px and changes palettes"), one cause.
                # The cure is to take sy from the record that will be LIVE
                # when these sprites are displayed: the next one, whenever
                # this is the current event's last frame.
                emit(0x0C79, 0x0001, be32(0xFFFFF4))  # last frame of event?
                bxx(0x6600, "pwsy")                  # no -> this record
                emit(0x0C40, nevents - 1)            # already the last event?
                bxx(0x6700, "pwsy")                  # yes -> this record
                emit(0x43E9, 0x0010)                 # a1 = the NEXT record
                lab["pwsy"] = len(b)
                emit(0x3229, 0x000E)                 # d1 = sy
                emit(0x0441, 0x0100)                 # d1 -= SY0
                lea(patch_base, 2)                   # a2 = patch table
                lab["pw0"] = len(b)
                emit(0x341A)                         # move.w (a2)+,d2  band
                emit(0x0C42, 0xFFFF)
                bxx(0x6700, "pwdn")
                emit(0xE74A)                         # lsl.w #3,d2
                emit(0xD442)                         # add.w d2,d2   -> *16
                emit(0x9441)                         # sub.w d1,d2   -> screen y
                emit(0x361A)                         # move.w (a2)+,d3  col
                emit(0xE74B)                         # lsl.w #3,d3
                emit(0xD643)                         # add.w d3,d3   -> *16
                emit(0x0643, 0x0060)                 # +32 art origin, +64 OBJ
                # Earliest EVENT the patch is valid at.  Some patches describe
                # content that only exists once the pan has settled (the cell
                # holding Jessica's heel against the ground animates until the
                # camera stops, then holds for the rest of the scene), and
                # showing one early paints settled art over the animation.
                # The gate is the event index, NOT the scroll: the scroll
                # saturates at its final value a good second before the CD's
                # animation finishes, so a scroll gate let these patches in
                # early -- a 126-error block right at the settle.
                emit(0x381A)                         # move.w (a2)+,d4  min idx
                # and an END gate.  Both are read BEFORE either
                # compare, so the park path still only skips code+attr.  A
                # start gate alone cannot retire a patch, which is why the
                # figures could not be carried as objects and were flattened
                # into the plane -- where a refresh repaints background
                # through a character's palette (the miscoloured tile under
                # Jessica's heel).  0x7FFF = never; the compare is SIGNED,
                # so 0xFFFF would read as -1 and retire everything at once.
                emit(0x3A1A)                         # move.w (a2)+,d5  max idx
                emit(0xB044)                         # cmp.w d4,d0
                bxx(0x6D00, "pwpark")                # blt -> too early
                emit(0xB045)                         # cmp.w d5,d0
                bxx(0x6C00, "pwpark")                # bge -> expired
                # off-window (screen y outside 32..159) -> park this sprite
                emit(0x0C42, 0x0020)                 # cmpi.w #32,d2
                bxx(0x6D00, "pwpark")
                emit(0x0C42, 0x0090)                 # cmpi.w #144,d2
                bxx(0x6E00, "pwpark")
                emit(0x0642, 0x0010)                 # +16 OBJ y origin
                emit(0x38C3)                         # move.w d3,(a4)+   x
                emit(0x38C2)                         # move.w d2,(a4)+   y
                emit(0x28DA)                         # move.l (a2)+,(a4)+ code:attr
                bxx(0x6000, "pw0")
                # a record that is gated off or off-window is
                # SKIPPED, not parked.  Parking still consumed an OBJ slot,
                # so the table could never exceed the page's 128 -- which is
                # why it held 30 corrective patches and could not carry
                # animation.  Skipping makes the TABLE size unbounded; what
                # is bounded is how many records are live AT ONCE (measured
                # peak for the farewell: 121 on-screen of 561, against 124
                # slots free after the letterbox bars).  The slots the table
                # did not fill are parked once, below.
                lab["pwpark"] = len(b)
                emit(0x588A)                         # addq.l #4,a2 (skip code+attr)
                bxx(0x6000, "pw0")
                lab["pwdn"] = len(b)
                # park every slot the table did not use, up to the page end
                # (128 slots x 8 B = 0x400).  Without this, slots written on
                # a previous frame keep drawing a stale pose after its gate
                # closes.
                # Compare against an IMMEDIATE, not a register.  Parking the
                # page through a3 cost the captions: the caption emitter
                # further down reads its frame counter with `move.w (a3),d4`
                # and expects a3 set by earlier code, so borrowing it here
                # left the captions reading the OBJ page as a timer.
                # Four slots per movem.l (d0/d2/d4 = parked x, d1/d3/d5 = 0),
                # then singles for the tail; d7's flags are consumed above, so
                # it can carry the fourth pair.
                emit(0x203C, be32(0x01F00000))       # move.l #$01F00000,d0
                emit(0x7200)                         # moveq #0,d1
                emit(0x2400)                         # move.l d0,d2
                emit(0x2601)                         # move.l d1,d3
                emit(0x2800)                         # d4
                emit(0x2A01)                         # d5
                emit(0x2C00)                         # d6 (t no longer needed)
                emit(0x2E01)                         # d7
                lab["pwpk4"] = len(b)
                emit(0xB9FC, 0x0092, 0x03E0)         # cmpa.l #$9203E0,a4
                bxx(0x6200, "pwpk1")                 # bhi -> < 4 slots left
                emit(0x48D4, 0x00FF)                 # movem.l d0-d7,(a4)
                emit(0x49EC, 0x0020)                 # lea 32(a4),a4
                bxx(0x6000, "pwpk4")
                lab["pwpk1"] = len(b)
                emit(0xB9FC, 0x0092, 0x0400)         # cmpa.l #$920400,a4
                bxx(0x6400, "pwpkd")                 # hs -> page full
                emit(0x28C0)                         # move.l d0,(a4)+
                emit(0x28C1)                         # move.l d1,(a4)+
                bxx(0x6000, "pwpk1")
                lab["pwpkd"] = len(b)
        if ending_mode and not letterbox:
            # SIDE MARGIN BARS.  Every converted scene presents
            # the CD's 320 px in the x 32..351 window with black side
            # margins -- except the Guy/Cody pan, whose first event paints
            # the wrapping pano across ALL 64 map columns (the pan must
            # never reveal darkness), which also fills the margin-visible
            # columns: the margins collapsed 32px -> 0 at the scene's
            # first event (user: "the margins are reduced").  Scroll1
            # renders above the art, so 4-column bars of an opaque tile at
            # palette 1 -- asserted all-black here every vblank, same
            # reasoning as CAPPAL -- mask screen x 0..31 and 352..383 across
            # the art rows whatever scroll2 holds beneath.  Probe-measured
            # facts (lua pokes + clean-frame diffs): the engine's scroll1
            # places map col c at screen x = c*8 - 64 (the caption convention;
            # cols 0..7 are off-screen), so the left bar is cols 8..11 and the
            # right 52..55; art rows are map rows 4..19 (rows 20..27 are the
            # band wipe's); 0x40C0 renders TRANSPARENT in this bank, so the
            # bars use 0x4867, the most uniform of 170 diff-verified
            # fully-opaque tiles.
            # The palette is asserted every vblank (16 words, in the palette
            # section above); the TILES once, on the first vblank after INIT
            # (INIT cleared the page; nothing else writes those cells during
            # the scene -- the pixel gate over the whole reunion confirms it).
            emit(0x0C46, 0x0001)             # cmpi.w #1,d6: first vblank?
            bxx(0x6600, "mgdn")
            emit(0x203C, be32(0x48670001))   # d0 = opaque tile | pal 1
            for gi, cbase in enumerate((8, 52)):
                lea(0x908000 + cbase * 0x80 + 4 * 4, 4)
                emit(0x323C, 0x0003)         # d1 = 4 columns - 1
                lab[f"mgc{gi}"] = len(b)
                emit(0x740F)                 # moveq #15,d2 (16 rows)
                lab[f"mgl{gi}"] = len(b)
                emit(0x28C0)                 # move.l d0,(a4)+
                bxx(0x51CA, f"mgl{gi}")
                emit(0xD8FC, 0x0080 - 16 * 4)
                bxx(0x51C9, f"mgc{gi}")
            lab["mgdn"] = len(b)
        if gcspr:
            # ---- GUY/CODY SPRITE WALKER (JP e2) --------------------------
            # The scene is layered exactly as the Mega CD lays it out:
            # scroll2 carries the pillar background (sx from the script),
            # and the two characters are OBJ sprite groups whose anchor,
            # pose cel and visibility come from a PER-EVENT table.  Each
            # vblank: idx -> SPREVT row -> for each character, either walk
            # the cel's cells (anchor-relative) into the parked OBJ page or
            # park that character's slot range.
            # Outside the scene the page is parked ONCE, on the first event
            # past it (INIT parked it before the scene, and nothing else
            # writes it).
            MAXC = gcspr["maxc"]
            emit(0x3039, be32(0xFFFFF2))         # move.w idx,d0
            emit(0x0C40, 0xFFFF)
            bxx(0x6700, "gcdone")                # pre-first event
            emit(0x0440, gcspr["first_evt"])     # subi.w #first,d0
            bxx(0x6500, "gcdone")                # borrow: before the scene
            emit(0x0C40, gcspr["n_evt"])
            bxx(0x6700, "gcpka")                 # first event past it: park
            bxx(0x6200, "gcdone")                # bhi: later, already parked
            emit(0x3200)                         # move.w d0,d1
            emit(0xE748)                         # lsl.w #3,d0   (*8)
            emit(0xE549)                         # lsl.w #2,d1   (*4)
            emit(0xD041)                         # add.w d1,d0   (*12)
            lea(gcspr["sprevt"], 2)              # a2 = SPREVT
            emit(0xD4C0)                         # adda.w d0,a2
            # slots 40.. (see the letterbox note: entries below ~38 are read
            # by the OBJ DMA before this override can write them); 2 x MAXC
            # <= 88 fits the page
            assert 40 + 2 * MAXC <= 128, MAXC
            lea(0x920000 + 40 * 8, 4)            # a4 = OBJ slots
            emit(0x3E3C, 0x0001)                 # move.w #1,d7 (2 chars)
            lab["gcchar"] = len(b)
            emit(0x321A)                         # move.w (a2)+,d1  x
            emit(0x341A)                         # move.w (a2)+,d2  y
            emit(0x361A)                         # move.w (a2)+,d3  cel
            emit(0x0C43, 0xFFFF)
            bxx(0x6700, "gcpkc")                 # hidden this event
            emit(0xD643)                         # add.w d3,d3
            lea(gcspr["celoff"], 3)              # a3 = CELOFF
            emit(0x3633, 0x3000)                 # move.w (a3,d3.w),d3
            lea(gcspr["celtab"], 3)              # a3 = CELTAB
            emit(0xD6C3)                         # adda.w d3,a3
            emit(0x381B)                         # move.w (a3)+,d4  count
            emit(0x3A3C, MAXC - 1)               # d5 = park budget
            emit(0x5344)                         # subq.w #1,d4 (dbf)
            lab["gcent"] = len(b)
            emit(0x3C1B)                         # move.w (a3)+,d6  dx
            emit(0xDC41)                         # add.w d1,d6
            # Cody exits LEFT, so his cells reach negative x -- and the OBJ
            # x field is narrower than a word, so the hardware masked -2 to
            # a large positive value and drew his hair back in on the RIGHT
            # of the screen for the last moments of the scene (user).
            # Measured: from e2 frame 2688 the walker writes
            # x = 65534, 65532, ... 65520 into 7-16 slots at once.
            # OBJ x = screen + 96, so ANY negative x is wholly off the left
            # edge and parking it cannot clip him on the way out.
            emit(0x4A46)                         # tst.w d6
            bxx(0x6A00, "gcxok")                 # bpl: on-screen side
            emit(0x3C3C, 0x01F0)                 # move.w #$1F0,d6 (parked)
            lab["gcxok"] = len(b)
            emit(0x38C6)                         # move.w d6,(a4)+  x
            emit(0x3C1B)                         # move.w (a3)+,d6  dy
            emit(0xDC42)                         # add.w d2,d6
            emit(0x38C6)                         # move.w d6,(a4)+  y
            emit(0x28DB)                         # move.l (a3)+,(a4)+ code:attr
            emit(0x5345)                         # subq.w #1,d5
            bxx(0x51CC, "gcent")                 # dbf d4
            bxx(0x6000, "gcpkr")
            lab["gcpkc"] = len(b)
            emit(0x3A3C, MAXC - 1)               # park the whole range
            lab["gcpkr"] = len(b)
            emit(0x4A45)                         # tst.w d5
            bxx(0x6B00, "gcnext")                # none left to park
            emit(0x203C, be32(0x01F00000))       # parked x, y 0
            lab["gcprk"] = len(b)
            emit(0x28C0)                         # move.l d0,(a4)+
            emit(0x429C)                         # clr.l (a4)+
            bxx(0x51CD, "gcprk")                 # dbf d5
            lab["gcnext"] = len(b)
            bxx(0x51CF, "gcchar")                # dbf d7 (next character)
            bxx(0x6000, "gcdone")
            lab["gcpka"] = len(b)                # first frame past the scene
            lea(0x920000 + 40 * 8, 4)
            emit(0x203C, be32(0x01F00000))
            emit(0x3A3C, 2 * MAXC - 1)
            lab["gcpk2"] = len(b)
            emit(0x28C0)
            emit(0x429C)
            bxx(0x51CD, "gcpk2")                 # dbf d5
            lab["gcdone"] = len(b)
        # ---- SCROLL + MAP CELLS after the OBJ page work: the CPS-A copies
        # the OBJ table one entry per line from the frame's OBJ-base write,
        # so the OBJ page must be complete as early in the blanking as
        # possible; the tilemap rows are fetched per 16-line band, top down,
        # and can follow (vbsched orders each vblank's cells top-down for the
        # same reason).
        bxx(0x6100, "tickb")
        lea(0xFF12AE, 3)                     # a3 = counter (the captions read it)
        if ecaps_base is not None:
            # caption records: u16 t0, u16 t1, u16 ncells, ncells x
            # (u16 mapoff, u16 tile, u16 attr); terminator t0 = FFFF.
            # Reveal = min(t - t0, ncells) cells, 1 per frame -- the
            # typewriter feel without the typewriter.
            # A caption CLEARS ITS OWN CELLS on the vblank t == t1 (the
            # counter advances exactly once per override call, so that vblank
            # always comes), rather than wiping the whole 8-row band -- 384
            # longs, 11.5k cycles -- every vblank to the same effect.
            emit(0x3813)                     # move.w (a3),d4   t
            lea(0x908000, 1)                 # a1 = scroll1 page
            lea(ecaps_base, 4)               # a4 = records
            lab["ecap0"] = len(b)
            emit(0x321C)                     # move.w (a4)+,d1  t0
            emit(0x0C41, 0xFFFF)
            bxx(0x6700, "ecapdn")
            emit(0x341C)                     # move.w (a4)+,d2  t1
            emit(0x361C)                     # move.w (a4)+,d3  ncells
            emit(0xB842)                     # cmp.w d2,d4
            bxx(0x6700, "ecclr")             # t == t1: clear this caption
            emit(0x7000)                     # moveq #0,d0      reveal
            emit(0xB841)                     # cmp.w d1,d4
            bxx(0x6500, "ecdrw")             # t < t0: nothing
            emit(0xB842)                     # cmp.w d2,d4
            bxx(0x6400, "ecdrw")             # t >= t1: nothing
            emit(0x3004)                     # move.w d4,d0
            emit(0x9041)                     # sub.w d1,d0      t - t0
            emit(0xB043)                     # cmp.w d3,d0
            bxx(0x6300, "ecdrw")             # <= ncells: keep
            emit(0x3003)                     # move.w d3,d0     clamp
            lab["ecdrw"] = len(b)
            emit(0x9640)                     # sub.w d0,d3      d3 = rest
            bxx(0x6000, "ecrt")
            lab["ecrl"] = len(b)
            emit(0x321C)                     # move.w (a4)+,d1  mapoff
            emit(0x45F1, 0x1000)             # lea (0,a1,d1.w),a2
            emit(0x24DC)                     # move.l (a4)+,(a2)+  tile:attr
            lab["ecrt"] = len(b)
            bxx(0x51C8, "ecrl")              # dbf d0 (draws d0 cells)
            emit(0x3203)                     # move.w d3,d1
            emit(0xE541)                     # asl.w #2,d1      4*rest
            emit(0xD243)                     # add.w d3,d1      5*rest
            emit(0xD243)                     # add.w d3,d1      6*rest
            emit(0xD8C1)                     # adda.w d1,a4     skip rest
            bxx(0x6000, "ecap0")
            lab["ecclr"] = len(b)
            emit(0x5343)                     # subq.w #1,d3
            bxx(0x6B00, "ecap0")             # bmi: no cells
            lab["eccl"] = len(b)
            emit(0x321C)                     # move.w (a4)+,d1  mapoff
            emit(0x42B1, 0x1000)             # clr.l (0,a1,d1.w)
            emit(0x588C)                     # addq.l #4,a4  (skip tile:attr)
            bxx(0x51CB, "eccl")              # dbf d3
            bxx(0x6000, "ecap0")
            lab["ecapdn"] = len(b)
        bxx(0x6000, "ovrx")
    # ---- PRE-ROLL BLACKOUT STUB (ending_mode instances only) -------------
    # Armed at 0x18518 (outer-4 inner-0, the pre-roll scene build): the
    # stock reunion cel fades in for ~0.5 s BEFORE the ending entry runs.
    # Displacing the phase's own layer-shadow write (move.w #$12CE,$6e(a5))
    # lets us substitute scrolls-off and park the obj fetch page, so the
    # whole pre-roll runs invisibly -- its fade tasks still execute (the
    # phase timing is untouched), the hardware just never shows them.
    # INIT re-blanks at the entry and fin restores the obj base.
    if ending_mode:
        lab["blk"] = len(b)
        # Zero the frame counter HERE, at the phase's first frame.  INIT
        # clears it too, but INIT does not run for another two frames, and
        # the dark-tick guard reads this counter: left holding the previous
        # engine's value (1560) it compared as "settled" and let the layers
        # come on, showing one frame of the art UNMASKED before the
        # letterbox sprites existed -- the flicker at the scene's start.
        emit(0x4279, be32(0xFF12AE))          # clr.w counter
        emit(0x3B7C, 0x12C0, 0x006E)          # shadow: scrolls off (not 12CE)
        emit(0x33FC, 0x12C0, be32(0x80016E))  # reg now
        emit(0x48E7, 0x4040)                  # movem.l d1/a1,-(sp)
        lea(0x920000, 1)                      # park the spare obj page
        emit(0x323C, 0x03FF)
        lab["bprk"] = len(b)
        emit(0x32FC, 0x01F0)
        bxx(0x51C9, "bprk")
        emit(0x33FC, 0x9200, be32(0xFF809E))  # obj base SHADOW -> parked page
                                              # (pushed by $584 next vblank:
                                              # one $800100 write per frame)
        emit(0x4CDF, 0x0202)                  # movem.l (sp)+,d1/a1
        # skip the rest of the stock scene build: the phase is a coroutine
        # that yields for ~18 frames building + fading in the stock cel
        # (scroll-drawn -- it survives the obj park, and the fade system
        # rewrites the layer shadow over anything we set).  We replace
        # that scene wholesale, so return PAST it: 0x18656 (pushed by the
        # arm jsr) + blk_skip lands on the text-select -> arm -> INIT tail
        # (reunion: 0x18656+0x54 = 0x186AA; farewell: 0x18A70+0x52 = 0x18AC2
        # -- the distance DIFFERS per phase, and a wrong one lands
        # mid-instruction and silently kills the scene).
        # The whole entry then runs in its first frame: no pre-roll at
        # all, and every clock (captions, VO track, cue-54 countdown)
        # stays keyed to INIT exactly as before.
        emit(0x0697, be32(blk_skip))          # addi.l #skip,(sp)
        emit(0x4E75)
    # ---- VBLANK OVERRIDE ------------------------------------------------
    # THE opening's per-frame body.  Catch-up (last vblank's late record),
    # tick, caption pump, caption palettes -- all in vblank.
    lab["ovr"] = len(b)
    lea(STATE, 0)
    emit(0x0C50, magic)
    bxx(0x6700, "ovrcafe")
    emit(0x0C50, 0x51DE)                     # slide mode?
    bxx(0x6700, "vbslide")
    for chain_magic, chain_addr in (ovr_chain or ()):
        # further engine states: an ENDING instance owns this vblank
        emit(0x0C50, chain_magic)
        emit(0x6606)                         # bne.s past the jmp
        emit(0x4EF9, be32(chain_addr))
    # ---- CLOSE THE CREDITS' BLANK-BEAT LAG (polish, not a fix:
    # stock does this too, confirmed by review in the pure arcade ending).
    #
    # Between credits scenes the sequencer blanks by writing 0x12C2 to the
    # LAYER SHADOW ($6e(a5)).  Measured by sweeping the layer word over a
    # static stretch of credits and photographing each value:
    # bit 0x02 = scroll1 = the CREDIT ROLL
    # bit 0x04 = scroll2, bit 0x08 = scroll3 = the PICTURE
    # 12C2 -> picture 10.2 (the all-off floor is 10.4), roll 24.9
    # So 0x12C2 should already show no picture.  It does not, because the
    # shadow and the LIVE register are one frame apart: the shadow takes
    # 0x12C2 on the beat's first frame, the game's own refresh only pushes
    # it to 0x80016E the frame after, and the frame in between renders the
    # OLD value while the incoming scene's art is already resident.  That
    # one frame is the flash.
    #
    # Push the shadow to the live register in the SAME vblank.  This runs in
    # the ISR (we own the vblank return at 0x5E2), which is why it can work
    # where a Lua poke could not -- MAME's frame_done fires after the frame
    # has already been rendered, so four separate poke experiments fired
    # correctly and changed nothing.
    # Gated hard: only outer==0x0008 (the credits) and only the exact blank
    # value, so no other screen in the game can see this.
    emit(0x0C79, 0x0008, be32(0xFF1288))     # cmpi.w #8,outer
    bxx(0x6600, "ovrx")                      # not the credits -> out
    emit(0x0C79, 0x12C2, be32(0xFF806E))     # cmpi.w #$12C2,layer shadow
    bxx(0x6600, "ovrx")                      # not the blank beat -> out
    emit(0x33FC, 0x12C2, be32(0x80016E))     # live reg NOW, not next frame
    bxx(0x6000, "ovrx")
    lab["ovrcafe"] = len(b)
    # ---- STORY LIVENESS.  The dispatcher only beats STATE+8 (see the stub)
    # and this override runs the tick, so it must know when the story block
    # is live.
    # * outer ($FF1288) in the story range {2..8}: run.  Measured: the
    #   sequencer sets outer=2 the frame BEFORE the dispatcher's first call,
    #   so the first ISR tick lands on the vblank that starts the event-0
    #   frame.
    # * outer below 2 with idx still FFFF: the story has not started (the
    #   INIT-to-outer=2 gap): idle.  Below 2 or above 8 otherwise: the game
    #   left the story block (title 0xA, credited 0x12, test): die.
    # * heartbeat 3+ vblanks stale (coin-in mode switch freezes the
    #   dispatcher) or any credit ($FF804C, the BCD the game's own digit
    #   $18B8 reads -- set the frame the coin registers, BEFORE the title
    #   build): die.  Measured: a frozen-dispatcher coin parks outer at 2,
    #   so the range test alone is not enough, and the credit test alone
    #   misses test-menu switches.
    # Dying restores the layer control and sets DEAD, so nothing types
    # captions over whatever the game now shows.
    emit(0x3239, be32(0xFF1288))             # move.w outer,d1
    emit(0x0C41, 0x0002)                     # cmpi.w #2,d1
    bxx(0x6C00, "ovrst")                     # bge: 2 or more
    emit(0x0C68, 0xFFFF, 0x0002)             # idx == FFFF: not started yet
    bxx(0x6700, "ovrx")
    bxx(0x6000, "ovrdie")
    lab["ovrst"] = len(b)
    emit(0x0C41, 0x0008)                     # cmpi.w #8,d1
    bxx(0x6E00, "ovrdie")                    # bgt: past the story block
    emit(0x5268, 0x0008)                     # addq.w #1,8(a0)
    emit(0x0C68, 0x0003, 0x0008)             # cmpi.w #3,8(a0)
    bxx(0x6C00, "ovrdie")                    # bge: dispatcher quiet 3+ vblanks
    emit(0x4A79, be32(0xFF804C))             # tst.w credit BCD
    bxx(0x6600, "ovrdie")                    # bne: coined -- die now
    bxx(0x6000, "ovrlive")
    lab["ovrdie"] = len(b)
    lea(0x800140, 1)
    emit(0x337C, 0x12CE, 0x002E)             # layer control -> stock
    emit(0x30BC, 0xDEAD)                     # engine off
    bxx(0x6000, "ovrx")
    lab["ovrlive"] = len(b)
    # ---- ORDER: publish last vblank's palettes (the DMA start, so it runs
    # early in the blanking); tick_a (advance, cue, this record's block
    # copy); the catch-up; tick_b (scroll, this record's map cells) --
    # newer cells after older ones, as before; then the caption pump.
    emit_paldma("o")                         # publish last vblank's palettes
    emit(0x3C68, 0x0002)                     # movea.w 2(a0),a6: idx BEFORE the tick
    lea(0xFF8000, 5)                         # a5: the cue path needs it
    bxx(0x6100, "tick")
    # ---- CATCH-UP: the LATE record loaded by the previous vblank's tick
    # (bit 13: the conversion's >=64-cell events) lands now, one vblank
    # after its load -- the frame timing for big deltas that the scroll-exit
    # ring clear and the black bridge are matched to (convert.py).  Loop from
    # last-applied+1 to the pre-tick idx so a record can never be skipped;
    # the palette was copied by the tick at load and is not copied again here.
    # STATE+6 = last applied.
    emit(0x300E)                             # move.w a6,d0
    emit(0x3600)                             # move.w d0,d3   idx before the tick
    emit(0x0C43, 0xFFFF)
    bxx(0x6700, "cudone")                    # pre-first: nothing loaded
    emit(0x3028, 0x0006)                     # move.w 6(a0),d0   last applied
    lab["cu"] = len(b)
    emit(0xB043)                             # cmp.w d3,d0
    bxx(0x6C00, "cudone")                    # bge.w done
    emit(0x5240)                             # addq.w #1,d0
    emit(0x3200)                             # move.w d0,d1
    record_a1(1)                             # a1 = record d1
    emit(0x0829, 0x0005, 0x0008)             # btst #5,8(a1): word bit 13, late
    bxx(0x6700, "cu")                        # early: the tick applied it
    emit(0x48E7, 0x9000)                     # movem.l d0/d3,-(sp)
    lea(BUF, 4)
    bxx(0x6100, "delta")
    emit(0x4CDF, 0x0009)                     # movem.l (sp)+,d0/d3
    bxx(0x6000, "cu")                        # next record
    lab["cudone"] = len(b)
    emit(0x3143, 0x0006)                     # move.w d3,6(a0)
    lea(0xFF8000, 5)
    bxx(0x6100, "tickb")                     # scroll regs + this record's cells
    # the pump and palettes below run from the vblank AFTER event 0's load
    # (the override sees idx = 0 one vblank after the tick loads it): gate on
    # the pre-tick idx
    emit(0x300E)                             # move.w a6,d0
    emit(0x0C40, 0xFFFF)
    bxx(0x6700, "ovrx")
    # ---- caption pump: fire scheduled text-job ids via the stock $283A
    # enqueue (queue -> $4B5A dispatcher -> typewriter task / clears).
    # id 0x200 = group-2 clear (wipes the scroll1 page ONLY -- never use
    # 0x700 mid-story: its group-3 sub-clear wipes the scroll2 art page).
    lea(0xFF12AE, 3)                         # lea counter,a3
    emit(0x5253)                             # addq.w #1,(a3)
    lab["caplp"] = len(b)
    emit(0x302B, 0xFFE2)                     # move.w -$1e(a3),d0: ptr at
                                             # 0xFF1290 (12B0 gets re-disarmed
                                             # to $FFFF by the stock preamble)
    lea(CAPTAB, 4)
    emit(0xD8C0)                             # adda.w d0,a4
    emit(0x321C)                             # move.w (a4)+,d1   entry t
    emit(0x0C41, 0xFFFF)
    bxx(0x6700, "capdn")
    emit(0xB253)                             # cmp.w (a3),d1
    bxx(0x6200, "capdn")                     # bhi: not yet due
    emit(0x3014)                             # move.w (a4),d0    text id
    emit(0x2F0B)                             # move.l a3,-(sp)
    lea(0xFF8000, 5)                         # lea $ff8000,a5 ($283A needs it;
    emit(0x4EB8, 0x283A)                     #   ovrx movem restores all regs)
    emit(0x265F)                             # move.l (sp)+,a3
    emit(0x586B, 0xFFE2)                     # addq.w #4,ptr(0xFF1290)
    bxx(0x6000, "caplp")
    lab["capdn"] = len(b)
    # scroll1 caption palettes: the arcade faded the text palettes out
    # before the story and the fade-in lives in the stubbed entries, so
    # re-assert pals 6 and 14/15 at full fade every vblank (48 words).
    lea(CAPPAL, 4)                           # lea CAPPAL,a4
    lea(0x9144C0, 3)                         # pal 6
    for _ in range(8):
        emit(0x26DC)                         # move.l (a4)+,(a3)+
    lea(0x9145C0, 3)                         # pals 14+15 (contiguous)
    for _ in range(16):
        emit(0x26DC)
    bxx(0x6000, "ovrx")
    # ---- sprite slide, final form.  Wall + rubble = the OBJ
    # list (stamped once by the title build, never rewritten -- measured);
    # logo = SCROLL1 (+ scroll3 tail).  n==2 (post-stamp): displace every
    # active sprite +168 in one shot.  Each later frame: wall travel -=4
    # with an incremental pass over the static list (168 = 4*42, lands
    # exact); logo travel -=5 clamped, s1y written from the shadows so it
    # lands pixel-exact.  scroll1 stays 0x12CC (texts hidden) until
    # landing, then 0x12CE.
    # THE SLIDE RUNS INSIDE A DYING HOOK.  The $5E2 epilogue we detour
    # belongs to the title-anim vblank mode only: it is called from
    # title init (fin+2) until the stock anim ends ~75 frames later
    # (measured: f8894..f8971), then never again this cycle.  So (a)
    # the slide must LAND before the hook dies -- logo travel 208 =
    # 42 steps, ~5 frames of margin -- and (b) landing must NOT set
    # DEAD while the stock anim still runs (un-overriding mid-anim
    # shows the stock anim's own mid-flight positions = visible jump).
    # Instead, landed frames keep the hook: skip the clears, pin the
    # scroll1 shadows at 0, re-stamp the texts (idempotent), and let
    # the hook die naturally with the stock anim writing finals.
    # STATE never reaches DEAD this cycle; the attract-entry tick
    # re-inits it to CAFE next cycle.  The n>=80 check is insurance
    # if some later vblank mode ever calls the epilogue with 51DE
    # still set: immediate teardown.
    # ORDER: the sprite/scroll updates run FIRST -- they change what is
    # displayed and must sit in vblank, or the rising wall tears on hardware
    # (running them ~80 lines in, in active display, is what tore it); the
    # idempotent restores (text-row blanks, scroll2 rows 16-31) run after
    # them, and the scroll2 restore rotates 128 of its 1024 cells per vblank
    # (any stray rewrite still heals within 8 frames, without rewriting all
    # 1024 every vblank: 30k cycles).
    lab["vbslide"] = len(b)
    emit(0x3028, 0x0004)                     # d0 = n (F4)
    emit(0x5268, 0x0004)                     # n++
    emit(0x0C40, 0x0050)                     # cmpi.w #80,d0
    bxx(0x6C00, "sdead")                     # hook revived late: teardown
    emit(0x3228, 0x0002)                     # d1 = wall travel (F2)
    emit(0x8268, 0x0006)                     # or.w logo travel (F6),d1
    bxx(0x6700, "slanded")                   # both 0: hold landed state
    emit(0x0C40, 0x0003)                     # cmpi.w #3,d0
    bxx(0x6C00, "sstep")                     # n>=3: step
    # n==0,1,2: settle.  The title init stamps sprites/scroll on its
    # own clock inside this window, so every settle vblank (a) hides
    # scroll1 behind the black scroll2 backdrop (layer $12CC -- the
    # stock frame-1 logo position flashed for one frame otherwise),
    # (b) pins the s1y shadows at full travel, and (c) displaces any
    # freshly-stamped sprite.  Threshold $F8: finals are <=$F0,
    # displaced are >=$F8 (min final $50 + $A8) -- disjoint, so
    # nothing moves twice however often this runs.
    emit(0x3228, 0x0006)                     # d1 = logo travel (F6)
    emit(0x3B41, 0x0024)                     # pin shadows
    emit(0x3B41, 0x0028)
    # (the sprite displacement and the OBJ base now live in the objhook at
    # $584 -- BEFORE the handler's single $800100 write; see the hook)
    # NO palette writes anywhere in the slide: the stock title FADES IN
    # (CRAM high nibble = fade level, ramped by the title task) and any
    # CRAM write of ours resets cells out of the ramp -- measured as a
    # dim slide (level 1 vs 8 mid-slide).  The two 1-frame leaks are
    # closed without palettes: sprites by the parked OBJ base swap below,
    # the INSERT COIN blink by the $1258 stamper gate (skips while a
    # travel is nonzero under magic 51DE).
    # OBJ base: NO register write here (see objhook: a second $800100
    # write per frame restarts the CPS-A OBJ DMA on the core)
    lea(0x800140, 1)                         # bury scroll1 while settling
    emit(0x337C, 0x348C, 0x002E)
    bxx(0x6000, "srest")
    lab["sstep"] = len(b)
    # every sliding frame: real OBJ list back (sprites displaced below
    # the screen, rising into view under the stock fade-in)
    # (wall travel step, the -4 pass and the OBJ base -> real list all
    # run in the objhook at $584 this same vblank, before the pulse)
    lab["slogo"] = len(b)
    emit(0x3228, 0x0006)                     # d1 = logo travel (F6)
    bxx(0x6700, "schk")
    # 5px/frame (user: the bricks landed ~10 frames before the
    # title and the late arrival read badly): 208/5 = 42 steps, landing
    # the same frame as the wall's 168/4 = 42.  The bpl/clamp pair below
    # absorbs the 3px remainder on the last step.
    emit(0x5B41)                             # subq.w #5,d1
    emit(0x6A02)                             # bpl .st
    emit(0x7200)                             # moveq #0,d1
    emit(0x3141, 0x0006)                     # .st: store
    lab["schk"] = len(b)
    # the logo (and texts) are SCROLL1 -- proven by the content wipe.
    # Registers are re-copied from shadows by the game every frame, so
    # write the shadow pair: s1y = $28(a5), staged $24 -> $28.  Value =
    # +travel holds the layer above the screen, descending to the stock
    # rest (0) as the travel shrinks.
    emit(0x3B41, 0x0024)                     # move.w d1,$24(a5)
    emit(0x3B41, 0x0028)                     # move.w d1,$28(a5)
    emit(0x3028, 0x0002)                     # landed?  wall | logo == 0
    emit(0x8041)                             # or.w d1,d0
    bxx(0x6600, "slay")
    # LANDING FRAME (falls through exactly once; later landed frames branch
    # to slanded from the entry check).
    # complete the title fade.  The stock title FADES IN by ramping the
    # CRAM high nibble 0->F: a blocking loop ($2530: step cmd via $2688,
    # wait a frame, repeat) run by the title thread whose epilogue we
    # detour -- the hijack abandons it mid-ramp at level 9 (measured:
    # every palette word 9xxx in the attract title vs Fxxx credited;
    # brightness 0x21/0x2D = the dim logo whose drowned taper reads as
    # the "black mark" on the F).  Nothing re-asserts the level after
    # the ramp dies (a CRAM poke sticks -- measured), so one stamp of
    # F000 through the game's own applier ($27B2: d2 = level, d1 = page
    # mask b3/b0/b1/b2 = obj/s1/s2/s3) finishes the fade exactly where
    # stock would.  Landing frame only: mid-slide the ramp is still alive and
    # our write would knock cells out of it (measured, the dim-slide note
    # below); re-running the applier on EVERY landed frame would be 48k
    # cycles (75 lines) of identical CRAM rewrites a frame.  This one call is
    # the only piece of the opening that overruns the vblank (palette RAM
    # only, one frame, the title landing).  d1/d2/d3/d4/d5/d6/d7/a1 clobbered
    # -- all reloaded below, and ovrx restores everything anyway.
    emit(0x343C, 0xF000)                     # move.w #$F000,d2
    emit(0x323C, 0x000F)                     # move.w #$F,d1 (all pages)
    emit(0x4EB8, 0x27B2)                     # jsr $27b2.w
    lab["slanded"] = len(b)
    emit(0x7200)                             # moveq #0,d1
    emit(0x3B41, 0x0024)                     # pin s1y shadows at rest
    emit(0x3B41, 0x0028)
    emit(0x3B7C, 0x9000, 0x009E)             # obj base shadow stays stock
                                             # (the register is the hook's)
    # Restore the stock SCR2 title X so the Fight tail remains visible.
    # SCR3 is blank in the stock title; EX uses it as a foreground overlay.
    # The ISR derives hardware scrolls from these staged/live shadows.
    emit(0x323C, 0x0200)                     # move.w #$200,d1
    emit(0x3B41, 0x002A)                     # s2x staged  $2a(a5)
    emit(0x3B41, 0x002E)                     # s2x live    $2e(a5)
    emit(0x323C, 0x0710)                     # move.w #$710,d1 (EX map origin)
    emit(0x3B41, 0x0034)                     # s3y staged  $34(a5)
    emit(0x3B41, 0x0038)                     # s3y live    $38(a5)
    # Preserve stock SCR2/OBJ/SCR1 ordering, with EX on top in SCR3.
    # Keep the stock priority masks; the ISR consumes these shadows.
    emit(0x3B7C, 0x348E, 0x006E)             # layer control staged $6e(a5)
    emit(0x3B7C, 0x348E, 0x0070)             # layer control live   $70(a5)
    emit(0x3B7C, 0x4009, 0x0074)             # -> reg 0x800170
    emit(0x3B7C, 0x7FFF, 0x0076)             # -> reg 0x800168
    # coin-prompt line: call the game's own chooser at $18B8 -- it
    # stamps INSERT COIN (credits 0) or PUSH 1P START (credits > 0)
    # plus the credit digit, exactly as stock (a coin can land during
    # the slide, so the choice must be live).  Travels are 0 here, so
    # the $1258 gate passes it through.  Clobbers d0/d1/a0 -- all
    # dead in this path.
    emit(0x4EB9, be32(0x000018B8))
    # F-tail taper tiles (see the szloop note): the slide wiped them
    # and nothing at the title restores them -- stamp every landed
    # frame, idempotent like the rest of this path.
    emit(0x23FC, be32(0x4B24001F), be32(0x00908B58))
    emit(0x23FC, be32(0x4B25001F), be32(0x00908BD8))
    emit(0x23FC, be32(0x4B26001F), be32(0x00908C58))
    emit(0x23FC, be32(0x4B27001F), be32(0x00908CD8))
    # copyright strip, measured per region: US = CO.,LTD. at row 26 +
    # U.S.A.,INC. at row 27 (tiles $4960+i/$4970+i); J = the single
    # CO.,LTD. strip at row 25 (its stock position -- rows 26/27 stay
    # clear).  Cols 24-39 both.  ATTRACT only: the stock credited
    # title shows no copyright, and a coin can land mid-slide -- the
    # strip left a lone (c) tile there (JP coin test).
    emit(0x4A79, be32(0xFF804C))             # credits? (BCD word)
    bxx(0x6600, "scskip")
    if jp_title:
        lea(0x908C64, 2)                     # row 25 col 24
        emit(0x363C, 0x000F)                 # d3 = 15
        emit(0x303C, 0x4960)                 # d0 = first tile
        lab["scloop"] = len(b)
        emit(0x3480)                         # move.w d0,(a2)
        emit(0x5240)                         # addq.w #1,d0
        emit(0x45EA, 0x0080)                 # next col
        bxx(0x51CB, "scloop")
    else:
        lea(0x908C68, 2)                     # row 26 col 24
        emit(0x363C, 0x000F)                 # d3 = 15
        emit(0x303C, 0x4960)                 # d0 = first top-half tile
        lab["scloop"] = len(b)
        emit(0x3480)                         # move.w d0,(a2)   row 26
        emit(0x3200)                         # move.w d0,d1
        emit(0x0641, 0x0010)                 # addi.w #$10,d1
        emit(0x3541, 0x0004)                 # move.w d1,4(a2)  row 27
        emit(0x5240)                         # addq.w #1,d0
        emit(0x45EA, 0x0080)                 # next col
        bxx(0x51CB, "scloop")
    lab["scskip"] = len(b)
    lea(0x800140, 1)
    emit(0x337C, 0x348E, 0x002E)
    bxx(0x6000, "ovrx")
    lab["sdead"] = len(b)
    lea(0x800140, 1)
    emit(0x337C, 0x348E, 0x002E)
    emit(0x30BC, 0xDEAD)
    bxx(0x6000, "ovrx")
    lab["slay"] = len(b)
    lea(0x800140, 1)                         # keep stock layer order
    emit(0x337C, 0x348E, 0x002E)
    # ---- RESTORES (every pre-landed vblank, after the updates above).
    lab["srest"] = len(b)
    # blank the scroll1 TEXT rows every sliding vblank -- the game
    # re-stamps INSERT COIN on its own schedule (blinker $12A2/$12FA),
    # so a one-shot clear loses the race; we run after the game's
    # writes each frame.  Measured map (cell = row*4 + col*$80): logo
    # rows 2-21+23, INSERT COIN row 22, copyright strip rows 26+27.
    # blank the text rows with the CREDITED-title cell values (measured
    # by diffing the attract vs coin-inserted title in the same build --
    # the user's coin tip): row 22 = $4420/attr $001F, rows 25-27 =
    # $4420/attr 0.  Wrong values here (attr 0 on row 22, clr.l = opaque
    # tile 0 on 25-27) mask the "Fight" F/g descender band.  With these
    # exact values the tail composites like the credited title.  (values
    # preloaded: 16-cycle stores)
    # (half the columns per vblank -- odd/even by n -- so the pass costs 4
    # lines, not 8; the blink gate suppresses the game's stamps while
    # sliding, so a cell is never more than two vblanks stale)
    emit(0x203C, be32(0x4420001F))           # d0 = row-22 cell
    emit(0x223C, be32(0x44200000))           # d1 = rows 25-27 cell
    lea(0x908000, 2)                         # a2 = scroll1 base
    emit(0x3428, 0x0004)                     # d2 = n
    emit(0x0242, 0x0001)                     # & 1
    emit(0xEF4A)                             # lsl.w #7,d2: odd -> col 1
    emit(0xD4C2)                             # adda.w d2,a2
    emit(0x363C, 0x001F)                     # d3 = 32 cols
    lab["szloop"] = len(b)
    emit(0x2540, 0x0058)                     # move.l d0,$58(a2)  row 22
    emit(0x2541, 0x0064)                     # move.l d1,$64(a2)  row 25
    emit(0x2541, 0x0068)                     # row 26
    emit(0x2541, 0x006C)                     # row 27
    emit(0x45EA, 0x0100)                     # lea $100(a2),a2: col + 2
    bxx(0x51CB, "szloop")
    # row 22 cols 22-25 are LOGO art, not text: the "Fight" F-tail's
    # bottom taper tiles ($4B24-$4B27/attr $1F, from the full credited-title
    # row diff).  Blanking them like text (the blanket space wipe above) cuts
    # the taper flat at the row-21 boundary -- the residual "black mark".
    # Restamp them every pass; they are part of the logo and ride the slide
    # with it.
    emit(0x23FC, be32(0x4B24001F), be32(0x00908B58))
    emit(0x23FC, be32(0x4B25001F), be32(0x00908BD8))
    emit(0x23FC, be32(0x4B26001F), be32(0x00908C58))
    emit(0x23FC, be32(0x4B27001F), be32(0x00908CD8))
    # scroll2 leftovers: the cutscene engine's black canvas (codes
    # $4000/$4001) stays in scroll2 map rows 16-31 after the opening
    # ends, sitting BETWEEN scroll1 and the scroll3 logo -- it masked
    # the F descender's tail (flat cut + floating dash, user report).
    # Stock keeps those rows filled with its blank $3000/attr 0
    # (MEASURED, all 1024 cells) and never rewrites them at the title,
    # so restoring that exact state every pre-landed vblank is
    # idempotent and title-safe.  128 cells per vblank, slice (n & 7) --
    # the whole block every 8 vblanks, from the first settle frames on.
    emit(0x3028, 0x0004)                     # d0 = n (already ++)
    emit(0x0240, 0x0007)                     # andi.w #7,d0
    emit(0xE148)                             # lsl.w #8,d0
    emit(0xE348)                             # lsl.w #1,d0: slice*512 bytes
    lea(0x90D000, 2)                         # a2 = scroll2 map row 16
    emit(0xD4C0)                             # adda.w d0,a2
    emit(0x203C, be32(0x30000000))
    emit(0x2200)
    emit(0x2400)
    emit(0x2600)
    emit(0x2800)
    emit(0x2A00)
    emit(0x2C00)
    emit(0x2E00)                             # d0-d7 = blank cells
    emit(0x47EA, 0x0200)                     # lea 512(a2),a3: 128 cells
    lab["s2lp"] = len(b)
    emit(0x48D2, 0x00FF)                     # movem.l d0-d7,(a2)
    emit(0x45EA, 0x0020)                     # lea 32(a2),a2
    emit(0xB5CB)                             # cmpa.l a3,a2
    bxx(0x6500, "s2lp")                      # blo
    lab["ovrx"] = len(b)
    if os.environ.get("FFCD_PROBE_MARK") == "1":
        # raster-probe builds only: an end-of-override marker the RTL/MAME
        # register-write probes can timestamp (same value the stock ISR
        # writes at $58C every frame; harmless)
        emit(0x33FC, 0x003F, be32(0x80016A))
    # ---- back into the stock handler.  The override hooks the ISR at $5A0
    # -- the `jsr $984.l` right after the register/shadow copies ($554..$59C)
    # -- instead of the movem/rte at $5E2: on jtcps1 the sound/input calls
    # between the two ($984/$F72/$E42/$50E) take ~13 lines against MAME's ~4,
    # so hooking after them leaves the override only ~19 lines of blanking on
    # the core.  The displaced call is made here as a tail jump; a5 is the one
    # register the rest of the handler relies on.
    lea(0xFF8000, 5)
    emit(0x4EF9, be32(0x00000984))           # jmp $984.l
    # ---- INIT ------------------------------------------------------------
    lab["init"] = len(b)
    emit(0x48E7, 0xE0C0)                     # movem.l d0-d2/a0-a1,-(sp)
    lea(STATE, 0)
    emit(0x30BC, magic)                      # instance magic (CAFE/CAFD)
    emit(0x317C, 0xFFFF, 0x0002)
    emit(0x317C, lead_in, 0x0004)            # ticks until event 0 loads
    emit(0x317C, 0xFFFF, 0x0006)             # last-applied = none
    emit(0x4268, 0x0008)                     # heartbeat = 0
    if ending_mode and magic == 0xCAFD:
        # ---- CPS-B PRIORITY MASKS.  The stock ending never loads its own:
        # it inherits $72/$74/$76/$78(a5) from the last gameplay area, and
        # the vblank ISR ($554) copies them to $800166/170/168/172 every
        # frame.  After a genuine round-6 clear those are 0000/157E/4FFF/
        # 7FFF, and prio3=7FFF is what puts the credits' statue columns
        # (SCR2 group 3, attr 0x0983) in FRONT of Guy and Cody.  Reached any
        # other way -- the one-stage debug route inherits stage 1's
        # 0000/4009/7FFF/0000 -- prio3 is 0 and the sprites walk through the
        # columns.  Measured (HBMAME, debug route, corridor snapshot 0062).
        # So the reunion engine establishes the round-6 masks itself: a
        # no-op on a real clear, faithful on every other route.  Absolute
        # addresses: a5 is not guaranteed in INIT context.
        for off, val in ((0x0072, 0x0000), (0x0074, 0x157E),
                         (0x0076, 0x4FFF), (0x0078, 0x7FFF)):
            emit(0x33FC, val, be32(0xFF8000 + off))
    if init_cue:
        # AFTER every STATE store: $9d0 clobbers a0 (queue lea) -- firing
        # it mid-sequence sent the last-applied init into the sound ring
        # and the map base never painted (corruption)
        #
        # CREDIT GATE .  This INIT is hooked onto the stock
        # story-block arm (rom.py, 0x170F6), which the game runs when it
        # ENTERS the attract -- including the entry it is about to abort
        # because credits are waiting.  Stock emits no sound cue there at
        # all: it goes straight 0xf0 -> 0xf2 -> 0xf7 and parks on the
        # title (measured on ffight from r6clear with coins inserted).
        # Firing the voiced key regardless meant the pack was told to
        # start a 137 s track that 0xf7 cancelled 0.4 s later -- an
        # audible ~0.26 s burst, since the CD master's head is not silent
        # (first sample at 0.14 s).  So test the game's own credit counter
        # first and emit nothing when a game is about to be startable.
        # The OPENING is credit-gated.  The ENDING instance (ending_mode)
        # is NOT: it runs after a real clear, when the credit counter may
        # legitimately be non-zero, and its voiced key (0x71 US / 0x73 JP)
        # must fire regardless.  It also sends through $9e4 -- the entry
        # the game's own ending code uses for cue 0x54 at 0x186D0/0x18636
        # -- which skips $9d0's extra $56ac(a5) refusal.
        if not ending_mode:
            emit(0x4A79, 0x00FF, 0x804C)     # tst.w $FF804C (credits, BCD)
            emit(0x6608)                     # bne.s +8 -> skip both below
        emit(0x303C, init_cue)               # move.w #cue,d0
        emit(0x4EB8, 0x09E4 if ending_mode else 0x09D0)  # jsr sound cue
    lea(BUF, 1)
    # map clear value.  Default 0x4000/attr0 is a REAL art tile, not
    # transparent -- harmless when the scene's art covers the screen (CD-1
    # bakes its own letterbox), but a scrolling scene leaves it showing
    # beyond the art columns.  Scrolling instances clear with their own
    # transparent tile instead.
    emit(0x223C, be32(clear_cell))
    emit(0x343C, 0x0FFF)
    lab["clr"] = len(b)
    emit(0x22C1)
    emit(0x51CA, 0x0000)
    if ending_mode:
        # clear the WHOLE scroll1 page: the stock scene build does this and
        # this engine skips it, and without the clear the attract story's map
        # overlay (stale 8x8 cells in the 128-col page) scrolls into view
        # during the credits.  The credits names redraw fresh; our subtitles
        # redraw every vblank; nothing needs the old page.
        lea(0x908000, 1)
        emit(0x343C, 0x0FFF)
        lab["es1c"] = len(b)
        emit(0x4299)                         # clr.l (a1)+
        bxx(0x51CA, "es1c")                  # dbf d2
    if ending_mode:
        # blank the arm frames: drawing event 0 from here tears (palette
        # and the CPS1 obj list latch on their own schedules, so the tiles
        # would render under the stale palette with stock sprite fragments).
        # Instead render black until the first tick's palettes have
        # propagated:
        # * layer control REG and its SHADOW $6e(a5) -> scrolls off
        # (the stock vblank handler re-copies the shadow every frame,
        # so the reg write alone lasts one frame)
        # * obj base + shadow -> a freshly parked spare page (the live
        # list at 0x900000 still holds the stock reunion cel and its
        # eof copy may already be latched); fin restores 0x9040
        # The tick holds the scrolls dark while idx==FFFF (see the gate
        # at tick start) and asserts 0x12C6 from tick 2 on.
        emit(0x33FC, 0x12C0, be32(0x80016E))  # layer control: scrolls off
        emit(0x33FC, 0x12C0, be32(0xFF806E))  # and its shadow
        lea(0x920000, 1)                      # park the spare obj page
        emit(0x323C, 0x03FF)
        lab["eprk"] = len(b)
        emit(0x32FC, 0x01F0)                  # move.w #$1F0,(a1)+
        bxx(0x51C9, "eprk")                   # dbf d1
        emit(0x33FC, 0x9200, be32(0xFF809E))  # obj base SHADOW -> parked page
                                              # (blk already switched it; the
                                              # $584 pulse is the only writer)
    emit(0x4CDF, 0x0307)                     # restore d0-d2/a0-a1
    # re-execute the instruction the arm displaced (raw words -- the opcode
    # differs by site: opening arm 0x170F6 = 3B7C xxxx 92B0 move.w; the
    # ending arm 0x186BA = 1B7C 0001 008D move.b #1,$8d(a5))
    emit(*arm_restore)
    if ending_mode:
        # counter := SENTINEL (see the FRAME COUNTER PHASE note in eovr),
        # +12B0 cleared as before
        emit(0x23FC, be32(0x80000000), be32(0xFF12AE))
    else:
        emit(0x42B9, be32(0xFF12AE))         # clr.l: caption counter (+12B0)
    emit(0x4279, be32(0xFF1290))             # clr.w: caption table ptr
    emit(0x4E75)

    # ---- BLINK GATE: detour target for the string stamper at $1258.
    # Skip the stamp only while the title slide is ACTIVE (magic 51DE
    # and a travel nonzero); everything else falls through to the
    # displaced entry instructions.  d0 = caller's string index --
    # untouched on the pass path (d1 is stamper-clobbered anyway).
    # Liveness: if the slide counter F4 hasn't advanced since the last
    # gated call (F8 = last-seen, fin sets $FFFF sentinel), the vblank
    # hook died mid-slide (coin/test switch) -- unwind: real OBJ base
    # back (a parked base shadow would leave gameplay spriteless),
    # DEAD, and let the stamp through.  The game's own shadow copy
    # restores the layer order within a frame.
    lab["blink"] = len(b)
    emit(0x0C79, 0x51DE, 0x00FF, 0xFFF0)     # cmpi.w #$51DE,STATE
    emit(0x6636)                             # bne.b .pass
    emit(0x4A79, 0x00FF, 0xFFF2)             # tst.w wall travel
    emit(0x6608)                             # bne.b .chk
    emit(0x4A79, 0x00FF, 0xFFF6)             # tst.w logo travel
    emit(0x6726)                             # beq.b .pass (landed)
    emit(0x3239, 0x00FF, 0xFFF4)             # .chk: d1 = n (F4)
    emit(0xB279, 0x00FF, 0xFFF8)             # cmp.w F8,d1
    emit(0x6708)                             # beq.b .stale
    emit(0x33C1, 0x00FF, 0xFFF8)             # F8 = n
    emit(0x4E75)                             # rts (suppress stamp)
    emit(0x33FC, 0x9000, 0x00FF, 0x809E)     # .stale: obj base shadow (the
                                             # $584 hook pushes it next frame)
    emit(0x33FC, 0xDEAD, 0x00FF, 0xFFF0)     # magic = DEAD
    emit(0xD000)                             # .pass: add.b d0,d0 (displaced)
    emit(0x6506)                             # bcs.b .off
    emit(0x4EF9, 0x0000, 0x125E)             # jmp stamper ON path
    emit(0x4EF9, 0x0000, 0x12AE)             # .off: jmp erase path

    # ---- OBJ-BASE HOOK: detour target for the stock vblank handler's
    # `move.w $9e(a5),$800100.l` at $584 (rom.py: jsr + nop, slide builds
    # only).  Runs INSIDE the handler, ~0.5 line into vblank, with every
    # register saved by the $53E movem and a5 = $FF8000.
    #
    # WHY HERE.  On jtcps1 every full-word write to $800100 pulses
    # obj_dma_ok (jtcps1_mmr.v:384-387) and the DMA swaps its two OBJ
    # table banks at the next line start and restarts the copy at entry
    # 0, ONE ENTRY PER LINE (jtcps1_dma.v:329-367, 476-486).  So the
    # slide's second write per frame (from the epilogue, ~65 lines after
    # the handler's) put the bank that held only entries 0-64 on screen
    # for the rest of the frame -- the wall's lower rows never showed and
    # the whole wall popped in when the hook died.  MAME latches the OBJ
    # RAM once per vblank (cps1_objram_latch) and never sees write count,
    # which is why the slide animated there.  The rule is therefore: the
    # slide never writes $800100 itself; it drives the shadow ($9e(a5)),
    # and it does its list edits HERE, before the handler's one pulse, so
    # the line-by-line copy that starts at that pulse reads a finished
    # list (no tear on the core, no double pulse).  Same phase on MAME:
    # its vblank-start latch sees last frame's list either way.
    #
    # Settle threshold: $F0, not $F8.  The stock title stamps its top brick
    # row at y=$48; $48+$A8 = $F0 < $F8 read as "not yet displaced" and
    # the row was displaced twice (landed at $F0 = off the bottom -- the
    # backport title was missing its top brick row; stock vs shipped
    # snapshots differ on rows 56-71).  Finals are $48..$A8, displaced
    # are >= $F0: disjoint at $F0.
    lab["objhook"] = len(b)
    bxx(0x6100, "extick")                    # topmost EX overlay, title-only
    emit(0x0C79, 0x51DE, 0x00FF, 0xFFF0)     # cmpi.w #$51DE,STATE.l
    bxx(0x6600, "ohstock")                   # not the slide -> stock write
    emit(0x3039, 0x00FF, 0xFFF4)             # move.w F4,d0 (n, pre-increment:
                                             # the epilogue's n++ runs later
                                             # this same vblank, so the phase
                                             # matches its settle/step split)
    emit(0x0C40, 0x0003)                     # cmpi.w #3,d0
    bxx(0x6C00, "ohstep")                    # n>=3: step
    # n=0,1,2: settle -- displace every freshly stamped sprite +168 (the
    # title build stamps on its own clock inside this window)
    emit(0x45F9, be32(0x900000))             # a2 = OBJ list
    emit(0x363C, 0x007F)                     # d3 = 127
    lab["ohsi"] = len(b)
    emit(0x0C52, 0x01F0)                     # parked?
    bxx(0x6700, "ohsn")
    emit(0x4A6A, 0x0004)                     # zeroed entry?
    bxx(0x6700, "ohsn")
    emit(0x0C6A, 0x00F0, 0x0002)             # cmpi.w #$F0,y: already displaced?
    bxx(0x6400, "ohsn")                      # bcc: y >= $F0
    emit(0x066A, 0x00A8, 0x0002)             # addi.w #168,y
    lab["ohsn"] = len(b)
    emit(0x508A)                             # addq.l #8,a2
    bxx(0x51CB, "ohsi")
    # keep the parked page live for this pulse: the title build's own
    # `move.w #$9000,$9e(a5)` ($1816A, right before its stamp loop) lands
    # in the main loop between our frames; without this the handler would
    # push the real list -- at its stock finals -- for one frame
    emit(0x3B7C, 0x9200, 0x009E)             # shadow = parked page
    bxx(0x6000, "ohstock")
    lab["ohstep"] = len(b)
    emit(0x3039, 0x00FF, 0xFFF2)             # move.w F2,d0 (wall travel)
    bxx(0x6700, "ohbase")                    # 0: landed, list is final
    emit(0x5940)                             # subq.w #4,d0 (4px/frame,
                                             # 168 = 42 exact steps)
    emit(0x33C0, 0x00FF, 0xFFF2)             # move.w d0,F2
    emit(0x45F9, be32(0x900000))             # -4 pass over the list
    emit(0x363C, 0x007F)
    lab["ohsw"] = len(b)
    emit(0x0C52, 0x01F0)
    bxx(0x6700, "ohwn")
    emit(0x4A6A, 0x0004)
    bxx(0x6700, "ohwn")
    emit(0x046A, 0x0004, 0x0002)             # subi.w #4,y
    lab["ohwn"] = len(b)
    emit(0x508A)
    bxx(0x51CB, "ohsw")
    lab["ohbase"] = len(b)
    emit(0x3B7C, 0x9000, 0x009E)             # shadow = real list
    lab["ohstock"] = len(b)
    emit(0x33ED, 0x009E, 0x0080, 0x0100)     # move.w $9e(a5),$800100.l --
                                             # the displaced stock instruction:
                                             # THE one OBJ pulse per frame
    emit(0x4E75)                             # rts

    # ---- PALETTE-DMA HOOK: detour target for the handler's per-frame
    # `move.w #$9140,$80010A.l` at $594 (rom.py: jsr + nop; the opening
    # engine hosts it).  On the CPS-A a write to the palette base register
    # STARTS the palette copy (VRAM -> colour RAM, ~12 lines for the six
    # pages) -- and on jtcps1 the copy owns the SDRAM bank the 68000's
    # work RAM and VRAM live in, so the CPU stalls for those ~12 lines
    # (a25 RTL probe: the handler's next 11 instructions took 12.6 lines).
    # While one of our engines is rendering (magic CAFE/CAFD/CAFC and an
    # event loaded) nothing but the engine writes palette RAM, and the
    # engine triggers the copy itself right after it changes a palette
    # (block copies, OBJ-palette uploads, its first frame): so the stock
    # per-frame trigger is skipped then, and every other frame of the
    # game runs stock.  MAME: the same-value register write has no
    # effect either way.  No registers touched.
    lab["palhook"] = len(b)
    emit(0x0C79, 0xCAFE, be32(STATE))        # cmpi.w #$CAFE,STATE
    bxx(0x6700, "phchk")
    emit(0x0C79, 0xCAFD, be32(STATE))
    bxx(0x6700, "phchk")
    emit(0x0C79, 0xCAFC, be32(STATE))
    bxx(0x6700, "phchk")
    lab["phdo"] = len(b)
    emit(0x33FC, 0x9140, be32(0x80010A))     # the displaced stock write
    emit(0x4E75)
    lab["phchk"] = len(b)
    emit(0x0C79, 0xFFFF, be32(STATE + 2))    # no event loaded yet: stock
    bxx(0x6700, "phdo")
    emit(0x4E75)

    # ---- CONTINUE-ENTRY HOOK: detour target for the continue/game-over
    # screen setup's `move.w #$9080,$800104.l` at 0x5E010 (rom.py: jsr +
    # nop, every build).  Called from inside the setup routine (a5 =
    # $FF8000, d0 dead: 0x5E02C reloads it before any use).
    #
    # WHY.  0x5E004 switches the SCR2/SCR3 bases to the shared map by
    # writing the CPS-A registers DIRECTLY, but its scroll and layer
    # control go through the a5 shadows, which the vblank handler copies
    # to the hardware one frame later ($574: lc <- $70, $70 <- $6E; $5E8:
    # scroll <- stage2, stage2 <- stage1) -- and 0x5E004 itself runs from
    # the handler's game-logic calls, AFTER those copies.  So exactly one
    # render sees the new bases with the PLAY scroll and the play layer
    # control (measured frame by frame, a30 probes: bases new at frame N,
    # scroll new at N+1, lc new at N+2, no other partial state).  In that
    # render the SCR2 window sits at the play scroll over the shared map,
    # and when that scroll aliases map cols 32-47 it reads the SCR3 frame
    # codes 0x0980-0x0A3F: unmapped (blank) under the ffight PAL / jtcps1
    # 0x1E, but under 0x20 SCR2 draws OBJ tiles 0x980.. as 16x16 fragments
    # (US f53730 / f103080 of the full-game sweep, JP twins).  The same
    # render is also where the backport flashes its own appended art: a
    # play scroll that lands the SCR2 window on the SCR1 rows (filler
    # $4420, the text) draws SCR2 tile $4420.. -- our cutscene art, at a
    # place a stock board reads unpopulated ROM (blank).
    #
    # FIX: turn SCR2/SCR3 off in the hardware register for that one render,
    # BEFORE the base flips.  Only the register is written -- $70/$6E stay
    # stock -- so the handler's next copy restores exactly the stock value
    # (12CE, 138E, 06CE, whatever the entry state was) and every later
    # frame is byte-for-byte stock.  The value comes from $70(a5) (what the
    # next vblank will write; equal to the register in steady state) with
    # bits 2,3 (S2/S3 enable) clear.
    lab["conthook"] = len(b)
    emit(0x302D, 0x0070)                     # move.w $70(a5),d0
    emit(0x0240, 0xFFF3)                     # andi.w #$FFF3,d0   (S2,S3 off)
    emit(0x33C0, 0x0080, 0x016E)             # move.w d0,$80016E.l -- this render only
    emit(0x33FC, 0x9080, 0x0080, 0x0104)     # move.w #$9080,$800104.l -- the displaced insn
    emit(0x4E75)                             # rts

    # BOOTHOOK.  The reset path enables all three scroll layers seven
    # instructions in (move.w #$12CE,$80016E at ROM 0x5E7D8) and only THEN
    # spends ~3 frames clearing OBJ/palette and filling the maps -- until
    # the SCR2/SCR3 fills land, the maps hold power-on $0000: unmapped
    # (blank) under the ffight PAL / jtcps1 0x1E, but OBJ tile 0 tiled
    # across the screen under the unrestricted 0x20 the backport ships on
    # (MiSTer wipes SDRAM on reset, so real hardware boots with the same
    # zeros: a grid flash before the ROM check).  rom.py masks the enables
    # out of the boot write ($12CE -> $12C0, the value the engines already
    # park the register at) and detours the first instruction after the
    # SCR3 fill here for the deferred enable.  Stack-free (jmp in, jmp
    # out): work RAM is untested this early -- the stock boot itself calls
    # its fills via jmp (a4) for the same reason.  Under 0x1E both windows
    # render the backdrop pen, so real-PAL hardware and MAME stay
    # pixel-identical; jtcps1 resets layer_ctrl with the enable bits low
    # (jtcps1_mmr.v), so the pre-write window is backdrop there too.
    lab["boothook"] = len(b)
    emit(0x33FC, 0x12CE, 0x0080, 0x016E)     # move.w #$12CE,$80016E.l -- deferred
    emit(0x3039, 0x0080, 0x0160)             # move.w $800160.l,d0 -- displaced insn
    emit(0x4EF9, 0x0005, 0xE846)             # jmp $5E846.l -- resume CPS-B ID check

    title_ex.emit_tick(emit, bxx, lab, b)

    for name, positions in fix.items():
        for pos in positions:
            b[pos + 2:pos + 4] = be16(lab[name] - (pos + 2))
    for name, off in (("clr", 2),):
        pos = lab[name] + off
        assert b[pos:pos + 2] in (bytes.fromhex("51CA"), bytes.fromhex("51CB")), \
            (name, b[pos:pos + 2].hex())
        b[pos + 2:pos + 4] = be16((lab[name] - (pos + 2)) & 0xFFFF)
    LABELS.append(dict(lab))
    return (bytes(b), lab["init"], lab["ovr"], lab["blink"], lab["eovr"],
            lab.get("blk"), lab["objhook"], lab["palhook"], lab["conthook"],
            lab["boothook"])
