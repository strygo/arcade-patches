"""CPS-1 formats.

  * the graphics codec -- 64-bit-interleaved ROM set <-> chunky tiles, the
    layout every c07.cNN member uses;
  * the sprite-patch record -- the 12-byte row the engine reads for the
    farewell's OBJ overlay, defined once so the writer and the reader
    cannot disagree.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

FFIGHT_GFX = ["ff-5m.7a", "ff-7m.9a", "ff-1m.3a", "ff-3m.5a"]  # offsets 0/2/4/6


def interleave64(roms4: list[bytes]) -> bytes:
    """4 ROMs (byte offsets 0/2/4/6, ROM_LOAD64_WORD) -> linear gfx space."""
    n = len(roms4[0])
    assert all(len(r) == n for r in roms4)
    space = bytearray(n * 4)
    for fi, rom in enumerate(roms4):
        off = fi * 2
        for w in range(n // 2):
            space[w * 8 + off:w * 8 + off + 2] = rom[w * 2:w * 2 + 2]
    return bytes(space)


def deinterleave64(space: bytes) -> list[bytes]:
    n = len(space) // 4
    roms = []
    for fi in range(4):
        off = fi * 2
        rom = bytearray(n)
        for w in range(n // 2):
            rom[w * 2:w * 2 + 2] = space[w * 8 + off:w * 8 + off + 2]
        roms.append(bytes(rom))
    return roms


# Layout (MEASURED against cps1_layout16x16, drivers/cps1.cpp:3354, and
# verified by rendering -- the CPS2 codec's 16-bit-wide planes do NOT carry
# over; the first render attempt with them came out shredded):
# each 8-byte row = [b0 b1 b2 b3] planes LSB..MSB for pixels 0-7,
# then [b4 b5 b6 b7] planes LSB..MSB for pixels 8-15;
# pixel x reads bit (7 - x&7) of each plane byte, MSB-first.

def planar_to_chunky(reg: bytes) -> bytes:
    """CPS1 planar (128 B/16x16 tile) -> chunky (2 px/byte, left px high)."""
    out = bytearray(len(reg))
    for base in range(0, len(reg), 128):
        oi = base
        for row in range(16):
            b = reg[base + row * 8: base + row * 8 + 8]
            pens = []
            for half in range(2):
                p0, p1, p2, p3 = b[half * 4:half * 4 + 4]
                for x in range(8):
                    bt = 7 - x
                    pens.append((((p3 >> bt) & 1) << 3)
                                | (((p2 >> bt) & 1) << 2)
                                | (((p1 >> bt) & 1) << 1)
                                | ((p0 >> bt) & 1))
            for c in range(0, 16, 2):
                out[oi] = (pens[c] << 4) | pens[c + 1]
                oi += 1
    return bytes(out)


def chunky_to_planar(ch: bytes) -> bytes:
    out = bytearray(len(ch))
    for base in range(0, len(ch), 128):
        for row in range(16):
            px = []
            for k in range(8):
                b = ch[base + row * 8 + k]
                px.append(b >> 4)
                px.append(b & 0xF)
            oi = base + row * 8
            for half in range(2):
                p = [0, 0, 0, 0]
                for x in range(8):
                    v = px[half * 8 + x]
                    bt = 7 - x
                    for pl in range(4):
                        p[pl] |= ((v >> pl) & 1) << bt
                for pl in range(4):
                    out[oi + half * 4 + pl] = p[pl]
    return bytes(out)


def to_chunky(roms4: list[bytes]) -> bytes:
    return planar_to_chunky(interleave64(roms4))


def to_cps1(chunky: bytes) -> list[bytes]:
    return deinterleave64(chunky_to_planar(chunky))


def main() -> int:
    archive = sys.argv[1] if len(sys.argv) > 1 else "ffight.7z"
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["7zz", "e", "-y", f"-o{td}", archive] + FFIGHT_GFX,
                       check=True, capture_output=True)
        roms = [Path(td, n).read_bytes() for n in FFIGHT_GFX]
    ch = to_chunky(roms)
    back = to_cps1(ch)
    ok = all(back[i] == roms[i] for i in range(4))
    print(f"round-trip byte-exact: {ok} ({len(ch) // 128} tiles)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())



import struct

FMT = ">6H"
SIZE = struct.calcsize(FMT)          # 12
NEVER = 0x7FFF                       # end gate; compared SIGNED by the walker
TERMINATOR = struct.pack(">H", 0xFFFF)


def pack(band, col, mind1, maxd1, code, objpal) -> bytes:
    return struct.pack(FMT, band, col, mind1, maxd1, code, objpal)


def unpack_all(blob: bytes):
    """Every record in a patches.bin, terminator excluded."""
    n = (len(blob) - len(TERMINATOR)) // SIZE
    if (len(blob) - len(TERMINATOR)) % SIZE:
        raise ValueError(f"patches.bin is {len(blob)} B, not a whole number "
                         f"of {SIZE}-byte records + terminator")
    return [struct.unpack_from(FMT, blob, i * SIZE) for i in range(n)]


def gate_offsets(index: int):
    """Byte offsets of (mind1, maxd1) for record `index`.

    retime.py renumbers BOTH gates when it splices an event in; a
    gate left alone fires one event early.
    """
    base = index * SIZE
    return base + 2 * 2, base + 3 * 2
