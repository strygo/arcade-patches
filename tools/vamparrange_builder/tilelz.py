#!/usr/bin/env python3
"""
tilelz.py -- per-tile graphics codec for the Capcom PS2 Anthology
(Street Fighter Alpha Anthology / Vampire: Darkstalkers Collection).

SOLVED. The codec is the control-bit LZSS at SLUS_213.17 vaddr 0x148778
(disassembled directly from the EE executable). It decodes a COMPRESSED tile
block (bit31-flagged pointer in a tile container, AFS X_DATA entries ~208-545)
into a 128-byte 16x16 4bpp "chunky" tile (1 nibble/pixel, row-major, byte = 2
horizontally adjacent pixels, hi-nibble = left pixel). RAW blocks (bit31=0) are
already 128-byte chunky tiles.

ALGORITHM (from the 0x148778 disassembly; a0=src, a1=dst, a2=dst_end, t1=bitpos,
t2=control byte):
  - Read output MSB-first under an 8-bit CONTROL byte (a fresh control byte is
    fetched whenever the bit position runs out; block[0] is the first control
    byte -- always <0x80 because the first token must be a literal, there being
    no prior output to back-reference).
  - control bit CLEAR -> LITERAL: copy 1 byte from source to output.
  - control bit SET    -> BACK-REFERENCE over the already-decoded output:
        b = next source byte
        offset = (b >> 4) + 1        # 1..16 bytes back from current output end
        length = (b & 0x0f) + 2      # 2..17 bytes
        copy `length` bytes one-at-a-time from output[-offset] (overlap-capable,
        i.e. RLE when offset < length).
  - stop when 128 output bytes are produced.

VALIDATION: 100% of compressed blocks across all 175 containers decode to exactly
128 bytes; 98.5% consume their input exactly (the rest have 1-10 bytes of
container alignment padding). Decoded tiles are coherent 4bpp sprites
(compressed = simpler tiles ~7 colors; raw = complex tiles ~13 colors).

NOTE: this is the DECOMPRESSION only. The 128-byte chunky tile is a bit-
permutation ("chunky->CPS-2 planar") + interleave away from a MAME gfx ROM, and
is stored GS-swizzled + palette-remapped once uploaded to GS memory -- those are
separate transforms (see HANDOFF section 4), not part of the codec.
"""


def decompress(block, out_len=128):
    """Decompress one compressed tile block -> `out_len` (128) chunky bytes."""
    out = bytearray()
    sp = 0
    ctrl = 0
    bit = 0
    while len(out) < out_len:
        if bit == 0:
            ctrl = block[sp]; sp += 1; bit = 0x80
        if ctrl & bit:                       # back-reference
            b = block[sp]; sp += 1
            offset = (b >> 4) + 1
            length = (b & 0x0f) + 2
            for _ in range(length):
                out.append(out[-offset])
        else:                                # literal
            out.append(block[sp]); sp += 1
        bit >>= 1
    return bytes(out[:out_len])


def decompress_verbose(block, out_len=128):
    """Like decompress() but also return bytes consumed (for validation)."""
    out = bytearray(); sp = 0; ctrl = 0; bit = 0
    while len(out) < out_len:
        if bit == 0:
            ctrl = block[sp]; sp += 1; bit = 0x80
        if ctrl & bit:
            b = block[sp]; sp += 1
            offset = (b >> 4) + 1; length = (b & 0x0f) + 2
            for _ in range(length):
                out.append(out[-offset])
        else:
            out.append(block[sp]); sp += 1
        bit >>= 1
    return bytes(out[:out_len]), sp


def to_nibbles(tile):
    """128-byte chunky tile -> 256 pixel indices (row-major, hi-nibble first)."""
    px = []
    for b in tile:
        px.append(b >> 4); px.append(b & 0x0f)
    return px


if __name__ == '__main__':
    import sys, struct
    # self-test over a disc's tile containers: python3 tilelz.py <X_DATA.BIN>
    if len(sys.argv) < 2:
        raise SystemExit('usage: tilelz.py <X_DATA.BIN>')
    path = sys.argv[1]
    d = open(path, 'rb').read()
    nn = struct.unpack_from('<I', d, 4)[0]
    e = [struct.unpack_from('<II', d, 8 + 8 * i) for i in range(nn)]
    tot = ok = exact = 0
    for ent in range(nn):
        o, s = e[ent]
        if s < 0x100 or d[o:o + 0x14] != b'\0' * 0x14: continue
        if struct.unpack_from('<I', d, o + 0x18)[0] != 0x30: continue
        cnt = struct.unpack_from('<I', d, o + 0x14)[0]
        if not (1 <= cnt <= 0x10000): continue
        off2f = {}
        for i in range(cnt):
            p = struct.unpack_from('<I', d, o + 0x30 + 4 * i)[0]
            off2f[p & 0x7fffffff] = p >> 31
        offs = sorted(off2f)
        for k, off in enumerate(offs):
            if not off2f[off]: continue
            end = offs[k + 1] if k + 1 < len(offs) else s
            blk = d[o + off:o + end]
            try:
                out, used = decompress_verbose(blk)
                tot += 1
                if len(out) == 128: ok += 1
                if used == len(blk): exact += 1
            except Exception:
                tot += 1
    print('compressed blocks: %d | ->128B: %d (%.1f%%) | input-exact: %d (%.1f%%)'
          % (tot, ok, 100 * ok / tot, exact, 100 * exact / tot))
