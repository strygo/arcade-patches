"""CPS-2 sprite graphics <-> 128-byte planar tiles, and PS2 disc tiles -> planar.

A CPS-2 graphics group is four ROMs interleaved by 16-bit words into 64-bit
region words, then shuffled per 0x200000-byte bank.  A bank decodes from a
quarter-sized slice of each of its four ROMs, so tiles moved between banks or
games are copies at tile level even though their ROM bytes scatter.

Nothing here holds game data; every input comes from the user's own files.
"""
import array

TILE = 128
BANK_BYTES = 0x200000                 # shuffle granularity (decoded bytes)
MEMBER_SLICE = BANK_BYTES // 4        # each ROM's share of one bank
TILES_PER_BANK = BANK_BYTES // TILE
GROUPS = (("13", "15", "17", "19"), ("14", "16", "18", "20"))
_WORDS = BANK_BYTES // 8


def _interleave(slices):
    out = bytearray(len(slices[0]) * 4)
    for j, s in enumerate(slices):
        out[2 * j::8] = s[0::2]
        out[2 * j + 1::8] = s[1::2]
    return bytes(out)


def _deinterleave(region):
    return [bytes(_merge(region[2 * j::8], region[2 * j + 1::8])) for j in range(4)]


def _merge(even, odd):
    out = bytearray(len(even) * 2)
    out[0::2], out[1::2] = even, odd
    return out


def _shuffle(w, s, n):
    if n == 2:
        return
    n //= 2
    for i in range(0, n, 2):
        w[s + i + 1], w[s + n + i] = w[s + n + i], w[s + i + 1]
    _shuffle(w, s, n)
    _shuffle(w, s + n, n)


def _unshuffle(w, s, n):
    if n == 2:
        return
    n //= 2
    _unshuffle(w, s, n)
    _unshuffle(w, s + n, n)
    for i in range(0, n, 2):
        w[s + i + 1], w[s + n + i] = w[s + n + i], w[s + i + 1]


def _apply(region, fn):
    w = array.array("Q")
    w.frombytes(region)
    for b in range(0, len(w), _WORDS):
        fn(w, b, _WORDS)
    return w.tobytes()


def decode_bank(members4, bank):
    """Decoded (MAME region order) bytes of shuffle bank `bank` of a four-ROM group."""
    slices = [m[bank * MEMBER_SLICE:(bank + 1) * MEMBER_SLICE] for m in members4]
    if any(len(s) != MEMBER_SLICE for s in slices):
        raise ValueError(f"graphics ROMs too small for bank {bank}")
    return _apply(_interleave(slices), _shuffle)


def encode_bank(decoded):
    """Inverse of decode_bank: the four ROM slices of one bank."""
    if len(decoded) != BANK_BYTES:
        raise ValueError("decoded bank size")
    return _deinterleave(_apply(decoded, _unshuffle))


def decode_group(members4):
    """Every bank of a four-ROM group, decoded and concatenated."""
    return b"".join(decode_bank(members4, b) for b in range(len(members4[0]) // MEMBER_SLICE))


def encode_group(decoded):
    """Inverse of decode_group: the four ROMs."""
    parts = [encode_bank(decoded[b:b + BANK_BYTES]) for b in range(0, len(decoded), BANK_BYTES)]
    return [b"".join(p[j] for p in parts) for j in range(4)]


def disc_to_planar(chunky):
    """PS2 chunky tiles (left pixel = low nibble) -> decoded CPS-2 tile bytes: per
    4-byte group of 8 pixels, plane p's byte holds bit p of pixel k at bit 7-k."""
    out = bytearray(len(chunky))
    for g in range(0, len(chunky), 4):
        p0 = p1 = p2 = p3 = 0
        for k in range(8):
            byte = chunky[g + (k >> 1)]
            px = (byte & 15) if (k & 1) == 0 else (byte >> 4)
            bit = 1 << (7 - k)
            if px & 1:
                p0 |= bit
            if px & 2:
                p1 |= bit
            if px & 4:
                p2 |= bit
            if px & 8:
                p3 |= bit
        out[g], out[g + 1], out[g + 2], out[g + 3] = p0, p1, p2, p3
    return bytes(out)
