"""Caption and credit text: layout, glyph selection, and the ROM tables.

The captions are the CD's dialogue rendered as CPS-1 text; the credits are
the arcade staff roll re-flowed to fit the backport's timing.
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
from engine import CAPTAB, CAPPAL, be16  # noqa: E402

# ---- JP reunion subtitles: the ROM's OWN kana, retimed to the CD voice.
# Anchored by MEASURING the CD's mouth animation, not by trusting a clock:
# the E6 hijack capture's audio is the wrong CD-DA track (that is a known
# property of the hijack), so the session wav cannot align anything.  The
# animation can -- it was authored to the Japanese voice.  Haggar's measured
# mouth blocks line up with data/audio/ending_vo_jp.tsv at a CONSTANT 4.28 s
# offset (track 13.10 -> scene 8.82, 17.44 -> 13.16, 21.74 -> 17.46), which
# also mirrors the US scene's 4.2 s lead.  Frames below are scene-relative,
# taken from the mouth-block onsets themselves.
#
# (t0, t1, group-1 index, line, band row)   line = index within the record
# Kana glyph ids, MEASURED on the JP board (not inferred from gojuon
# order -- the ordering has filler runs: 0x48e-0x491 and 0x49e-0x49f are
# solid blocks, so SU/SE/SO are at 0x492-0x494, NOT right after SHI).
# Method: poke each candidate into scroll1 with every other layer's
# palette blanked and palette 23 forced white, then read the screen --
# everything but the poked row goes black, so no screen-coordinate model
# is needed.  Cross-checked against the ROM's own reunion records
# 0x39/0x3b, whose readings are known: SHI/TA/CHI/TSU/TE/TO/NA/NI/HA/MA/
# MO/RA/RI/RE all land on their measured ids.
#
# Read the probe with a LEADING DUMMY.  The first cell of a poked row
# sits at the left screen edge and is clipped, so a probe row that starts
# with a real glyph silently loses it and shifts everything after it by
# one -- which is exactly how SU first came out as 0x491 (a filler block)
# and shipped a corrupt tile in "iku to SURU ka".  Anchor every reading
# against a verified glyph at the RIGHT-hand end of the row.
KANA_GLYPH = {
    "あ": 0x482, "い": 0x483, "う": 0x484, "え": 0x485, "お": 0x486,
    "か": 0x487, "き": 0x488, "く": 0x489, "け": 0x48a, "こ": 0x48b,
    "さ": 0x48c, "し": 0x48d, "す": 0x492, "せ": 0x493, "そ": 0x494,
    "た": 0x495, "ち": 0x496, "つ": 0x497, "て": 0x498, "と": 0x499,
    "な": 0x49a, "に": 0x49b, "ぬ": 0x49c, "ね": 0x49d, "の": 0x4a0,
    "は": 0x4a1, "ひ": 0x4a2, "ふ": 0x4a3, "へ": 0x4a4, "ほ": 0x4a5,
    "ま": 0x4a6, "み": 0x4a7, "む": 0x4a8, "め": 0x4a9, "も": 0x4aa,
    "や": 0x4ab, "ゆ": 0x4ac, "よ": 0x4ad,
    "ら": 0x4ae, "り": 0x4af, "る": 0x4b0, "れ": 0x4b1, "ろ": 0x4b2,
    "わ": 0x4b3, "を": 0x4b4, "ん": 0x4b5, "っ": 0x4b6, "ー": 0x4ba,
    "。": 0x4bd, "、": 0x4be, "！": 0x4bf,
    "ォ": 0x4c0, "ェ": 0x4c1, "ィ": 0x4c2, "死": 0x4c4,
    "カ": 0x4d5, "コ": 0x4d9, "シ": 0x4db, "テ": 0x4e2,
    " ": 0x020,
}
DAKUTEN_GLYPH = 0x4bb
# voiced kana are the base glyph plus a dakuten ONE ROW ABOVE (the font
# has no precomposed voiced forms -- this is how the ROM's own records
# spell BU/JI/DE in "yoku buji de ite kureta")
VOICED = {"が": "か", "ぎ": "き", "ぐ": "く", "げ": "け", "ご": "こ",
          "ざ": "さ", "じ": "し", "ず": "す", "ぜ": "せ", "ぞ": "そ",
          "だ": "た", "ぢ": "ち", "づ": "つ", "で": "て", "ど": "と",
          "ば": "は", "び": "ひ", "ぶ": "ふ", "べ": "へ", "ぼ": "ほ",
          "ジ": "シ"}
# ---- ENDING CREDITS ---------------------------------------------------
# The stock staff roll is a flat stream of rows at 0x018F94, reached from
# ONE instruction (0x018E5C `move.l #$018F94,$70(a6)`), byte-identical in US
# and JP.  Row = [u16 attr][u16 glyph]...[u16 0].  attr bit15 ends the
# stream; tile = 0x4400 + glyph; each glyph advances one COLUMN.
CREDITS_SRC = 0x018F94
CREDITS_FIELD = 16             # the roll's column field; rows are centred
# Lowercase double-height header font, SOLVED from the ROM's own headers
# rather than guessed: "character design" fixed a/c/d/e/g/h/i/n/r/s/t,
# "object" gave b and j, "planner" gave l and p, "music sound" gave u and
# "special thanks" gave k.  Letters are ONE tile wide except 'm', which is
# a two-tile pair (0x0EE,0x0EF) -- that pair is why "programmer" decodes as
# 12 cells for 10 letters.  Only the letters needed are listed; anything
# else asserts rather than drawing junk.
CREDITS_LC = {
    'a': 0x0E4, 'b': 0x0E5, 'c': 0x0E6, 'd': 0x0E7, 'e': 0x0E8,
    'h': 0x0EA, 'i': 0x0EB, 'k': 0x0EC, 'l': 0x0ED,
    'n': 0x100, 'o': 0x101, 'r': 0x102, 's': 0x103, 't': 0x104,
    'u': 0x105, 'g': 0x10B, 'j': 0x10C, 'p': 0x10D, ' ': 0x020,
}
# 'm' is the one WIDE letter: two tiles, not one.  Kept separate so the
# cell-count assert in _cred_header sees its true width -- this is exactly
# what makes "mega cd backport" 17 cells and therefore unbuildable.
CREDITS_LC_PAIR = {'m': (0x0EE, 0x0EF)}
CREDITS_DOT = 0x60             # the dotted spine running down the column
def _cred_row(attr, cells):
    return be16(attr) + b"".join(be16(c) for c in cells) + be16(0)
def _cred_dot_row():
    lead = (CREDITS_FIELD - 1) // 2
    return _cred_row(0x0000, [0x20] * lead + [CREDITS_DOT])


def rom_caption_lines(region_key: str, idx: int):
    """Pull a ROM caption record apart into placeable LINES.

    The JP ending text already exists in the ROM (ids 0x139/0x13b), and the
    kana font ships with the JP board's gfx -- so JP captions REUSE the
    ROM's own glyph codes rather than mapping text to tiles.  This matters:
    the Latin path emits 0x4400 + ASCII, which cannot express kana at all.

    Kana dakuten are separate glyphs sitting one row ABOVE their base, so a
    "line" is its base row plus any marks on base-1.  Columns are returned
    relative to the line's leftmost cell so it can be re-centred in the
    caption band.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ffcd.captions import build_image, decode_group1
    _, _, segs = decode_group1(build_image(region_key), idx)
    rows = {}
    for cell, attr, glyphs in segs:
        col, row = cell // 0x80, (cell % 0x80) // 4
        if not glyphs or all(g == 0x020 for g in glyphs):
            continue
        rows.setdefault(row, []).append((col, attr, glyphs))
    DAKUTEN = 0x4bb
    out = []
    for b in sorted(r for r in rows
                    if any(any(g != DAKUTEN for g in gl) for _, _, gl in rows[r])):
        marks = rows.get(b - 1, []) if all(
            all(g == DAKUTEN for g in gl) for _, _, gl in rows.get(b - 1, [(0, 0, [DAKUTEN])])
        ) else []
        cells = []
        left = min(c for c, _, _ in rows[b])
        for col, attr, glyphs in rows[b]:
            for k, g in enumerate(glyphs):
                if g != 0x020:
                    cells.append((col - left + k, 0, g, attr))
        for col, attr, glyphs in marks:
            for k, g in enumerate(glyphs):
                if g != 0x020:
                    cells.append((col - left + k, -1, g, attr))
        # Type each dakuten WITH its base, not after the whole line.  The
        # walker emits one cell per frame in list order, so building every
        # base row first and then every mark types the line out in full and
        # only then dots the marks in on top -- user: the marks "render in
        # later than the kana, which looks bad".  Column order, base (dy 0)
        # before its mark (dy -1), so a voiced kana and its mark land one
        # frame apart.
        cells.sort(key=lambda c: (c[0], -c[1]))
        width = max(c for c, _, _, _ in cells) + 1
        out.append(dict(base=b, width=width, cells=cells,
                        pal=rows[b][0][1] & 0x1F))
    return out


def build_ending_captions_rom(region_key: str, sched) -> bytes:
    """JP subtitle blob: (t0, t1, id, line_index, band_row) records.

    Same E-CAPS format the Latin builder emits, but the cells carry the
    ROM's kana glyph codes.  band_row is where the line's BASE sits; any
    dakuten land one row above it, which is why single lines go at 23 and
    pairs at 22/24 -- all inside the vblank band wipe (rows 20..27).
    """
    out = bytearray()
    cache = {}
    for rec in sched:
        if isinstance(rec[2], (str, tuple, list)):
            # AUTHORED line(s): (t0, t1, kana_text, band_row, pal).  The
            # text may be one string or a tuple of two; a pair goes in ONE
            # record (rows band_row and band_row+2) so the walker types
            # them in sequence at its usual one cell per frame.  Rendered
            # with the same glyph codes, tile formula and speaker palettes
            # as the ROM path -- only the source of the text differs.
            # (Keyed on the text field's type, not on tuple length: the ROM
            # form is a 5-tuple too.)
            t0, t1, text, band_row, attr = rec
            lines = (text,) if isinstance(text, str) else tuple(text)
            cells = []
            for li, line in enumerate(lines):
                row = band_row + li * 2
                col0 = 8 + (48 - len(line)) // 2
                for k, ch in enumerate(line):
                    base = VOICED.get(ch, ch)
                    g = KANA_GLYPH.get(base)
                    assert g is not None, f"no glyph for {ch!r} in {line!r}"
                    off = (col0 + k) * 0x80 + row * 4
                    cells.append((off, 0x4400 + g, attr))
                    if ch in VOICED:
                        up = (col0 + k) * 0x80 + (row - 1) * 4
                        cells.append((up, 0x4400 + DAKUTEN_GLYPH, attr))
            out += be16(t0) + be16(t1) + be16(len(cells))
            for off, tile, a in cells:
                out += be16(off) + be16(tile) + be16(a)
            continue
        t0, t1, idx, line_i, band_row = rec
        lines = cache.setdefault(idx, rom_caption_lines(region_key, idx))
        ln = lines[line_i]
        col0 = 8 + (48 - ln["width"]) // 2
        cells = []
        for dx, dy, g, attr in ln["cells"]:
            off = (col0 + dx) * 0x80 + (band_row + dy) * 4
            # tile = 0x4400 + glyph -- the SAME formula the Latin path uses
            # (there the glyph id IS the ASCII code); kana simply occupy
            # higher glyph ids in the same ending font, ONE tile each.
            # Confirmed against the stock game: dumping scroll1 while the
            # unpatched JP ending types id 0x13b gives 4886 4899 4884 488c
            # 48b5 48be for glyphs 486 499 484 48c 4b5 4be.
            # I had guessed 0x4000 + glyph (which lands in the Latin range,
            # so the captions came out as English fragments) and then
            # invented a double-height rule on top of that.
            cells.append((off, 0x4400 + g, attr))
        out += be16(t0) + be16(t1) + be16(len(cells))
        for off, tile, attr in cells:
            out += be16(off) + be16(tile) + be16(attr)
    out += be16(0xFFFF)
    return bytes(out)


def build_ending_captions(sched) -> bytes:
    """ENDING_CAPTIONS -> E-CAPS blob for the engine's subtitle walker.

    Cells use the ending typewriter's own conventions (probe-verified):
    tile = 0x4400 + ASCII, attr = palette word, map cell = col*0x80+row*4
    in page 0x908000.  Measured on-set mapping: map col = screen col + 8,
    map row = screen row (no y offset, unlike the stock ending's -16).
    Lines are centered on the visible width and vertically centered in
    the band below the letterboxed art (art = screen rows 4..19, band =
    20..27): one line at row 23, two at rows 22/24 -- all inside the
    vblank band wipe (rows 20..27).
    """
    out = bytearray()
    for t0, t1, pal, lines in sched:
        cells = []
        rows = (23,) if len(lines) == 1 else (22, 24)
        for row, line in zip(rows, lines):
            assert len(line) <= 48, line
            col0 = 8 + (48 - len(line)) // 2
            for k, ch in enumerate(line):
                off = (col0 + k) * 0x80 + row * 4
                cells.append((off, 0x4400 + ord(ch), pal))
        out += be16(t0) + be16(t1) + be16(len(cells))
        for off, tile, attr in cells:
            out += be16(off) + be16(tile) + be16(attr)
    out += be16(0xFFFF)
    return bytes(out)


def _cred_header(text):
    """A magenta section header, as TWO rows -- the bottom half is +0x10."""
    cells = []
    for ch in text:
        pair = CREDITS_LC_PAIR.get(ch)
        if pair:
            cells.extend(pair)
            continue
        g = CREDITS_LC.get(ch)
        assert g is not None, f"no credits glyph for {ch!r} in {text!r}"
        cells.append(g)
    assert len(cells) <= CREDITS_FIELD, (
        f"header {text!r} is {len(cells)} cells; the field is {CREDITS_FIELD} "
        f"and the widest stock header ('character design') already ends 2 px "
        f"from the screen edge, so anything wider is drawn off-screen. "
        f"NOTE 'm' costs TWO cells.")
    lead = [0x20] * ((CREDITS_FIELD - len(cells)) // 2)
    bot = [c if c == 0x20 else c + 0x10 for c in cells]
    return (_cred_row(0x0006, lead + cells)
            + _cred_row(0x0006, lead + bot))


def _cred_name(text):
    """A name row: single height, plain ASCII, the orange palette."""
    cells = [ord(c) for c in text]
    lead = [0x20] * ((CREDITS_FIELD - len(cells)) // 2)
    return _cred_row(0x0000, lead + cells)


def _cred_parse(src):
    """Split the stock stream into whole rows, terminator included."""
    rows, i = [], 0
    while True:
        attr = struct.unpack_from(">H", src, i)[0]
        start = i
        i += 2
        if attr & 0x8000:
            rows.append((attr, bytes(src[start:i])))
            return rows
        while struct.unpack_from(">H", src, i)[0] != 0:
            i += 2
        i += 2
        rows.append((attr, bytes(src[start:i])))
        assert i < len(src), "ran off the end of the credits stream"


def _cred_glyphs(raw):
    return [struct.unpack_from(">H", raw, k)[0]
            for k in range(2, len(raw) - 2, 2)]


def _cred_collapse_blanks(rows):
    """Squeeze runs containing FULLY BLANK rows down to a single dot.

    The roll separates most things with its dotted spine, but three gaps
    are padded with rows carrying no glyphs at all -- 2 before "planner",
    4 before "programmer", 2 before the second "character design" -- and
    those read as a hole in the dotted line rather than as spacing.  Any
    maximal run of blank/dot rows that contains at least one BLANK becomes
    one dot; runs that are already all dots (the 3-dot gap every header
    uses before its first name) are left exactly alone.
    """
    out, i, saved = [], 0, 0
    while i < len(rows):
        attr, raw = rows[i]
        gl = _cred_glyphs(raw)
        is_blank = not gl
        is_dot = bool(gl) and all(g in (0x20, CREDITS_DOT) for g in gl)
        if not (attr & 0x8000) and (is_blank or is_dot):
            j = i
            has_blank = False
            while j < len(rows):
                a2, r2 = rows[j]
                if a2 & 0x8000:
                    break
                g2 = _cred_glyphs(r2)
                b2 = not g2
                d2 = bool(g2) and all(g in (0x20, CREDITS_DOT) for g in g2)
                if not (b2 or d2):
                    break
                has_blank |= b2
                j += 1
            if has_blank:
                out.append((0x0000, _cred_dot_row()))
                saved += (j - i) - 1
                i = j
                continue
        out.append((attr, raw))
        i += 1
    return out, saved


def _cred_is_header_top(raw):
    """A header's TOP row: attr 0x0006 and no bottom-half glyph (>= 0x110)."""
    return not any(struct.unpack_from(">H", raw, k)[0] >= 0x110
                   for k in range(2, len(raw) - 2, 2))


def build_credits(read, header: str) -> bytes:
    """Stock roll + the backport credit, as a relocatable blob.

    Placed immediately BEFORE the last section header ("special thanks"),
    which is where review asked for it, and written in the roll's own idiom.
    The stock rhythm, read off the stream itself:

        HEADER (two rows) | 3 dots | name, dot, name, dot ... | 1 dot | next
        HEADER

    so the block is header + 3 dots + name + 1 dot = SEVEN rows.  It needs
    no leading dot of its own: the row before the insertion point is
    already a single dot -- in the stock roll it separates "music sound"'s
    names from the "special thanks" header -- and it serves as our block's
    leading dot.  Every stock row is copied verbatim -- no existing gap
    changes.

    This ADDS 7 rows.  The roll is data-driven -- one row per 16 frames, and
    the ending phase advances when the drawer meets the terminator -- so the
    ending grows by 112 frames and everything after the credits, including
    the CD-6 farewell, lands that much later against an arrange track that
    does not move.  The farewell caption schedules are shifted to match; see
    FAREWELL_CAPTIONS_*.  Reclaiming the frames from the stock dot rows
    instead was rejected: the dots are the visible spine of the credit
    column, not padding.
    """
    rows = _cred_parse(read(CREDITS_SRC, 0x1000))
    n0 = len(rows)
    rows, saved = _cred_collapse_blanks(rows)
    tops = [k for k, (attr, raw) in enumerate(rows)
            if attr == 0x0006 and _cred_is_header_top(raw)]
    assert tops, "no section headers found in the credits stream"
    at = tops[-1]                      # "special thanks"
    block = (_cred_header(header)
             + _cred_dot_row() * 3
             + _cred_name("STEVE GORDON")
             + _cred_dot_row())
    out = (b"".join(r for _, r in rows[:at]) + block
           + b"".join(r for _, r in rows[at:]))
    n1 = len(rows) + 7
    print(f"  credits: {n0} rows -> {n1} "
          f"(-{saved} blank-run, +7 backport) = {(n1 - n0) * 16:+d} f")
    return out
