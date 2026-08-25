"""CPS-2 graphics format transforms (vendored, mechanical, public format).

Port of the reverse-engineered CPS-2 gfx encoding: ROM_LOAD64_WORD interleave,
the per-0x200000-bank 64-bit word shuffle, and the planar tile encoding. The
disc's chunky tiles (low-nibble-first) are encoded to planar and placed into
the shuffled interleaved region; unshuffle + deinterleave yields MAME ROMs.

No game data — a byte-format codec only. Round-trip byte-exact vs arcade ROMs.
"""
import array

BANK_WORDS = 0x200000 // 8


def interleave(roms):
    n = len(roms[0])
    reg = bytearray(n * 4)
    for j in range(4):
        r = roms[j]
        for w in range(n // 2):
            reg[w * 8 + j * 2: w * 8 + j * 2 + 2] = r[w * 2: w * 2 + 2]
    return bytes(reg)


def deinterleave(reg):
    n = len(reg) // 8
    roms = [bytearray(n * 2) for _ in range(4)]
    for j in range(4):
        for w in range(n):
            roms[j][w * 2: w * 2 + 2] = reg[w * 8 + j * 2: w * 8 + j * 2 + 2]
    return [bytes(r) for r in roms]


def _shuffle(buf, s, l):
    if l == 2:
        return
    l //= 2
    for i in range(0, l, 2):
        buf[s + i + 1], buf[s + l + i] = buf[s + l + i], buf[s + i + 1]
    _shuffle(buf, s, l)
    _shuffle(buf, s + l, l)


def _unshuffle(buf, s, l):
    if l == 2:
        return
    l //= 2
    _unshuffle(buf, s, l)
    _unshuffle(buf, s + l, l)
    for i in range(0, l, 2):
        buf[s + i + 1], buf[s + l + i] = buf[s + l + i], buf[s + i + 1]


def _apply(region, fn):
    w = array.array('Q')
    w.frombytes(region)
    for b in range(0, len(w), BANK_WORDS):
        fn(w, b, BANK_WORDS)
    return w.tobytes()


def planar(roms4):
    """4 bitplane ROMs -> shuffled interleaved planar region (the placement space)."""
    return _apply(interleave(roms4), _shuffle)


def planar_to_roms(region):
    """shuffled planar region -> 4 MAME-loadable bitplane ROMs."""
    return deinterleave(_apply(region, _unshuffle))


def encode_true_tile(tile: bytes) -> bytes:
    """One disc chunky tile (low-nibble-first) -> one 128-byte CPS-2 planar tile."""
    px = []
    for v in tile:
        px.append(v & 0x0F)
        px.append(v >> 4)
    out = bytearray(128)
    for row in range(16):
        for half in range(2):
            g = [0, 0, 0, 0]
            for col in range(8):
                p = px[row * 16 + half * 8 + col]
                bit = 7 - col
                for pl in range(4):
                    g[pl] |= ((p >> pl) & 1) << bit
            s = row * 8 + half * 4
            out[s:s + 4] = bytes(g)
    return bytes(out)
