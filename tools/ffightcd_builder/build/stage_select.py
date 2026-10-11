"""Stage select: a title-screen code, then a STAGE SELECT screen after
character select.

Hold attack + jump while pressing Start on the credited title screen.
Damnd's laugh (stock cue 0x28) confirms the code.  After character select,
a STAGE SELECT screen shows the arcade's own between-round Metro City map
with the highlighted round lit.  The joystick moves between rounds 1-6 and
a button or Start picks one.  Capcom's own progression routine then sets up
the round.  Runs started this way never enter the high-score table.

Everything drawn is stock: SCROLL1 map variants 4-9 of the $1BB8 graphics
table (the ones the round intro draws), the SCROLL1 palette block at $C2800,
and the big font the stock "SELECT PLAYER" caption uses ($14C2).  Nothing
reaches the appended graphics region, so the screen renders the same on
jtcps1 as in HBMAME.

The code is OUR 68K, assembled instruction by instruction like the rest of
the engine, and placed at BASE in the free tail of the 0x80000 program bank.
rom.py checks every patched stock site byte for byte before replacing it.
"""
from __future__ import annotations

import argparse
import struct

BASE = 0x0F0000                 # free p5 tail, well past ENGINE3

# Work RAM.  No stock operand (absolute or a5-relative) reaches
# 0xFFD800-0xFFFFEF, and it stays zero through a stock round; FFFFF0-F9 is
# the cutscene engine's and FFFFFE the game's.  The 30th AE hack keeps its
# third player at FFF300+ for the same reason.
SAVE_SCR1 = 0xFFD800            # 0x2000: SCROLL1 page 0 while the screen is up
SAVE_PAL = 0xFFF800             # 0x400: SCROLL1 palettes
VARS = 0x7C00                   # a5-relative: $FF8000 + $7C00 = $FFFC00
FLAG = VARS + 0                 # b: code entered on the title screen
SEL = VARS + 1                  # b: highlighted round, 0..5
PREV = VARS + 2                 # b: last frame's held inputs
BLINK = VARS + 3                # b: blink phase counter
SAVE_6E = VARS + 4              # w: layer control shadow
SAVE_SCROLL = VARS + 6          # 4 w: scroll1 shadows $22,$24,$26,$28
SAVE_53A9 = VARS + 14           # b: SCROLL1 page-select flag

# Stock sites and routines (identical in ffightu, ffightj and the EX builds).
TITLE_START = 0x1168            # tst.w -$6d5e(a5); beq.b; move.w; move.b
TITLE_RESUME = 0x117A
TITLE_BYTES = bytes.fromhex("4a6d92a2670c3b6d929e91501b6d92a09152")
CHARSEL_WAIT = 0x4CC8           # move.w #0,d0; jsr $87e.w (sleep through select)
CHARSEL_BYTES = bytes.fromhex("303c00004eb8087e")
RANK = 0x15468                  # high-score rank; d0 = 4 means "not ranked"
RANK_BYTES = bytes.fromhex("303c0004323c0002")
RANK_RESUME = 0x15470
MAP_TABLE = 0x66F3E             # the $1BB8 graphics-record table
MAP_TABLE_LEA = (0x1BB8, bytes.fromhex("41f900066f3e"))
MAP_VARIANT0 = 4                # records 4..9: the map with rounds 1..6 reached
MAP_ORIGIN = 0x908888           # where the intro draws those records
HIGHLIGHT_TABLE = 0x5D496       # per-round masks of the district to light
HIGHLIGHT_CODE = (0x5D480, bytes.fromhex("7000102d00bed000303b000c41fb00082d4800164e75"))
LIT_ATTR = 5                    # the intro lights a district with palette 5
SCR1_PALETTES = 0xC2800         # 32 SCROLL1 palettes, the gameplay set
PAL4 = SCR1_PALETTES + 4 * 32   # map, unlit
PAL5 = SCR1_PALETTES + 5 * 32   # map, lit district
PAL5_LIVE = 0x914400 + 5 * 32
BIGFONT = 0x14C2                # draw one [cell][attr][chars..][0] record
LEVEL_PROGRESS = 0x54D4         # $be -> $c1 (progression index) and $122
SOUND = 0x09F8                  # sound-queue entry, d0 = cue
SND_MOVE = 0x0BBA               # character select's cursor sound
SND_PICK = 0x0BC2               # character select's confirm sound
SLEEP = 0x087E                  # task sleep, d0 = frames (0 = until woken)
LAUGH = 0x28                    # Damnd's laugh
# The laugh runs ~110 frames (MEASURED, -wavwrite).  The game silences every
# channel (cue 0xF0, $4BE0) 19 frames after Start, which cut it short, so
# the title holds this long before the game starts.
LAUGH_HOLD = 104

# Screen layout.  SCROLL1 at scroll (0,0): VRAM pixel = screen + (64,16).
MAP_ROWS, MAP_COLS = 13, 32
MAP_ROW, MAP_COL = 10, 16       # map at screen (64,64)-(319,167): its art
                                # (rows 0-99 of the 104) centred on 224 lines
TITLE_ROW, TITLE_COL = 27, 19   # "STAGE SELECT" where "SELECT PLAYER" sits:
                                # same row, same centre (x 184)
TITLE_ATTR = 0x0186             # the stock "SELECT PLAYER" attribute
TITLE_TEXT = b"SELECT\x10STAGE"  # 0x10 is the big font's space; reads like
                                 # the stock "SELECT PLAYER"
# The round name under the map, in the stock 8x8 font (tile $4400 + ASCII).
# Subway has no label on the map, which is why the line exists at all.
ROUND_ROW = 8                   # "ROUND n": screen y 48-55, above the map
NAME_ROW = 24                   # the name: screen y 176-183, between map and title
NAMES = (b"SLUM", b"SUBWAY", b"WEST SIDE", b"INDUSTRIAL AREA", b"BAY AREA", b"UP TOWN")
NAME_ATTR = 0x0180              # palette 0, the stock text attribute group
NAME_REC = 32                   # bytes per name record: [cell][chars][0]
LAYER_SCR1_ONLY = 0x12C2        # gameplay layer order, only SCROLL1 enabled
BLINK_PERIOD, BLINK_ON = 30, 20  # lit for 20 of every 30 frames
PICK_FRAMES = 40                # fast blink after a pick


def cell(row, col):
    """SCROLL1 page-0 cell address (64x32 cells, column-major)."""
    assert 0 <= row < 32 and 0 <= col < 64
    return 0x908000 + ((row & 0x1F) + (col << 5)) * 4


class Asm:
    def __init__(self, base):
        self.base, self.b, self.lab, self.fix, self.abs = base, bytearray(), {}, [], []

    def w(self, *words):
        for v in words:
            self.b += struct.pack(">H", v & 0xFFFF)

    def l(self, v):
        self.b += struct.pack(">I", v & 0xFFFFFFFF)

    def label(self, name):
        assert name not in self.lab, name
        self.lab[name] = len(self.b)

    def br(self, opcode, name):
        """Bcc / BRA / BSR / DBcc with a 16-bit displacement."""
        self.w(opcode)
        self.fix.append((len(self.b), name))
        self.w(0)

    def la(self, name):
        """A 32-bit absolute address of a label (for lea/jmp operands)."""
        self.abs.append((len(self.b), name))
        self.l(0)

    def finish(self):
        for at, name in self.fix:
            disp = self.lab[name] - at
            assert -0x8000 <= disp < 0x8000, name
            struct.pack_into(">h", self.b, at, disp)
        for at, name in self.abs:
            struct.pack_into(">I", self.b, at, self.base + self.lab[name])
        return bytes(self.b)


BEQ, BNE, BRA, BSR, BPL, BCS = 0x6700, 0x6600, 0x6000, 0x6100, 0x6A00, 0x6500
DBRA = {d: 0x51C8 | d for d in range(8)}


def _copy(a, src, dst, n):
    """move.l loop of n bytes, src/dst absolute or label names (a0/a1/d7)."""
    assert n % 4 == 0
    for opcode, x in ((0x41F9, src), (0x43F9, dst)):
        a.w(opcode)
        a.la(x) if isinstance(x, str) else a.l(x)
    a.w(0x3E3C, n // 4 - 1)                 # move.w #n/4-1,d7
    name = f"copy{len(a.b)}"
    a.label(name)
    a.w(0x22D8)                             # move.l (a0)+,(a1)+
    a.br(DBRA[7], name)


def build(title_bytes):
    """Return (blob, entries).  title_bytes: the 18 stock bytes at $1168."""
    assert len(title_bytes) == TITLE_RESUME - TITLE_START
    a = Asm(BASE)
    w, l, label, br = a.w, a.l, a.label, a.br

    # ---- title-screen Start ---------------------------------------------
    # jmp from $1168, d0 = the start buttons just pressed.  Either player
    # holding attack + jump (bits 4,5; inputs are active low) enters the code.
    label("title")
    w(0x48E7, 0xC080)                       # movem.l d0-d1/a0,-(sp)
    w(0x422D, FLAG)                         # clr.b FLAG(a5)
    for port in (0x800001, 0x800000):       # P1, then P2
        w(0x1239); l(port)                  # move.b port,d1
        w(0x4601)                           # not.b d1
        w(0x0201, 0x0030)                   # andi.b #$30,d1
        w(0x0C01, 0x0030)                   # cmpi.b #$30,d1
        br(BEQ, "code_in")
    br(BRA, "title_out")
    label("code_in")
    w(0x1B7C, 0x0001, FLAG)                 # move.b #1,FLAG(a5)
    w(0x303C, LAUGH)                        # move.w #$28,d0
    w(0x4EB8, SOUND)                        # jsr $9f8.w: Damnd's laugh
    w(0x303C, LAUGH_HOLD)                   # move.w #104,d0
    w(0x4EB8, SLEEP)                        # jsr $87e.w: the title task waits
    label("title_out")
    w(0x4CDF, 0x0103)                       # movem.l (sp)+,d0-d1/a0
    # The displaced stock instructions, verbatim.  Their beq.b skips the two
    # moves, landing on the jmp below exactly where it landed on $117A.
    a.b += title_bytes
    w(0x4EF9); l(TITLE_RESUME)              # jmp $117A

    # ---- high-score rank --------------------------------------------------
    # jmp from $15468.  A stage-select run reports "not ranked".
    label("rank")
    w(0x303C, 0x0004)                       # move.w #4,d0
    w(0x4A2D, FLAG)                         # tst.b FLAG(a5)
    br(BNE, "rank_out")
    w(0x323C, 0x0002)                       # move.w #2,d1
    w(0x4EF9); l(RANK_RESUME)               # jmp $15470
    label("rank_out")
    w(0x4E75)                               # rts

    # ---- after character select ------------------------------------------
    # jsr from $4CC8 in the game task (a5 = $FF8000).  It runs the stock
    # sleep itself, so it returns into the stock code after select ends.
    label("charsel")
    w(0x303C, 0x0000)                       # move.w #0,d0
    w(0x4EB8, SLEEP)                        # jsr $87e.w
    w(0x4A2D, FLAG)                         # tst.b FLAG(a5)
    br(BNE, "screen")
    w(0x4E75)                               # rts: the stock path

    label("screen")
    # Save what the screen borrows: SCROLL1 page 0, its palettes, the
    # layer-control and scroll1 shadows, and the page-select flag.
    _copy(a, 0x908000, SAVE_SCR1, 0x2000)
    _copy(a, 0x914400, SAVE_PAL, 0x400)
    w(0x3B6D, 0x006E, SAVE_6E)              # move.w $6e(a5),SAVE_6E(a5)
    for i, off in enumerate((0x22, 0x24, 0x26, 0x28)):
        w(0x3B6D, off, SAVE_SCROLL + 2 * i)
    w(0x1B6D, 0x53A9, SAVE_53A9)            # move.b $53a9(a5),SAVE_53A9(a5)
    # Only SCROLL1, at scroll (0,0), with the gameplay palettes, page blank.
    w(0x3B7C, LAYER_SCR1_ONLY, 0x006E)      # move.w #$12c2,$6e(a5)
    for off in (0x22, 0x24, 0x26, 0x28):
        w(0x426D, off)                      # clr.w off(a5)
    _copy(a, SCR1_PALETTES, 0x914400, 0x400)
    w(0x41F9); l(0x908000)                  # lea $908000,a0
    w(0x3E3C, 0x2000 // 4 - 1)              # move.w #2047,d7
    label("blank")
    w(0x20FC); l(0x44200000)                # move.l #$44200000,(a0)+: blank
    br(DBRA[7], "blank")
    # The title, in the big font, on page 0 whatever $53a9 says.
    w(0x422D, 0x53A9)                       # clr.b $53a9(a5)
    w(0x43F9); a.la("title_rec")            # lea title_rec,a1
    w(0x4EB8, BIGFONT)                      # jsr $14c2.w
    w(0x422D, SEL)                          # clr.b SEL(a5): round 1
    w(0x1B7C, 0x00FF, PREV)                 # everything counts as held, so a
                                            # button still down from select
                                            # cannot pick before its release
    w(0x422D, BLINK)                        # clr.b BLINK(a5)
    br(BSR, "draw_map")

    label("frame")
    w(0x303C, 0x0001)                       # move.w #1,d0
    w(0x4EB8, SLEEP)                        # jsr $87e.w: next frame
    # d0 = held inputs of either player, 1 = held:
    # bits 0-3 R/L/D/U, 4-5 attack/jump, 6-7 1P/2P start.
    w(0x1039); l(0x800001)                  # move.b $800001,d0
    w(0x1239); l(0x800000)                  # move.b $800000,d1
    w(0xC001)                               # and.b d1,d0
    w(0x4600)                               # not.b d0
    w(0x0200, 0x003F)                       # andi.b #$3f,d0
    w(0x1239); l(0x800018)                  # move.b $800018,d1
    w(0x4601)                               # not.b d1
    w(0x0201, 0x0030)                       # andi.b #$30,d1
    w(0xE509)                               # lsl.b #2,d1
    w(0x8001)                               # or.b d1,d0
    # newly pressed = held now and not held last frame
    w(0x122D, PREV)                         # move.b PREV(a5),d1
    w(0x1B40, PREV)                         # move.b d0,PREV(a5)
    w(0x4601)                               # not.b d1
    w(0xC001)                               # and.b d1,d0
    w(0x1200)                               # move.b d0,d1
    w(0x0201, 0x00F0)                       # andi.b #$f0,d1: button or start
    br(BNE, "pick")
    w(0x1200)                               # move.b d0,d1
    w(0x0201, 0x000A)                       # andi.b #$0a,d1: left or up
    br(BNE, "back")
    w(0x0200, 0x0005)                       # andi.b #$05,d0: right or down
    br(BNE, "fwd")
    # blink the lit district: palette 5 lit for BLINK_ON of BLINK_PERIOD
    w(0x522D, BLINK)                        # addq.b #1,BLINK(a5)
    w(0x0C2D, BLINK_PERIOD, BLINK)          # cmpi.b #30,BLINK(a5)
    br(BCS, "phase")
    w(0x422D, BLINK)                        # clr.b BLINK(a5)
    label("phase")
    w(0x0C2D, BLINK_ON, BLINK)              # cmpi.b #20,BLINK(a5)
    br(BCS, "lit")
    br(BSR, "unlit_pal")
    br(BRA, "frame")
    label("lit")
    br(BSR, "lit_pal")
    br(BRA, "frame")

    label("back")
    w(0x532D, SEL)                          # subq.b #1,SEL(a5)
    br(BPL, "moved")
    w(0x1B7C, 0x0005, SEL)                  # move.b #5,SEL(a5): wrap to 6
    br(BRA, "moved")
    label("fwd")
    w(0x522D, SEL)                          # addq.b #1,SEL(a5)
    w(0x0C2D, 0x0006, SEL)                  # cmpi.b #6,SEL(a5)
    br(BCS, "moved")
    w(0x422D, SEL)                          # clr.b SEL(a5): wrap to 1
    label("moved")
    w(0x4EB8, SND_MOVE)                     # jsr $bba.w
    w(0x422D, BLINK)                        # restart the blink lit
    br(BSR, "draw_map")
    br(BRA, "frame")

    label("pick")
    w(0x4EB8, SND_PICK)                     # jsr $bc2.w
    w(0x3C3C, PICK_FRAMES - 1)              # move.w #39,d6
    label("pick_blink")
    w(0x303C, 0x0001)                       # move.w #1,d0
    w(0x4EB8, SLEEP)                        # jsr $87e.w (keeps d6)
    w(0x0806, 0x0002)                       # btst #2,d6
    br(BEQ, "pick_lit")
    br(BSR, "unlit_pal")
    br(BRA, "pick_next")
    label("pick_lit")
    br(BSR, "lit_pal")
    label("pick_next")
    br(DBRA[6], "pick_blink")
    # Round SEL+1 is level ID SEL.  Capcom's own routine derives the
    # progression index and bonus flag from it, as its debug select did.
    w(0x7000)                               # moveq #0,d0
    w(0x102D, SEL)                          # move.b SEL(a5),d0
    w(0x1B40, 0x00BE)                       # move.b d0,$be(a5)
    w(0x422D, 0x00BF)                       # clr.b $bf(a5)
    w(0x4EB8, LEVEL_PROGRESS)               # jsr $54d4.w
    # Put back everything the screen borrowed.
    _copy(a, SAVE_SCR1, 0x908000, 0x2000)
    _copy(a, SAVE_PAL, 0x914400, 0x400)
    w(0x3B6D, SAVE_6E, 0x006E)              # move.w SAVE_6E(a5),$6e(a5)
    for i, off in enumerate((0x22, 0x24, 0x26, 0x28)):
        w(0x3B6D, SAVE_SCROLL + 2 * i, off)
    w(0x1B6D, SAVE_53A9, 0x53A9)            # move.b SAVE_53A9(a5),$53a9(a5)
    w(0x4E75)                               # rts: into the stock code

    # ---- draw the map lit for round SEL+1 --------------------------------
    # The same record walk as $1BB8's mode 2, but to our cell: 13 rows of
    # 32 (tile, attr) pairs, tile + $4400.
    label("draw_map")
    # Always the round 1 record: the map stays gray, with no green for
    # cleared districts; only the selected district lights, below.
    w(0x7000 | 2 * MAP_VARIANT0)            # moveq #8,d0: record 4
    w(0x41F9); l(MAP_TABLE)                 # lea $66f3e,a0
    w(0x3030, 0x0000)                       # move.w 0(a0,d0.w),d0
    w(0x41F0, 0x0000)                       # lea 0(a0,d0.w),a0
    w(0x2068, 0x000C)                       # movea.l 12(a0),a0: tile data
    w(0x43F9); l(cell(MAP_ROW, MAP_COL))    # lea first cell,a1
    w(0x3A3C, MAP_ROWS - 1)                 # move.w #12,d5
    label("map_row")
    w(0x2449)                               # movea.l a1,a2
    w(0x363C, MAP_COLS - 1)                 # move.w #31,d3
    label("map_col")
    w(0x3018)                               # move.w (a0)+,d0
    w(0x0640, 0x4400)                       # addi.w #$4400,d0
    w(0x34C0)                               # move.w d0,(a2)+
    w(0x34D8)                               # move.w (a0)+,(a2)+
    w(0x45EA, 0x007C)                       # lea $7c(a2),a2: next column
    br(DBRA[3], "map_col")
    w(0x5889)                               # addq.l #4,a1: next row
    br(DBRA[5], "map_row")
    # Light the district the way the intro does ($5D43E): the round's mask
    # record is [cell][rows-1][cols-1][one byte per cell], and cells marked
    # 1 take palette 5.  The record addresses the intro's map position, so
    # shift it to ours (no row wraps: both maps sit inside rows 0-31).
    w(0x7000)                               # moveq #0,d0
    w(0x102D, SEL)                          # move.b SEL(a5),d0
    w(0xD040)                               # add.w d0,d0
    w(0x41F9); l(HIGHLIGHT_TABLE)           # lea $5d496,a0
    w(0x3030, 0x0000)                       # move.w 0(a0,d0.w),d0
    w(0x41F0, 0x0000)                       # lea 0(a0,d0.w),a0
    w(0x2258)                               # movea.l (a0)+,a1
    w(0xD3FC); l(cell(MAP_ROW, MAP_COL) - MAP_ORIGIN)   # adda.l #shift,a1
    w(0x3A18)                               # move.w (a0)+,d5
    w(0x3818)                               # move.w (a0)+,d4
    label("mask_row")
    w(0x3604)                               # move.w d4,d3
    w(0x2449)                               # movea.l a1,a2
    label("mask_col")
    w(0x0C18, 0x0001)                       # cmpi.b #1,(a0)+
    br(BNE, "mask_skip")
    w(0x357C, LIT_ATTR, 0x0002)             # move.w #5,2(a2)
    label("mask_skip")
    w(0x45EA, 0x0080)                       # lea $80(a2),a2: next column
    br(DBRA[3], "mask_col")
    w(0x5889)                               # addq.l #4,a1: next row
    br(DBRA[5], "mask_row")
    # "ROUND n" above the map and the round's name below: blank both lines,
    # then write the record's two [cell][chars][0] segments.
    for row in (ROUND_ROW, NAME_ROW):
        w(0x41F9); l(cell(row, 8))          # lea first visible cell,a0
        w(0x7E00 | 47)                      # moveq #47,d7: 48 visible columns
        label(f"clr{row}")
        w(0x30BC, 0x4420)                   # move.w #$4420,(a0)
        w(0x41E8, 0x0080)                   # lea $80(a0),a0
        br(DBRA[7], f"clr{row}")
    w(0x7000)                               # moveq #0,d0
    w(0x102D, SEL)                          # move.b SEL(a5),d0
    w(0xEB48)                               # lsl.w #5,d0: * NAME_REC
    w(0x43F9); a.la("names")                # lea names,a1
    w(0xD2C0)                               # adda.w d0,a1
    br(BSR, "text")                         # ROUND n
    br(BSR, "text")                         # the name
    br(BRA, "lit_pal")                      # a fresh highlight starts lit

    # one [cell][chars][0] segment at a1 (word aligned); a1 ends aligned
    label("text")
    w(0x3019)                               # move.w (a1)+,d0: cell offset
    w(0x45F9); l(0x908000)                  # lea $908000,a2
    w(0xD4C0)                               # adda.w d0,a2
    label("name_chr")
    w(0x7000)                               # moveq #0,d0
    w(0x1019)                               # move.b (a1)+,d0
    br(BEQ, "text_end")
    w(0x0640, 0x4400)                       # addi.w #$4400,d0
    w(0x34C0)                               # move.w d0,(a2)+
    w(0x34FC, NAME_ATTR)                    # move.w #attr,(a2)+
    w(0x45EA, 0x007C)                       # lea $7c(a2),a2: next column
    br(BRA, "name_chr")
    label("text_end")
    w(0x5289)                               # addq.l #1,a1
    w(0x2009)                               # move.l a1,d0
    w(0x0200, 0x00FE)                       # andi.b #$fe,d0: round down to even
    w(0x2240)                               # movea.l d0,a1
    w(0x4E75)

    label("lit_pal")
    _copy(a, PAL5, PAL5_LIVE, 32)
    w(0x4E75)
    label("unlit_pal")
    _copy(a, PAL4, PAL5_LIVE, 32)
    w(0x4E75)

    w(0x4E71) if len(a.b) % 2 else None
    label("names")
    def segment(row, text):
        col = (64 - len(text)) // 2     # centre on the 48 visible columns 8..55
        seg = struct.pack(">H", cell(row, col) - 0x908000) + text + b"\0"
        return seg + (b"\0" if len(seg) % 2 else b"")
    for k, name in enumerate(NAMES):
        r = segment(ROUND_ROW, b"ROUND %d" % (k + 1)) + segment(NAME_ROW, name)
        assert len(r) <= NAME_REC
        a.b += r + b"\0" * (NAME_REC - len(r))
    label("title_rec")
    rec = struct.pack(">HH", cell(TITLE_ROW, TITLE_COL) - 0x908000, TITLE_ATTR)
    rec += TITLE_TEXT + b"\0"
    a.b += rec + (b"\0" if len(rec) % 2 else b"")

    blob = a.finish()
    entries = {k: BASE + a.lab[k] for k in ("title", "rank", "charsel")}
    return blob, entries


def install(w_pair, r_pair, p5):
    """Patch the three stock sites and place the code.  w_pair/r_pair
    address the 0x00000-0x7FFFF program space; p5 is the word-swapped
    0x80000-0xFFFFF bank."""
    assert r_pair(*MAP_TABLE_LEA[:1], len(MAP_TABLE_LEA[1])) == MAP_TABLE_LEA[1]
    for k in range(6):
        off = struct.unpack(">H", r_pair(MAP_TABLE + 2 * (MAP_VARIANT0 + k), 2))[0]
        mode, dest, rows, cols, attr = struct.unpack(">HIHHH", r_pair(MAP_TABLE + off, 12))
        assert (mode, rows + 1, cols + 1) == (2, MAP_ROWS, MAP_COLS), (k, mode, rows, cols)
    assert r_pair(HIGHLIGHT_CODE[0], len(HIGHLIGHT_CODE[1])) == HIGHLIGHT_CODE[1]
    for k in range(6):
        off = struct.unpack(">h", r_pair(HIGHLIGHT_TABLE + 2 * k, 2))[0]
        dest, rows, cols = struct.unpack(">IHH", r_pair(HIGHLIGHT_TABLE + off, 8))
        # the mask must land inside the map we draw
        rel = (dest - MAP_ORIGIN) // 4
        r0, c0 = rel & 0x1F, rel >> 5
        assert (dest - MAP_ORIGIN) % 4 == 0 and r0 + rows < MAP_ROWS and c0 + cols < MAP_COLS, k
    title_bytes = r_pair(TITLE_START, TITLE_RESUME - TITLE_START)
    assert title_bytes == TITLE_BYTES, title_bytes.hex()
    assert r_pair(CHARSEL_WAIT, 8) == CHARSEL_BYTES
    assert r_pair(RANK, 8) == RANK_BYTES
    blob, entries = build(title_bytes)
    jmp = lambda target: b"\x4e\xf9" + struct.pack(">I", target)
    w_pair(TITLE_START, jmp(entries["title"]))
    w_pair(CHARSEL_WAIT, b"\x4e\xb9" + struct.pack(">I", entries["charsel"]) + b"\x4e\x71")
    w_pair(RANK, jmp(entries["rank"]) + b"\x4e\x71")
    for i, v in enumerate(blob):
        a = BASE + i - 0x80000
        assert p5[a ^ 1] == 0xFF, f"stage select code would overwrite {BASE + i:#x}"
        p5[a ^ 1] = v
    return blob, entries


def self_test():
    stock = TITLE_BYTES
    blob, entries = build(stock)
    assert len(blob) % 2 == 0
    # the displaced title code's beq.b must still reach the resume jmp
    at = blob.index(stock)
    assert blob[at + len(stock):at + len(stock) + 6] == bytes.fromhex("4ef90000117a")
    assert at + 4 + 2 + 0x0C == at + len(stock)
    try:
        from capstone import Cs, CS_ARCH_M68K, CS_MODE_M68K_000
    except ImportError:
        print("stage select: assembled (capstone absent, listing skipped)")
        return blob
    md = Cs(CS_ARCH_M68K, CS_MODE_M68K_000)
    n = sum(1 for _ in md.disasm(blob, BASE))
    print(f"stage select: {len(blob)} bytes at {BASE:#x}, {n} instructions; "
          f"entries " + ", ".join(f"{k} {v:#x}" for k, v in entries.items()))
    return blob


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.add_argument("--listing", action="store_true")
    args = parser.parse_args()
    blob = self_test()
    if args.listing:
        from capstone import Cs, CS_ARCH_M68K, CS_MODE_M68K_000
        for i in Cs(CS_ARCH_M68K, CS_MODE_M68K_000).disasm(blob, BASE):
            print(f"{i.address:06X}  {i.mnemonic:9s} {i.op_str}")
